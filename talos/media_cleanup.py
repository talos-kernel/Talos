"""Opt-in lifecycle cleanup for disposable outbox file copies, after upload only."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Iterator

from . import policy


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass
class DisposableCopy:
    directory_fd: int
    name: str
    before: tuple[int, ...]
    path: str

    def remove(self) -> bool:
        """A changed/replaced file is retained. Never follow links or delete directories."""
        current = os.stat(self.name, dir_fd=self.directory_fd, follow_symlinks=False)
        visible = os.stat(self.path, follow_symlinks=False)
        if _stamp(current) != self.before or _stamp(visible) != self.before:
            return False
        os.unlink(self.name, dir_fd=self.directory_fd)
        return True


@contextmanager
def disposable_copy(path: str, *, enabled: bool) -> Iterator[DisposableCopy | None]:
    """Pin a flat outbox directory before upload; no work without the operator opt-in.

    The caller has already applied attachment.resolve. Directory descriptors prevent
    ancestor swaps during upload from redirecting cleanup to another file. Symlinks,
    hardlinks, nested paths, originals and reference inputs are never candidates.
    """
    workspace = Path(os.path.realpath(policy.WORKSPACE_DIR))
    target = Path(path)
    if not enabled or target.parent != workspace / "outbox":
        yield None
        return
    directory_fd = None
    candidate = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = os.open(workspace, flags)
        try:
            directory_fd = os.open("outbox", flags, dir_fd=root_fd)
        finally:
            os.close(root_fd)
        info = os.stat(target.name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            candidate = DisposableCopy(directory_fd, target.name, _stamp(info), path)
    except (OSError, AttributeError):
        # Cleanup is optional. An unreadable/unsupported candidate must not prevent
        # delivery or become an invitation to loosen filesystem permissions.
        pass
    try:
        yield candidate
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
