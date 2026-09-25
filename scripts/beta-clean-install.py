#!/usr/bin/env python3
"""Execute the shipped installer in a disposable prefix and read back its result."""
from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", default="0.19.23-alpha")
    args = parser.parse_args()
    installer = ROOT / "site" / "install.sh"
    if f'VERSION="{args.current}"' not in installer.read_text():
        parser.error("requested version does not match the shipped installer")
    with tempfile.TemporaryDirectory(prefix="talos-beta-install-") as temporary:
        root = Path(temporary)
        prefix = root / "installation"
        bin_dir = root / "bin"
        environment = {key: os.environ[key] for key in
                       ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE",
                        "REQUESTS_CA_BUNDLE")
                       if key in os.environ}
        environment.update({"TALOS_BASE": "https://talos-agent.ch",
                            "TALOS_PREFIX": str(prefix), "TALOS_BIN_DIR": str(bin_dir),
                            "PIP_NO_INPUT": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                            "PYTHONNOUSERSITE": "1", "NO_COLOR": "1"})
        installed = subprocess.run(["bash", str(installer)], cwd=ROOT,
                                   env=environment, capture_output=True, text=True,
                                   check=False)
        if installed.returncode:
            tail = "\n".join((installed.stdout + "\n" + installed.stderr).splitlines()[-35:])
            print(f"Clean install failed (exit {installed.returncode}):\n{tail}", file=sys.stderr)
            return 1
        version = subprocess.run([str(prefix / ".venv" / "bin" / "python"),
                                  "-m", "talos", "version"], cwd=prefix,
                                 capture_output=True, text=True, check=False)
        if version.returncode or version.stdout.strip() != args.current:
            print("Installed version read-back failed", file=sys.stderr)
            return 1
        config = prefix / "talos.env"
        if not config.is_file() or stat.S_IMODE(config.stat().st_mode) != 0o600:
            print("Installed configuration mode read-back failed", file=sys.stderr)
            return 1
        if not (bin_dir / "talos").is_symlink() or not (prefix / "data").is_dir():
            print("Installed command or data directory missing", file=sys.stderr)
            return 1
        print(f"Clean install: {args.current}, suites passed, config 0600, command and data read back")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
