#!/usr/bin/env python3
"""Record a data-minimal, read-only daily beta health sample."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo


def _command(argv: list[str], root: Path, runner) -> subprocess.CompletedProcess:
    return runner(argv, cwd=root, capture_output=True, text=True, timeout=60, check=False)


def collect(root: Path, *, runner=subprocess.run, now: dt.datetime | None = None) -> dict:
    """Whitelisted metrics only. Never persist raw CLI output or error detail."""
    moment = now or dt.datetime.now(ZoneInfo("Europe/Zurich"))
    sample: dict = {
        "ts": moment.isoformat(timespec="seconds"),
        "day": moment.date().isoformat(),
        "ok": False,
        "health_status": "unavailable",
        "version": None,
        "service_active": False,
        "service_restarts": None,
        "service_entered": None,
        "verify_ok": False,
        "chain_ok": False,
        "chain_broken_id": None,
        "events_total": None,
        "events_chained": None,
        "errors_24h": None,
        "runs_24h": None,
        "anchor_verify_ok": None,
        "schedules_available": None,
        "schedules_pending": None,
    }
    python = str(root / ".venv" / "bin" / "python")
    try:
        health = _command([python, "-m", "talos", "health", "--json"], root, runner)
        if health.returncode in (0, 1):
            data = json.loads(health.stdout)
            log = data.get("event_log") or {}
            chain = data.get("chain") or {}
            anchor = data.get("anchor") or {}
            schedules = data.get("schedules") or {}
            sample.update(
                health_status=data.get("status") if data.get("status") in ("ok", "critical") else "unavailable",
                chain_ok=chain.get("chain_ok") is True,
                chain_broken_id=chain.get("chain_broken_id") if isinstance(chain.get("chain_broken_id"), int) else None,
                events_total=log.get("events_total") if isinstance(log.get("events_total"), int) else None,
                events_chained=chain.get("chained") if isinstance(chain.get("chained"), int) else None,
                errors_24h=log.get("errors_24h") if isinstance(log.get("errors_24h"), int) else None,
                runs_24h=log.get("runs_24h") if isinstance(log.get("runs_24h"), int) else None,
                anchor_verify_ok=anchor.get("verify_ok") if anchor else None,
                schedules_available=schedules.get("available") if isinstance(schedules.get("available"), bool) else None,
                schedules_pending=schedules.get("pending") if isinstance(schedules.get("pending"), int) else None,
            )
        verify = _command([python, "-m", "talos", "verify"], root, runner)
        sample["verify_ok"] = verify.returncode == 0
        version = _command([python, "-m", "talos", "version"], root, runner)
        if version.returncode == 0 and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?", version.stdout.strip()):
            sample["version"] = version.stdout.strip()
        service = _command(
            ["systemctl", "--user", "show", "talos.service", "-p", "ActiveState",
             "-p", "NRestarts", "-p", "ActiveEnterTimestamp"], root, runner
        )
        if service.returncode == 0:
            values = dict(line.split("=", 1) for line in service.stdout.splitlines() if "=" in line)
            sample["service_active"] = values.get("ActiveState") == "active"
            if re.fullmatch(r"[0-9]+", values.get("NRestarts", "")):
                sample["service_restarts"] = int(values["NRestarts"])
            # A change in this timestamp across samples signals a restart.
            entered = values.get("ActiveEnterTimestamp", "")
            if re.fullmatch(r"[A-Za-z]{3} [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]+ [A-Z]+", entered):
                sample["service_entered"] = entered
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        pass
    sample["ok"] = all((
        sample["health_status"] == "ok",
        sample["chain_ok"],
        sample["verify_ok"],
        sample["service_active"],
        sample["service_restarts"] == 0,
        sample["version"] is not None,
        sample["events_total"] is not None,
        sample["events_chained"] is not None,
        sample["anchor_verify_ok"] is not False,
    ))
    return sample


def append_record(path: Path, sample: dict) -> None:
    if not path.is_absolute():
        raise ValueError("output path must be absolute")
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink():
        raise ValueError("output directory must not be a symlink")
    os.chmod(parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("output must be a regular file")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8", closefd=False) as stream:
            stream.write(json.dumps(sample, sort_keys=True, separators=(",", ":")) + "\n")
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("TALOS_BETA_ROOT"))
    parser.add_argument("--out", default=os.environ.get("TALOS_BETA_OUT"))
    args = parser.parse_args()
    if not args.root or not args.out:
        parser.error("--root and --out (or their environment variables) are required")
    root = Path(args.root)
    if not root.is_absolute() or not root.is_dir():
        parser.error("root must be an existing absolute directory")
    sample = collect(root)
    append_record(Path(args.out), sample)
    print(f"beta observation {sample['day']}: {'ok' if sample['ok'] else 'attention'}")
    return 0 if sample["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
