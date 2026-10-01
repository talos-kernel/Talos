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
    parser.add_argument("--previous", default="0.19.23-alpha")
    parser.add_argument("--current", default=__version__)
    parser.add_argument("--base", default="https://talos-agent.ch")
    parser.add_argument("--assets-dir", type=Path)
    parser.add_argument("--without-upgrade", action="store_true",
                        help="Run source gates only; does not qualify as a complete beta gate")
    args = parser.parse_args()
    if shutil.which("osv-scanner") is None:
        print("FAILED: osv-scanner is required", file=sys.stderr)
        return 1
    checks = (
        ("Regression suite", [sys.executable, "-m", "pytest", "tests/", "-q"]),
        ("Adversarial suite", [sys.executable, "redteam.py"]),
        ("Public hygiene", [sys.executable, "scripts/check-public-hygiene.py"]),
        ("Locked dependency scan", ["osv-scanner", "scan", "source", "--no-resolve",
                                    "--lockfile", "requirements.txt:requirements.lock",
                                    "--lockfile", "requirements.txt:requirements-dev.lock"]),
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
