"""Apply reviewed agent imports to an inactive, recoverable Talos staging area.

The dry-run plan is the authority for *what may be copied*, never for permission.  A
human repeats its content-derived fingerprint, the command refuses Talos' own sandbox,
and every accepted byte is read again through the importer's no-follow boundary before
an atomically published staging directory appears.  Nothing here activates profile
instructions, schedules, permissions, credentials, plugins or MCP servers.

One separately confirmed skill may be activated through the existing managed-skill
writer.  That reuses its validation, secret rejection, atomic publication and history
contract instead of inventing a softer second writer.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from types import SimpleNamespace
import uuid

from . import skills
from .import_agents import (
    ImportPlanError,
    PLAN_VERSION,
    STATUS_CONFLICT,
    STATUS_REJECTED,
    STATUS_SKIPPED,
    STATUS_STAGED,
    STAGING_ROOT,
    _open_directory_nofollow,
    _read_portable,
    _safe_root,
    plan_import,
)

MANIFEST = "import-manifest.json"
MANIFEST_VERSION = 1
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_FOLLOW_UP_STATES = {
    "secrets": "reauth-required",
    "authority": "approval-required",
    "executables": "disabled-review",
    "schedules": "disabled-review",
    "sessions": "archive-only",
}


class ImportApplyError(ValueError):
    """Nothing was published, unless the message explicitly says otherwise."""


def apply_import(
    source: str | os.PathLike[str],
    target: str | os.PathLike[str],
    *,
    source_type: str,
    confirmation: str,
) -> dict[str, object]:
    """Copy one unchanged reviewed plan into inactive staging, atomically."""
    source_root = _safe_root(source, "source", must_exist=True)
    target_root = _private_profile(target)
    plan = plan_import(source_root, target_root, source_type=source_type)
    fingerprint = plan.fingerprint()
    if not _SHA256.fullmatch(confirmation) or confirmation != fingerprint:
        raise ImportApplyError("confirmation does not match the current dry-run plan")
    blocked = [item for item in plan.items if item.status in {STATUS_REJECTED, STATUS_CONFLICT}]
    if blocked:
        raise ImportApplyError(
            f"current plan contains {len(blocked)} rejected or conflicting item(s); nothing was copied"
        )
    staged = [item for item in plan.items if item.status == STATUS_STAGED]
    if not staged:
        raise ImportApplyError("current plan contains no portable items")

    root_fd = _open_directory_nofollow(target_root)
    temp_name = f".{source_type}.{uuid.uuid4().hex}.tmp"
    temp_path = target_root / STAGING_ROOT.parent / STAGING_ROOT.name / temp_name
    try:
        data_fd = _open_or_create_dir(root_fd, "data")
        try:
            staging_fd = _open_or_create_dir(data_fd, "import-staging")
            try:
                fcntl.flock(staging_fd, fcntl.LOCK_EX)
                if _exists(staging_fd, source_type):
                    raise ImportApplyError(
                        f"inactive {source_type} import already exists; roll it back before replacing it"
                    )
                os.mkdir(temp_name, 0o700, dir_fd=staging_fd)
                temp_fd = os.open(temp_name, _DIR_FLAGS, dir_fd=staging_fd)
                try:
                    records: list[dict[str, object]] = []
                    prefix = (STAGING_ROOT / source_type).parts
                    for item in staged:
                        target_parts = Path(item.target).parts
                        if target_parts[: len(prefix)] != prefix:
                            raise ImportApplyError("plan target escaped inactive staging")
                        relative = target_parts[len(prefix):]
                        if not relative or not all(_SEGMENT.fullmatch(part) for part in relative):
                            raise ImportApplyError("plan target contains an unsafe path segment")
                        data = _read_portable(source_root, source_root / item.source)
                        if len(data) != item.bytes or hashlib.sha256(data).hexdigest() != item.sha256:
                            raise ImportApplyError("source changed after preview; run the dry-run again")
                        _write_tree_file(temp_fd, relative, data)
                        records.append({
                            "category": item.category,
                            "source": item.source,
                            "target": "/".join(relative),
                            "bytes": item.bytes,
                            "sha256": item.sha256,
                            "active": False,
                        })
                    plan_payload = plan.to_dict()
                    expected_records, follow_up = _expected_manifest(plan_payload, source_type)
                    if records != expected_records:
                        raise ImportApplyError("current plan could not be bound to its manifest")
                    manifest = {
                        "version": MANIFEST_VERSION,
                        "source_type": source_type,
                        "fingerprint": fingerprint,
                        "state": "inactive",
                        "plan": plan_payload,
                        "items": records,
                        "follow_up": follow_up,
                    }
                    _write_file(
                        temp_fd,
                        MANIFEST,
                        (json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode(),
                    )
                    os.fsync(temp_fd)
                finally:
                    os.close(temp_fd)
                if _exists(staging_fd, source_type):
                    raise ImportApplyError("staging destination appeared concurrently; nothing was replaced")
                os.rename(temp_name, source_type, src_dir_fd=staging_fd, dst_dir_fd=staging_fd)
                os.fsync(staging_fd)
            except BaseException:
                with contextlib.suppress(OSError):
                    shutil.rmtree(temp_path)
                raise
            finally:
                os.close(staging_fd)
        finally:
            os.close(data_fd)
    finally:
        os.close(root_fd)
    return {
        "ok": True,
        "source_type": source_type,
        "fingerprint": fingerprint,
        "copied": len(staged),
        "state": "inactive",
        "next": "review staged files; activate skills individually; re-authenticate connections",
    }


def rollback_import(
    target: str | os.PathLike[str], *, source_type: str, confirmation: str
) -> dict[str, object]:
    """Move inactive staging into a private recovery archive; delete nothing."""
    target_root = _private_profile(target)
    manifest = read_manifest(target_root, source_type)
    fingerprint = manifest.get("fingerprint")
    if not isinstance(fingerprint, str) or confirmation != fingerprint:
        raise ImportApplyError("confirmation does not match the staged import")
    root_fd = _open_directory_nofollow(target_root)
    try:
        data_fd = _open_existing_dir(root_fd, "data")
        try:
            staging_fd = _open_existing_dir(data_fd, "import-staging")
            backups_fd = _open_or_create_dir(data_fd, "import-backups")
            try:
                fcntl.flock(staging_fd, fcntl.LOCK_EX)
                archive = f"{source_type}-{fingerprint[:12]}"
                if _exists(backups_fd, archive):
                    raise ImportApplyError("recovery archive already exists; nothing was moved")
                os.rename(source_type, archive, src_dir_fd=staging_fd, dst_dir_fd=backups_fd)
                os.fsync(staging_fd)
                os.fsync(backups_fd)
            finally:
                os.close(backups_fd)
                os.close(staging_fd)
        finally:
            os.close(data_fd)
    finally:
        os.close(root_fd)
    return {"ok": True, "source_type": source_type, "fingerprint": fingerprint,
            "state": "rolled-back", "archive": f"data/import-backups/{source_type}-{fingerprint[:12]}"}


def activate_imported_skill(
    target: str | os.PathLike[str], *, source_type: str, name: str, confirmation: str
) -> dict[str, object]:
    """Activate exactly one reviewed skill through Talos' managed-skill writer."""
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name or "") or len(name) > 40:
        raise ImportApplyError("skill name is invalid")
    target_root = _private_profile(target)
    manifest = read_manifest(target_root, source_type)
    fingerprint = manifest.get("fingerprint")
    if not isinstance(fingerprint, str) or confirmation != fingerprint:
        raise ImportApplyError("confirmation does not match the staged import")
    relative = f"skills/{name}/{skills.SKILL_FILENAME}"
    record = next((item for item in manifest.get("items", [])
                   if isinstance(item, dict) and item.get("target") == relative), None)
    if record is None:
        raise ImportApplyError("that skill is not present in the staged import")
    data = _read_staged(target_root, source_type, relative)
    digest = hashlib.sha256(data).hexdigest()
    if digest != record.get("sha256"):
        raise ImportApplyError("staged skill no longer matches its manifest")
    try:
        text = data.decode("utf-8")
        head, body = skills._split_frontmatter(text)
        fields, _metadata = skills._parse_frontmatter(head)
        skills._validated_identity(fields, name)
    except (UnicodeDecodeError, skills.SkillError):
        raise ImportApplyError("staged skill is no longer a valid Agent Skill") from None
    from .skillmanage import make_skill_manage_runner

    request = SimpleNamespace(args={
        "action": "create",
        "name": name,
        "path": skills.SKILL_FILENAME,
        "expected_sha256": "absent",
        "description": fields["description"],
        "body": body.strip(),
    })
    try:
        result = json.loads(make_skill_manage_runner(target_root / "skills")(request))
    except (ValueError, OSError) as error:
        raise ImportApplyError(f"skill activation refused: {error}") from None
    return {"ok": True, "source_type": source_type, "fingerprint": fingerprint,
            "skill": name, "active": True, "sha256": result.get("expected_sha256")}


def read_manifest(target: Path, source_type: str) -> dict[str, object]:
    if source_type not in {"hermes", "openclaw"}:
        raise ImportApplyError("unknown import source type")
    data = _read_staged(target, source_type, MANIFEST)
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ImportApplyError("staged import manifest is invalid") from None
    if (not isinstance(payload, dict) or payload.get("version") != MANIFEST_VERSION
            or payload.get("source_type") != source_type or payload.get("state") != "inactive"
            or not isinstance(payload.get("items"), list)
            or not isinstance(payload.get("plan"), dict)):
        raise ImportApplyError("staged import manifest is invalid")
    plan_payload = payload["plan"]
    encoded = json.dumps(
        plan_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    fingerprint = hashlib.sha256(encoded).hexdigest()
    if (payload.get("fingerprint") != fingerprint
            or plan_payload.get("version") != PLAN_VERSION
            or plan_payload.get("source_type") != source_type):
        raise ImportApplyError("staged import manifest no longer matches its confirmed plan")
    expected_records, expected_follow_up = _expected_manifest(plan_payload, source_type)
    if payload["items"] != expected_records or payload.get("follow_up") != expected_follow_up:
        raise ImportApplyError("staged import manifest no longer matches its confirmed plan")
    return payload


def _expected_manifest(
    plan_payload: dict[str, object], source_type: str
) -> tuple[list[dict[str, object]], dict[str, dict[str, str]]]:
    rows = plan_payload.get("items")
    if not isinstance(rows, list):
        raise ImportApplyError("staged import manifest is invalid")
    prefix = f"{STAGING_ROOT.as_posix()}/{source_type}/"
    records: list[dict[str, object]] = []
    follow_up: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ImportApplyError("staged import manifest is invalid")
        status = row.get("status")
        if status == STATUS_STAGED:
            category, source, target = row.get("category"), row.get("source"), row.get("target")
            size, digest = row.get("bytes"), row.get("sha256")
            if (not isinstance(category, str) or not isinstance(source, str)
                    or not isinstance(target, str) or not target.startswith(prefix)
                    or not isinstance(size, int) or isinstance(size, bool) or size < 0
                    or not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
                raise ImportApplyError("staged import manifest is invalid")
            relative = target[len(prefix):]
            parts = Path(relative).parts
            if (not parts or not all(_SEGMENT.fullmatch(part) for part in parts)
                    or "/".join(parts) != relative):
                raise ImportApplyError("staged import manifest is invalid")
            records.append({
                "category": category,
                "source": source,
                "target": relative,
                "bytes": size,
                "sha256": digest,
                "active": False,
            })
        elif status == STATUS_SKIPPED:
            category, reason = row.get("category"), row.get("reason")
            if category not in _FOLLOW_UP_STATES or not isinstance(reason, str):
                raise ImportApplyError("staged import manifest is invalid")
            follow_up[category] = {
                "state": _FOLLOW_UP_STATES[category],
                "reason": reason,
            }
        elif status not in {STATUS_CONFLICT, STATUS_REJECTED}:
            raise ImportApplyError("staged import manifest is invalid")
    return records, follow_up


def _read_staged(target: Path, source_type: str, relative: str) -> bytes:
    root = target / STAGING_ROOT / source_type
    try:
        return _read_portable(root, root / relative)
    except ImportPlanError as error:
        raise ImportApplyError(f"staged import refused: {error}") from None


def _private_profile(raw: str | os.PathLike[str]) -> Path:
    root = _safe_root(raw, "target", must_exist=True)
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise ImportApplyError(
            "target profile must be owned by this user and not group/world writable"
        )
    return root


def _open_or_create_dir(parent: int, name: str) -> int:
    if not _SEGMENT.fullmatch(name):
        raise ImportApplyError("unsafe directory name")
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    return _open_existing_dir(parent, name)


def _open_existing_dir(parent: int, name: str) -> int:
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
    except OSError:
        raise ImportApplyError(f"{name} is missing, unsafe, or not a directory") from None
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        os.close(fd)
        raise ImportApplyError(f"{name} must be private and owned by this user")
    return fd


def _write_tree_file(root_fd: int, parts: tuple[str, ...], data: bytes) -> None:
    opened: list[int] = []
    parent = root_fd
    try:
        for part in parts[:-1]:
            parent = _open_or_create_dir(parent, part)
            opened.append(parent)
        _write_file(parent, parts[-1], data)
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _write_file(parent: int, name: str, data: bytes) -> None:
    if not _SEGMENT.fullmatch(name):
        raise ImportApplyError("unsafe file name")
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=parent)
    except OSError as error:
        raise ImportApplyError(f"cannot stage file: {error.strerror or error.__class__.__name__}") from None
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ImportApplyError("short write while staging file")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _exists(parent: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
