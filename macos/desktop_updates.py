"""Attended core updates; the signed app and operator profile stay in place."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def source_version(source: Path) -> str:
    tree = ast.parse((source / "talos/__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            version_key(value)
            return value
    raise RuntimeError("The runtime has no version metadata.")


def version_key(value: str) -> tuple[int, ...]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?", value)
    if not match:
        raise RuntimeError("The release version is not supported by this desktop app.")
    major, minor, patch, phase, number = match.groups()
    return (int(major), int(minor), int(patch), {"alpha": 0, "beta": 1, "rc": 2, None: 3}[phase], int(number or 0))


def selected_runtime(profile: Path, bundled: Path) -> Path:
    from desktop_runtime import private_path
    pointer = profile / "active-runtime.json"
    private_path(pointer)
    if not pointer.exists():
        return bundled
    record = json.loads(pointer.read_text())
    digest = record.get("sha256", "")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError("The desktop update record is invalid.")
    updates = profile / "updates"
    private_path(updates, directory=True)
    source = updates / digest
    private_path(source, directory=True)
    private_path(source / "talos", directory=True)
    private_path(source / "talos/__init__.py")
    version = source_version(source)
    if version != record.get("version") or not (source / ".venv/bin/python").is_file():
        raise RuntimeError("The desktop update is incomplete. Restore the previous update record.")
    # A newer app bundle must never be pinned to an older downloaded core.
    return source if version_key(version) > version_key(source_version(bundled)) else bundled


def check(current: str, *, fetch=None) -> dict:
    from talos import updater
    fetch = fetch or updater._urlopen_fetch
    latest = updater._published_version(fetch, updater.DEFAULT_BASE)
    available = version_key(latest) > version_key(current)
    notes = updater._optional_text(fetch, f"{updater.DEFAULT_BASE}/dist/CHANGELOG-{latest}.md")
    if not notes and latest == current:
        try:
            text = (Path(updater.__file__).resolve().parent.parent / "CHANGELOG.md").read_text(encoding="utf-8")
            marker = f"## [{current}]"
            if marker in text:
                notes = text[text.index(marker):][:12000]
        except (OSError, UnicodeError):
            pass
    return {"current": current, "latest": latest, "available": available,
            "notes": (notes or "Release notes are unavailable.")[:12000]}


def proof_runner(argv, cwd: Path) -> int:
    # In particular, no PYTHONPATH pointing back at the old runtime and no
    # operator config/credentials in the candidate's test environment.
    allowed = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # All updater steps invoke Python. -I ignores PYTHONDONTWRITEBYTECODE;
    # explicit -B also keeps pip from adding caches to the signed base interpreter.
    return subprocess.run([argv[0], "-B", *argv[1:]], cwd=cwd, env=env, check=False).returncode


def prepare_runner(destination: Path, runner):
    """A venv still uses its base stdlib. Keep update children out of the signed app.

    Tests may deliberately launch Python with -I, which ignores cache environment
    variables. Give the candidate a durable private interpreter before creating
    its venv, also keeping it independent of future app-bundle replacements.
    """
    base = Path(sys.base_prefix).resolve()
    if not any(part.endswith(".app") for part in base.parts) and base.name != ".desktop-python":
        return runner
    private_python = destination / ".desktop-python"
    if private_python.exists():
        raise RuntimeError("The release unexpectedly contains a desktop interpreter.")
    shutil.copytree(base, private_python, symlinks=True)
    executable = private_python / "bin" / Path(sys._base_executable).name
    if not executable.is_file() or not executable.resolve().is_relative_to(private_python.resolve()):
        raise RuntimeError("The desktop interpreter could not be isolated.")

    def run(argv, cwd):
        if list(argv[1:3]) == ["-m", "venv"]:
            argv = [str(executable), *argv[1:]]
        return runner(argv, cwd)
    return run


def install(profile: Path, current: str, *, fetch=None, runner=None, stdout=None) -> int:
    """Caller owns the session lock. Publish a pointer only after both proof suites."""
    from desktop_runtime import private_path, require_operator_terminal
    from talos import updater
    require_operator_terminal()
    fetch, runner = fetch or updater._urlopen_fetch, runner or proof_runner
    out = updater._Writer(stdout or sys.stdout)
    result = check(current, fetch=fetch)
    if not result["available"]:
        out.ok(f"Talos {current} is up to date. No files changed.")
        return 0
    updates = profile / "updates"
    private_path(updates, directory=True)
    updates.mkdir(mode=0o700, exist_ok=True)
    plan = updater._Plan(updater.DEFAULT_BASE, profile, updates / "unused", updates / "unused",
                         current, result["latest"])
    payload = updater._download(fetch, plan, out)
    digest = hashlib.sha256(payload).hexdigest()
    destination = updates / digest
    private_path(destination, directory=True)
    if destination.exists():
        raise RuntimeError("This update is already staged. Inspect the previous attempt before retrying.")
    plan = updater._Plan(plan.base, profile, destination, updates / "unused", current, result["latest"])
    pointer = profile / "active-runtime.json"
    private_path(pointer)
    activated = False
    try:
        with tempfile.TemporaryDirectory(prefix=".download-", dir=updates) as tmp:
            updater._unpack(payload, plan, Path(tmp))
        if source_version(destination) != plan.latest:
            raise RuntimeError("The signed archive version does not match the offered update.")
        updater._prepare(prepare_runner(destination, runner), plan, out)
        updater._prove(runner, plan, out)
        # Tests finish before the new code can see any operator state.
        for name in ("data", "workspace", "skills", "SOUL.md", "AGENTS.md", "USER.md"):
            target = destination / name
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            target.symlink_to(profile / name, target_is_directory=name in {"data", "workspace", "skills"})
        record = json.dumps({"version": plan.latest, "sha256": digest}) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".active-runtime-", dir=profile)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(record)
                stream.flush()
                os.fsync(stream.fileno())
            if pointer.exists():
                backup = profile / "previous-runtime.json"
                private_path(backup)
                shutil.copy2(pointer, backup)
            os.replace(temporary, pointer)
            activated = True
        finally:
            Path(temporary).unlink(missing_ok=True)
        out.ok(f"Talos {plan.latest} installed. Open a new chat to use it.")
        out.note("Your history, connections and workspace are unchanged. The previous runtime is retained.")
        return 0
    finally:
        if not activated and destination.exists():
            shutil.rmtree(destination)
