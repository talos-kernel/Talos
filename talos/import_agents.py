"""Read-only migration preview for portable Hermes and OpenClaw user data.

The importer deliberately stops at a plan.  Source files are foreign, persistent
instructions; copying them into the active profile would bypass Talos' review and
``skill_manage`` approval boundaries.  This module therefore reads a small allowlist,
labels every accepted item for inactive staging, and never creates or changes a file.

Secrets, authority, executable configuration, schedules, sessions and databases are
listed as skipped categories without opening their source files.  The plan contains
only sizes and hashes of portable files, never their contents.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from . import skills
from .recall import looks_secret

PLAN_VERSION = 1
SOURCE_TYPES = ("hermes", "openclaw")
STAGING_ROOT = Path("data") / "import-staging"
MAX_PORTABLE_BYTES = 256 * 1024
MAX_SKILL_ROOTS = 64
MAX_SKILL_CANDIDATES = 500
MAX_DIRECTORY_ENTRIES = 1_024

STATUS_STAGED = "staged"
STATUS_CONFLICT = "conflict"
STATUS_REJECTED = "rejected"
STATUS_SKIPPED = "skipped"

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class ImportPlanError(ValueError):
    """The requested source/target boundary is not safe enough to inspect."""


@dataclass(frozen=True)
class ImportItem:
    """One reviewable mapping; content is intentionally absent."""

    category: str
    source: str
    target: str
    status: str
    reason: str
    bytes: int = 0
    sha256: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ImportPlan:
    """A deterministic, serialisable dry-run result."""

    version: int
    source_type: str
    items: tuple[ImportItem, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "source_type": self.source_type,
            "items": [item.to_dict() for item in self.items],
        }

    def fingerprint(self) -> str:
        """Stable confirmation token; contains no source root or file content."""
        encoded = json.dumps(
            self.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def render(self) -> str:
        counts = {status: 0 for status in (
            STATUS_STAGED, STATUS_CONFLICT, STATUS_REJECTED, STATUS_SKIPPED
        )}
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        lines = [
            f"Talos import preview v{self.version} — {self.source_type}",
            "Dry run only: no files were changed. Imported text is untrusted data and grants no permission.",
        ]
        for item in self.items:
            suffix = f" ({_display(item.reason)})" if item.reason else ""
            mapping = f" -> {_display(item.target)}" if item.target else ""
            lines.append(
                f"[{item.status}] {item.category}: {_display(item.source)}{mapping}{suffix}"
            )
        lines.append(
            "Summary: " + ", ".join(
                f"{counts[status]} {status}" for status in (
                    STATUS_STAGED, STATUS_CONFLICT, STATUS_REJECTED, STATUS_SKIPPED
                )
            )
        )
        lines.append(f"Apply confirmation: {self.fingerprint()}")
        return "\n".join(lines)


@dataclass(frozen=True)
class _Candidate:
    category: str
    source: Path
    target: Path


def detect_source(source: str | os.PathLike[str]) -> str:
    """Detect a strong on-disk signature without opening configuration files."""
    root = _safe_root(source, "source", must_exist=True)
    openclaw = any(_entry_kind(root / name) == "file" for name in (
        "openclaw.json", "clawdbot.json", "moltbot.json"
    ))
    hermes = _entry_kind(root / "config.yaml") == "file" or (
        _entry_kind(root / "memories") == "directory"
        and _entry_kind(root / "skills") == "directory"
    )
    if openclaw and hermes:
        raise ImportPlanError("source matches both Hermes and OpenClaw; choose the source type explicitly")
    if openclaw:
        return "openclaw"
    if hermes:
        return "hermes"
    raise ImportPlanError("source is not a recognised Hermes or OpenClaw home")


def plan_import(
    source: str | os.PathLike[str],
    target: str | os.PathLike[str],
    *,
    source_type: str = "auto",
) -> ImportPlan:
    """Inspect allowlisted portable data and return a zero-write migration plan."""
    source_root = _safe_root(source, "source", must_exist=True)
    target_root = _safe_root(target, "target", must_exist=False)
    if _paths_overlap(source_root, target_root):
        raise ImportPlanError("source and target directories must not overlap")
    kind = detect_source(source_root) if source_type == "auto" else source_type
    if kind not in SOURCE_TYPES:
        raise ImportPlanError(f"source type must be one of: auto, {', '.join(SOURCE_TYPES)}")

    candidates, skill_roots = (
        _hermes_candidates(source_root, target_root)
        if kind == "hermes"
        else _openclaw_candidates(source_root, target_root)
    )
    items = [_inspect_candidate(source_root, target_root, candidate) for candidate in candidates]
    items.extend(_inspect_skills(source_root, target_root, kind, skill_roots))
    items.extend(_skipped_items(kind))
    ordered = tuple(sorted(items, key=lambda item: (
        item.status != STATUS_STAGED, item.category, item.source, item.target
    )))
    return ImportPlan(
        version=PLAN_VERSION,
        source_type=kind,
        items=ordered,
    )


def _hermes_candidates(source: Path, target: Path) -> tuple[list[_Candidate], tuple[Path, ...]]:
    stage = target / STAGING_ROOT / "hermes"
    candidates: list[_Candidate] = []
    _first(candidates, "identity", source, stage / "profile" / "SOUL.md", ("SOUL.md",))
    _first(candidates, "instructions", source, stage / "profile" / "AGENTS.md", ("AGENTS.md",))
    _first(candidates, "preferences", source, stage / "profile" / "USER.md", (
        "memories/USER.md", "USER.md"
    ))
    _first(candidates, "memory", source, stage / "memory" / "MEMORY.md", (
        "memories/MEMORY.md", "MEMORY.md"
    ))
    return candidates, (source / "skills",)


def _openclaw_candidates(source: Path, target: Path) -> tuple[list[_Candidate], tuple[Path, ...]]:
    stage = target / STAGING_ROOT / "openclaw"
    workspace = _first_directory(source, ("workspace", "workspace-main", "workspace.default"))
    if workspace is None:
        return [], ()
    candidates: list[_Candidate] = []
    _first(candidates, "identity", workspace, stage / "profile" / "SOUL.md", ("SOUL.md",))
    _first(candidates, "instructions", workspace, stage / "profile" / "AGENTS.md", ("AGENTS.md",))
    _first(candidates, "preferences", workspace, stage / "profile" / "USER.md", ("USER.md",))
    _first(candidates, "memory", workspace, stage / "memory" / "MEMORY.md", ("MEMORY.md",))
    return candidates, (workspace / "skills",)


def _first(
    candidates: list[_Candidate],
    category: str,
    base: Path,
    target: Path,
    names: Iterable[str],
) -> None:
    for name in names:
        candidate = base / name
        if _entry_kind(candidate) is not None:
            candidates.append(_Candidate(category, candidate, target))
            return


def _first_directory(source: Path, names: Iterable[str]) -> Path | None:
    for name in names:
        candidate = source / name
        if _entry_kind(candidate) == "directory":
            return candidate
        if _entry_kind(candidate) == "symlink":
            raise ImportPlanError("OpenClaw workspace must be a real directory (no symlinks)")
    return None


def _inspect_candidate(source_root: Path, target_root: Path, candidate: _Candidate) -> ImportItem:
    source_label = _relative_label(source_root, candidate.source)
    target_label = _relative_label(target_root, candidate.target)
    try:
        data = _read_portable(source_root, candidate.source)
        text = data.decode("utf-8")
        if not text.strip():
            raise ImportPlanError("empty portable file")
        secret = looks_secret(text)
        if secret:
            raise ImportPlanError(f"secret-like content ({secret})")
    except (ImportPlanError, UnicodeDecodeError, OSError) as error:
        reason = "not valid UTF-8" if isinstance(error, UnicodeDecodeError) else str(error)
        return ImportItem(candidate.category, source_label, target_label,
                          STATUS_REJECTED, reason)
    digest = hashlib.sha256(data).hexdigest()
    target_state = _target_state(target_root, candidate.target)
    if target_state == "unsafe":
        return ImportItem(
            candidate.category, source_label, target_label, STATUS_REJECTED,
            "staging target ancestry contains a symlink; target not inspected",
        )
    conflict = target_state == "conflict"
    return ImportItem(
        candidate.category,
        source_label,
        target_label,
        STATUS_CONFLICT if conflict else STATUS_STAGED,
        "staging target already exists; no overwrite" if conflict else "review required before activation",
        len(data),
        digest,
    )


def _inspect_skills(
    source_root: Path,
    target_root: Path,
    kind: str,
    roots: Iterable[Path],
) -> list[ImportItem]:
    items: list[ImportItem] = []
    seen: set[str] = set()
    files, rejected = _skill_files(source_root, tuple(roots))
    items.extend(rejected)
    for skill_file in files:
        source_label = _relative_label(source_root, skill_file)
        name = skill_file.parent.name
        target = target_root / STAGING_ROOT / kind / "skills" / name / skills.SKILL_FILENAME
        target_label = _relative_label(target_root, target)
        if name in seen:
            items.append(ImportItem("skill", source_label, target_label,
                                    STATUS_REJECTED, "duplicate skill name"))
            continue
        seen.add(name)
        item = _inspect_candidate(
            source_root, target_root, _Candidate("skill", skill_file, target)
        )
        if item.status in {STATUS_STAGED, STATUS_CONFLICT}:
            try:
                data = _read_portable(source_root, skill_file)
                text = data.decode("utf-8")
                head, _body = skills._split_frontmatter(text)
                fields, _metadata = skills._parse_frontmatter(head)
                skills._validated_identity(fields, name)
            except (skills.SkillError, ImportPlanError, UnicodeDecodeError, OSError):
                # Parser errors can contain attacker-controlled frontmatter. Keep the
                # reason generic so malformed persistent text cannot leak through a plan.
                item = ImportItem("skill", source_label, target_label,
                                  STATUS_REJECTED, "malformed Agent Skill frontmatter")
        items.append(item)
    return items


def _skill_files(
    source_root: Path,
    roots: tuple[Path, ...],
) -> tuple[tuple[Path, ...], list[ImportItem]]:
    """Find direct Agent Skills plus one category layer without opening any file."""
    files: list[Path] = []
    rejected: list[ImportItem] = []
    categories: list[Path] = []
    for root in roots:
        kind = _entry_kind(root)
        if kind is None:
            continue
        if kind != "directory":
            rejected.append(ImportItem(
                "skill", _relative_label(source_root, root), "", STATUS_REJECTED,
                "skill root must be a real directory (no symlinks)",
            ))
            continue
        children, overflow = _bounded_children(root)
        if overflow:
            rejected.append(ImportItem(
                "skill", _relative_label(source_root, root), "", STATUS_REJECTED,
                f"skill root exceeds {MAX_DIRECTORY_ENTRIES} visible entries",
            ))
        for child in children:
            child_kind = _entry_kind(child)
            if child_kind == "symlink":
                rejected.append(ImportItem(
                    "skill", _relative_label(source_root, child), "", STATUS_REJECTED,
                    "skill directory is a symlink",
                ))
                continue
            if child_kind != "directory":
                continue
            descriptor = child / skills.SKILL_FILENAME
            if _entry_kind(descriptor) is not None:
                files.append(descriptor)
            elif len(categories) < MAX_SKILL_ROOTS:
                categories.append(child)

    for category in categories:
        children, overflow = _bounded_children(category)
        if overflow:
            rejected.append(ImportItem(
                "skill", _relative_label(source_root, category), "", STATUS_REJECTED,
                f"skill category exceeds {MAX_DIRECTORY_ENTRIES} visible entries",
            ))
        for child in children:
            child_kind = _entry_kind(child)
            if child_kind == "symlink":
                rejected.append(ImportItem(
                    "skill", _relative_label(source_root, child), "", STATUS_REJECTED,
                    "skill directory is a symlink",
                ))
                continue
            descriptor = child / skills.SKILL_FILENAME
            if child_kind == "directory" and _entry_kind(descriptor) is not None:
                files.append(descriptor)
            if len(files) >= MAX_SKILL_CANDIDATES:
                rejected.append(ImportItem(
                    "skill", _relative_label(source_root, category), "", STATUS_REJECTED,
                    f"skill candidate limit of {MAX_SKILL_CANDIDATES} reached",
                ))
                break
        if len(files) >= MAX_SKILL_CANDIDATES:
            break
    ordered = tuple(sorted(set(files[:MAX_SKILL_CANDIDATES]), key=lambda path: path.as_posix()))
    return ordered, rejected


def _bounded_children(root: Path) -> tuple[tuple[Path, ...], bool]:
    """Enumerate a directory through a pinned no-follow fd with bounded memory."""
    try:
        descriptor = _open_directory_nofollow(root)
    except OSError as error:
        raise ImportPlanError("skill directory could not be inspected safely") from error
    names: list[str] = []
    try:
        with os.scandir(descriptor) as entries:
            for count, entry in enumerate(entries, start=1):
                if count > MAX_DIRECTORY_ENTRIES:
                    return (), True
                if entry.name and not entry.name.startswith("."):
                    names.append(entry.name)
    except OSError as error:
        raise ImportPlanError("skill directory could not be inspected safely") from error
    finally:
        os.close(descriptor)
    return tuple(root / name for name in sorted(names)), False


def _skipped_items(kind: str) -> list[ImportItem]:
    if kind == "hermes":
        rows = (
            ("secrets", ".env, auth.json, mcp-tokens/", "credentials are re-entered through Talos setup"),
            ("authority", "config.yaml approvals and command allowlists", "authority never migrates"),
            ("executables", "plugins/, hooks/, MCP command configuration", "executable configuration stays inactive"),
            ("schedules", "cron/, plans/", "scheduled effects require separate operator review"),
            ("sessions", "sessions/, logs/, databases", "private history is archive-only"),
        )
    else:
        rows = (
            ("secrets", ".env, auth profiles, tokens inside openclaw.json", "credentials are re-entered through Talos setup"),
            ("authority", "exec-approvals.json and approval policy", "authority never migrates"),
            ("executables", "plugins/, hooks/, MCP command configuration", "executable configuration stays inactive"),
            ("schedules", "cron/, HEARTBEAT.md", "scheduled effects require separate operator review"),
            ("sessions", "sessions/, logs/, databases", "private history is archive-only"),
        )
    return [ImportItem(category, source, "", STATUS_SKIPPED, reason)
            for category, source, reason in rows]


def _safe_root(raw: str | os.PathLike[str], label: str, *, must_exist: bool) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ImportPlanError(f"{label} path must be absolute")
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if must_exist:
                raise ImportPlanError(f"{label} path does not exist") from None
            break
        if stat.S_ISLNK(info.st_mode):
            raise ImportPlanError(f"{label} path contains a symlink")
    if must_exist:
        try:
            info = path.lstat()
        except FileNotFoundError:
            raise ImportPlanError(f"{label} path does not exist") from None
        if not stat.S_ISDIR(info.st_mode):
            raise ImportPlanError(f"{label} path must be a directory")
    elif _entry_kind(path) not in {None, "directory"}:
        raise ImportPlanError(f"{label} path must be a directory or absent")
    return path


def _entry_kind(path: Path) -> str | None:
    try:
        mode = path.lstat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return None
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "special"


def _relative_label(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return "[outside source refused]"
    return relative.as_posix() or "."


def _display(value: str) -> str:
    """Quote attacker-controlled labels so terminals cannot interpret controls."""
    return json.dumps(value, ensure_ascii=True)


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _target_state(root: Path, path: Path) -> str:
    """Probe target ancestry one component at a time, never through a symlink."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return "unsafe"
    current = root
    for index, part in enumerate(relative.parts):
        current /= part
        kind = _entry_kind(current)
        if kind is None:
            return "absent"
        if kind == "symlink":
            return "unsafe"
        if index < len(relative.parts) - 1 and kind != "directory":
            return "conflict"
    return "conflict"


def _read_portable(root: Path, path: Path) -> bytes:
    """Open each component without following links; read a bounded regular file."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ImportPlanError("portable path escapes the source root") from None
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ImportPlanError("invalid portable path")
    descriptors: list[int] = []
    try:
        current = _open_directory_nofollow(root)
        descriptors.append(current)
        for part in relative.parts[:-1]:
            current = os.open(part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=current)
            descriptors.append(current)
        fd = os.open(relative.parts[-1], os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=current)
        descriptors.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ImportPlanError("portable source is not a regular file")
        if info.st_nlink != 1:
            raise ImportPlanError("portable source must not be hard-linked")
        if info.st_size > MAX_PORTABLE_BYTES:
            raise ImportPlanError(f"portable source exceeds {MAX_PORTABLE_BYTES} bytes")
        chunks: list[bytes] = []
        remaining = MAX_PORTABLE_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_PORTABLE_BYTES:
            raise ImportPlanError(f"portable source exceeds {MAX_PORTABLE_BYTES} bytes")
        return data
    except OSError as error:
        raise ImportPlanError(f"portable source refused: {error.strerror or error.__class__.__name__}") from error
    finally:
        for fd in reversed(descriptors):
            try:
                os.close(fd)
            except OSError:
                pass


def _open_directory_nofollow(path: Path) -> int:
    """Open an absolute directory one component at a time without following links."""
    flags = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _NONBLOCK
    current = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            following = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise
