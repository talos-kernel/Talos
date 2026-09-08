"""Compatibility entry for the desktop's isolated Hermes inference profile."""
import os
import subprocess
import sys


def compatible_args(argv: list[str], help_text: str) -> list[str]:
    args = list(argv)
    # Some Hermes releases expose effort only in configuration. This cosmetic
    # inference preference must not prevent OAuth use; tool/identity flags stay.
    if "--reasoning" in args and "--reasoning " not in help_text:
        index = args.index("--reasoning")
        if index + 1 >= len(args) or args[index + 1] not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("Invalid reasoning preference")
        del args[index:index + 2]
    return args


if __name__ == "__main__":
    binary, isolated, *args = sys.argv[1:]
    for key in ("PYTHONPATH", "PYTHONHOME", "HERMES_PROFILE", "HERMES_CONFIG", "HERMES_ENV"):
        os.environ.pop(key, None)
    os.environ["HERMES_HOME"] = isolated
    if "--reasoning" in args:
        help_result = subprocess.run([binary, "--help"], capture_output=True, text=True, timeout=10)
        if help_result.returncode:
            raise SystemExit("Could not verify the installed Hermes command interface.")
        args = compatible_args(args, help_result.stdout)
    os.execv(binary, [binary, *args])
