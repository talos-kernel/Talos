"""Keep package-manager code replaceable and operator state durable.

Only the process environment and an installer-owned marker choose this layout.
talos.env cannot redirect its own protection boundary. Importing this module never
creates a profile, so diagnostics remain read-only.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat


INSTALL_DIR = Path(__file__).resolve().parent.parent
MANAGER_MARKER = INSTALL_DIR / ".talos-install-manager"


def install_manager() -> str:
    # A direct `python -m talos` must retain the same managed-install refusal as
    # the launcher, even when its environment flag is absent.
    marker = MANAGER_MARKER.read_text(encoding="utf-8").strip() if MANAGER_MARKER.is_file() else ""
    value = marker or os.environ.get("TALOS_INSTALL_MANAGER", "").strip()
    if value not in {"", "homebrew"}:
        raise ValueError("unknown Talos installation manager")
    return value


MANAGED = bool(install_manager())
PROFILE_DIR = Path(os.environ.get("TALOS_PROFILE_DIR") or (
    Path.home() / ".talos" / "homebrew" if MANAGED else INSTALL_DIR
)).expanduser()
if not PROFILE_DIR.is_absolute():
    raise ValueError("TALOS_PROFILE_DIR must be an absolute path")
if MANAGED and PROFILE_DIR.resolve().is_relative_to(INSTALL_DIR):
    raise ValueError("a managed profile must be outside the installed code")


def validate_profile() -> None:
    """Refuse another user's, shared or symlinked managed state; never repair it."""
    if PROFILE_DIR == INSTALL_DIR:
        return
    for path in (PROFILE_DIR, PROFILE_DIR / "data", PROFILE_DIR / "workspace"):
        if path.is_symlink():
            raise ValueError(f"profile directory must not be a symlink: {path}")
        if path.exists():
            info = path.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError(f"profile directory must be owned by this user with mode 700: {path}")
    for name in ("talos.env", "SOUL.md", "AGENTS.md", "USER.md"):
        path = PROFILE_DIR / name
        if path.is_symlink():
            raise ValueError(f"profile file must not be a symlink: {path}")
        if path.exists():
            info = path.stat()
            forbidden_mode = 0o027 if info.st_uid == 0 else 0o077
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & forbidden_mode:
                raise ValueError(f"profile file must be private, regular and singly linked: {path}")
            # Root-owned read-only configuration is a stronger boundary, not an
            # invitation to replace it. Initialization leaves every existing file.
            if info.st_uid not in {0, os.getuid()}:
                raise ValueError(f"profile file belongs to another user: {path}")


def initialize_profile(*, source: Path = INSTALL_DIR) -> None:
    """Seed missing identity templates once; upgrades never copy runtime state."""
    if PROFILE_DIR == INSTALL_DIR:
        return
    validate_profile()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("data", "workspace"):
        (PROFILE_DIR / name).mkdir(exist_ok=True, mode=0o700)
    validate_profile()
    for name in ("SOUL.md", "AGENTS.md", "USER.md"):
        template = source / name
        content = template.read_bytes() if template.is_file() else b""
        try:
            fd = os.open(PROFILE_DIR / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
