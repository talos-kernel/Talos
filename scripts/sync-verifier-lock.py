#!/usr/bin/env python3
"""Embed the existing, audited verifier pins before the archive trust boundary."""
import argparse
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
BEGIN = "# BEGIN PINNED VERIFIER\n"
END = "# END PINNED VERIFIER\n"


def verifier_lock(runtime: str) -> str:
    wanted = {"cryptography", "cffi", "pycparser"}
    blocks = re.split(r"(?=^[a-zA-Z0-9][a-zA-Z0-9_.-]*==)", runtime, flags=re.M)
    selected = []
    found = set()
    for block in blocks:
        name = block.split("==", 1)[0]
        if name in wanted:
            # Comments may mention runtime-only consumers; hashes and markers stay.
            selected.append("\n".join(line for line in block.splitlines()
                                      if not line.lstrip().startswith("#")).rstrip() + "\n")
            found.add(name)
    if found != wanted:
        raise ValueError("runtime lock must contain the entire verifier dependency closure")
    return "# Generated from requirements.lock by scripts/sync-verifier-lock.py.\n" + "".join(selected)


def embedded(lock: str) -> str:
    return (BEGIN + 'cat > "$TMP/verifier.lock" <<\'VERIFIER_LOCK\'\n' + lock
            + "VERIFIER_LOCK\n" + END)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    lock = verifier_lock((ROOT / "requirements.lock").read_text())
    installer = ROOT / "site/install.sh"
    content = installer.read_text()
    start, stop = content.index(BEGIN), content.index(END) + len(END)
    updated = content[:start] + embedded(lock) + content[stop:]
    target = ROOT / "requirements-verifier.lock"
    if args.check:
        if not target.exists() or target.read_text() != lock or updated != content:
            raise SystemExit("Verifier lock or installer is stale; run scripts/sync-verifier-lock.py")
    else:
        target.write_text(lock)
        installer.write_text(updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
