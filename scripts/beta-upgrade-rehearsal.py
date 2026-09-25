#!/usr/bin/env python3
"""Rehearse a published, signed upgrade and rollback in a disposable directory.

The old release's own updater performs the real download verification, venv install,
test suites, state copy and switch. No live installation or service is touched.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from talos.updater import RELEASE_PUBLIC_KEY

REPOSITORY = "talos-kernel/talos"


def _artifact(directory: Path, version: str, suffix: str) -> Path:
    return directory / f"talos-{version}.tar.gz{suffix}"


def _download(version: str, directory: Path) -> None:
    names = [_artifact(directory, version, suffix).name for suffix in ("", ".sha256", ".sig")]
    result = subprocess.run(
        ["gh", "release", "download", f"v{version}", "-R", REPOSITORY,
         "--dir", str(directory), "--clobber", "--pattern", f"talos-{version}.tar.gz*"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode or any(not (directory / name).is_file() for name in names):
        raise RuntimeError(f"Could not download all signed assets for {version}: {result.stderr.strip()}")


def _verify(directory: Path, version: str) -> bytes:
    archive = _artifact(directory, version, "")
    payload = archive.read_bytes()
    expected = _artifact(directory, version, ".sha256").read_text().split()[0]
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise RuntimeError(f"SHA-256 mismatch for {version}")
    signature = base64.b64decode(_artifact(directory, version, ".sig").read_bytes().strip(), validate=True)
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(RELEASE_PUBLIC_KEY))
    key.verify(signature, payload)
    print(f"{version}: SHA-256 and Ed25519 verified ({actual[:16]}…)", flush=True)
    return payload


def _extract_old(payload: bytes, destination: Path) -> None:
    archive_path = destination.parent / "old-release.tar.gz"
    archive_path.write_bytes(payload)
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        if any(member.name.startswith("/") or ".." in Path(member.name).parts
               or not (member.isfile() or member.isdir()) for member in members):
            raise RuntimeError("Old archive has unsafe members")
        roots = {Path(member.name).parts[0] for member in members}
        if len(roots) != 1:
            raise RuntimeError("Old archive does not have exactly one root")
        try:
            archive.extractall(destination.parent, filter="data")
        except TypeError:  # Python 3.11 builds before the filter backport
            archive.extractall(destination.parent)
    extracted = destination.parent / next(iter(roots))
    extracted.rename(destination)


def _python(code: str, tree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = {key: os.environ[key] for key in
                   ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE",
                    "REQUESTS_CA_BUNDLE")
                   if key in os.environ}
    environment.update({"PIP_NO_INPUT": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                        "PYTHONNOUSERSITE": "1"})
    environment["PYTHONPATH"] = str(tree)
    return subprocess.run([sys.executable, "-c", code, *args], cwd=tree,
                          env=environment, capture_output=True, text=True, check=False)


def _check_result(result: subprocess.CompletedProcess[str], step: str) -> None:
    if result.returncode:
        tail = "\n".join((result.stdout + "\n" + result.stderr).splitlines()[-35:])
        raise RuntimeError(f"{step} failed (exit {result.returncode}):\n{tail}")


def rehearse(previous: str, current: str, supplied_assets: Path | None = None) -> None:
    with tempfile.TemporaryDirectory(prefix="talos-beta-upgrade-") as temporary:
        root = Path(temporary)
        assets = supplied_assets if supplied_assets is not None else root / "assets"
        if supplied_assets is None:
            assets.mkdir()
            for version in (previous, current):
                _download(version, assets)
        old_payload = _verify(assets, previous)
        _verify(assets, current)

        installation = root / "installation"
        _extract_old(old_payload, installation)
        if previous not in (installation / "talos" / "__init__.py").read_text():
            raise RuntimeError("Old archive version does not match the requested tag")
        (installation / "talos.env").write_text("TALOS_ALLOWED_PRINCIPALS=\n")
        (installation / "talos.env").chmod(0o600)
        (installation / "USER.md").write_text("rehearsal preference\n")
        (installation / "workspace").mkdir(exist_ok=True)
        (installation / "workspace" / "marker.txt").write_text("rehearsal workspace\n")

        seed = _python(
            "from pathlib import Path; from talos.schedule import ScheduleStore; "
            "s=ScheduleStore(Path('data/schedules.db')); "
            "assert s.available; "
            "t=s.add(conversation='rehearsal', principal='cli:rehearsal', "
            "prompt='rehearsal schedule', interval_s=3600); "
            "assert t is not None; s.close()",
            installation,
        )
        _check_result(seed, "Seed schedule with previous release")

        # All network reads are pinned to the downloaded release assets. The old
        # updater itself still runs the actual venv, pip, pytest and red-team steps.
        updater_code = """
import sys
from pathlib import Path
from talos.updater import run_update
assets = Path(sys.argv[1])
version = sys.argv[2]
prefix = sys.argv[3]
def fetch(url):
    name = url.rsplit('/', 1)[-1]
    if name == 'latest.txt':
        return (version + '\\n').encode()
    path = assets / name
    if not path.is_file():
        raise FileNotFoundError(name)
    return path.read_bytes()
raise SystemExit(run_update(['--prefix', prefix, '--base', 'https://example.invalid'], http=fetch))
"""
        upgraded = _python(updater_code, installation, str(assets), current, str(installation))
        _check_result(upgraded, "Signed update through previous release's updater")
        if current not in (installation / "talos" / "__init__.py").read_text():
            raise RuntimeError("Update did not switch to current release")
        for relative, expected in (("talos.env", "TALOS_ALLOWED_PRINCIPALS=\n"),
                                   ("USER.md", "rehearsal preference\n"),
                                   ("workspace/marker.txt", "rehearsal workspace\n")):
            if (installation / relative).read_text() != expected:
                raise RuntimeError(f"Operator state was not preserved: {relative}")
        if (installation / "talos.env").stat().st_mode & 0o777 != 0o600:
            raise RuntimeError("Operator configuration mode was not preserved")

        migrated = _python(
            "from pathlib import Path; from talos.schedule import ScheduleStore; "
            "s=ScheduleStore(Path('data/schedules.db')); "
            "assert s.available; "
            "rows=s.list_for('rehearsal'); "
            "assert len(rows)==1 and rows[0].prompt=='rehearsal schedule'; "
            "assert rows[0].heartbeat is False; s.close()",
            installation,
        )
        _check_result(migrated, "Read schedule with new release")
        with sqlite3.connect(installation / "data" / "schedules.db") as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(schedules)")}
        if "heartbeat" not in columns:
            raise RuntimeError("Heartbeat migration was not applied")
        print("Upgrade: suites passed; operator state and schedule migration read back", flush=True)

        retired = root / f"installation.old-{previous}"
        if not retired.is_dir():
            raise RuntimeError("Updater did not retain the previous tree")
        upgraded_tree = root / "upgraded-tree"
        installation.rename(upgraded_tree)
        retired.rename(installation)
        if previous not in (installation / "talos" / "__init__.py").read_text():
            raise RuntimeError("Rollback did not restore previous release")
        if (installation / "talos.env").stat().st_mode & 0o777 != 0o600:
            raise RuntimeError("Rollback did not restore the configuration mode")
        rolled_back = _python(
            "from pathlib import Path; from talos.schedule import ScheduleStore; "
            "s=ScheduleStore(Path('data/schedules.db')); "
            "assert s.available and len(s.list_for('rehearsal'))==1; s.close()",
            installation,
        )
        _check_result(rolled_back, "Read original schedule after rollback")
        print("Rollback: previous release and original schedule read back", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", default="0.19.22-alpha")
    parser.add_argument("--current", default="0.19.23-alpha")
    parser.add_argument("--assets-dir", type=Path,
                        help="use six pre-downloaded release assets, e.g. on a host without gh")
    args = parser.parse_args()
    if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", version)
               for version in (args.previous, args.current)):
        parser.error("versions must be plain release identifiers")
    try:
        rehearse(args.previous, args.current, args.assets_dir)
    except Exception as error:
        print(f"Beta upgrade rehearsal failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
