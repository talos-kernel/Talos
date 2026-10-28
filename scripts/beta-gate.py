#!/usr/bin/env python3
"""Run the local, reproducible Talos beta gates; fail closed on a missing check."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from talos import __version__

LOCKFILES = (
    "requirements.lock",
    "requirements-dev.lock",
    "requirements-verifier.lock",
    "deploy/computer-requirements.lock",
)


def run(label: str, argv: list[str]) -> bool:
    print(f"\n=== {label} ===", flush=True)
    result = subprocess.run(argv, cwd=ROOT, check=False, capture_output=True, text=True)
    lines = (result.stdout + "\n" + result.stderr).splitlines()
    if result.returncode:
        print("\n".join(lines[-40:]), flush=True)
        print(f"FAILED: {label} (exit {result.returncode})", flush=True)
        return False
    print("\n".join(lines[-5:]), flush=True)
    print(f"PASSED: {label}", flush=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", help="Explicit published starting version for the upgrade rehearsal")
    parser.add_argument("--current", default=__version__)
    parser.add_argument("--base", default="https://talos-agent.ch")
    parser.add_argument("--assets-dir", type=Path)
    parser.add_argument("--without-upgrade", action="store_true",
                        help="Run source gates only; does not qualify as a complete beta gate")
    args = parser.parse_args()
    if not args.without_upgrade:
        if not args.previous:
            parser.error("--previous is required for a full gate; name the release being upgraded")
        if args.previous == args.current:
            parser.error("--previous and --current must differ; a no-op is not an upgrade rehearsal")
    # The verifier and optional computer environment are independently installed.
    # Scanning only the core runtime leaves both outside the release evidence.
    missing = [name for name in LOCKFILES if not (ROOT / name).is_file()]
    if missing:
        print("FAILED: required dependency locks missing: " + ", ".join(missing), file=sys.stderr)
        return 1
    if shutil.which("osv-scanner") is None:
        print("FAILED: osv-scanner is required", file=sys.stderr)
        return 1
    checks = (
        ("Release tooling", [sys.executable, "-m", "pytest", "scripts/tests/", "-q"]),
        ("Regression suite", [sys.executable, "-m", "pytest", "tests/", "-q"]),
        ("Adversarial suite", [sys.executable, "redteam.py"]),
        ("Public hygiene", [sys.executable, "scripts/check-public-hygiene.py"]),
        ("Embedded verifier pins", [sys.executable, "scripts/sync-verifier-lock.py", "--check"]),
        ("Locked dependency scan", ["osv-scanner", "scan", "source", "--no-resolve",
                                    *[arg for name in LOCKFILES
                                      for arg in ("--lockfile", "requirements.txt:" + name)]]),
    )
    for label, argv in checks:
        if not run(label, argv):
            return 1
    if args.without_upgrade:
        print("Source gates passed; signed upgrade/rollback was NOT run.")
        return 0
    if args.assets_dir is None and shutil.which("gh") is None:
        print("FAILED: gh is required for published release assets", file=sys.stderr)
        return 1
    if not run("Clean install from published release",
               [sys.executable, "scripts/beta-clean-install.py", "--current", args.current,
                "--base", args.base]):
        return 1
    if not run("Signed upgrade, migration and rollback",
               [sys.executable, "scripts/beta-upgrade-rehearsal.py",
                "--previous", args.previous, "--current", args.current,
                *(["--assets-dir", str(args.assets_dir.resolve())] if args.assets_dir else [])]):
        return 1
    print("\nAll local beta gates passed. Hosted CI and observation gates remain separate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
