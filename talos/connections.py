"""Talos Connections — public, operator-owned connector inventory and drafts.

This is deliberately not a marketplace and it ships no private service.  Active MCP
servers remain the existing two-gate worker registry.  A draft records only a disabled,
secret-free executable description; it cannot run until the operator separately installs
the executable and names it in both agent and worker allowlists.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

from .mcpservers import MAX_ARGS, MAX_ARG_CHARS, SERVER_NAME, McpServerRegistry
from .recall import looks_secret
from .import_agents import _open_directory_nofollow

DRAFT_VERSION = 1
MAX_DRAFTS = 32
MAX_DESCRIPTION = 240
MAX_FILE_BYTES = 32 * 1024
DRAFT_FILE = Path("data") / "connection-drafts.json"
ACTIVE_FILE = Path("data") / "mcp-servers.json"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class ConnectionError(ValueError):
    pass


def inventory(profile: str | os.PathLike[str]) -> dict[str, object]:
    root = _profile_root(profile)
    active = McpServerRegistry.from_path(root / ACTIVE_FILE)
    drafts = _read_drafts(root)
    return {
        "version": 1,
        "active_worker_connections": [
            {"name": item.name, "description": item.description, "scope": "isolated-worker"}
            for item in active.servers
        ],
        "disabled_drafts": [
            {"name": item["name"], "description": item["description"],
             "fingerprint": _fingerprint(item), "scope": "disabled"}
            for item in drafts
        ],
        "note": "Connections never grant permission; agent and worker allowlists must both opt in.",
    }


def create_draft(
    profile: str | os.PathLike[str], *, name: str, command: str,
    args: tuple[str, ...] = (), description: str = "", confirmation: str,
) -> dict[str, object]:
    """Atomically add a disabled, secret-free stdio connector description."""
    if confirmation != "DISABLED":
        raise ConnectionError("draft confirmation must be exactly DISABLED")
    if not SERVER_NAME.fullmatch(name or ""):
        raise ConnectionError("connection name must match [a-z0-9-]{1,32}")
    if not os.path.isabs(command) or len(command) > 400:
        raise ConnectionError("connection command must be an absolute path of at most 400 characters")
    if len(args) > MAX_ARGS or any(not isinstance(arg, str) or len(arg) > MAX_ARG_CHARS for arg in args):
        raise ConnectionError("connection arguments exceed their bounds")
    description = " ".join(description.split())
    if not 1 <= len(description) <= MAX_DESCRIPTION:
        raise ConnectionError(f"description must be 1..{MAX_DESCRIPTION} characters")
    candidate = {"name": name, "command": command, "args": list(args),
                 "description": description, "enabled": False}
    if looks_secret(json.dumps(candidate, sort_keys=True)):
        raise ConnectionError("connection draft looks like it contains a credential")
    # Reuse the execution-registry parser as a second schema check. It deliberately
    # ignores `enabled`; validate the executable shape before storing the inert draft.
    parsed = McpServerRegistry.from_mapping({"version": 1, "servers": [
        {key: value for key, value in candidate.items() if key != "enabled"}
    ]})
    if parsed.get(name) is None:
        raise ConnectionError("connection draft is not a valid worker connector")

    root = _profile_root(profile)
    with _draft_lock(root):
        drafts = _read_drafts(root)
        if any(item["name"] == name for item in drafts):
            raise ConnectionError("a draft with that name already exists")
        if len(drafts) >= MAX_DRAFTS:
            raise ConnectionError(f"connection draft limit of {MAX_DRAFTS} reached")
        drafts.append(candidate)
        payload = {
            "version": DRAFT_VERSION,
            "connections": sorted(drafts, key=lambda item: item["name"]),
        }
        _atomic_json(root, DRAFT_FILE, payload)
    return {"ok": True, "name": name, "enabled": False,
            "fingerprint": _fingerprint(candidate),
            "next": "install the executable, then opt in through both operator allowlists"}


def run_connections(args: list[str] | None = None, *, out=None, profile=None) -> int:
    argv = list(args or [])
    write = (out or sys.stdout).write
    usage = (
        "  usage: talos connections list\n"
        "         talos connections draft --name <name> --command <absolute-path> "
        "--description <text> [--arg <value> ...] --confirm DISABLED\n"
    )
    if argv in (["--help"], ["-h"]):
        write(usage + "  Drafts are inert; secrets and private services are never bundled.\n")
        return 0
    from .paths import PROFILE_DIR
    root = Path(profile) if profile is not None else PROFILE_DIR
    if argv == ["list"]:
        try:
            data = inventory(root)
        except (ConnectionError, OSError) as error:
            write(f"  connections inventory refused: {error}\n")
            return 1
        write("\nTalos Connections\n")
        for item in data["active_worker_connections"]:
            write(f"  active    {_display(item['name'])} — {_display(item['description'])}\n")
        for item in data["disabled_drafts"]:
            write(f"  disabled  {_display(item['name'])} — {_display(item['description'])}\n")
        if not data["active_worker_connections"] and not data["disabled_drafts"]:
            write("  none\n")
        write("\n  Connections grant no permission; both operator allowlists must opt in.\n\n")
        return 0
    if not argv or argv[0] != "draft":
        write(usage)
        return 2
    values: dict[str, str] = {}
    connector_args: list[str] = []
    index = 1
    while index < len(argv):
        key = argv[index]
        if key == "--arg" and index + 1 < len(argv):
            connector_args.append(argv[index + 1])
            index += 2
            continue
        if key in {"--name", "--command", "--description", "--confirm"} and index + 1 < len(argv):
            if key in values:
                write(f"  repeated option: {key}\n{usage}")
                return 2
            values[key] = argv[index + 1]
            index += 2
            continue
        write(f"  unknown or incomplete option: {key}\n{usage}")
        return 2
    if set(values) != {"--name", "--command", "--description", "--confirm"}:
        write(usage)
        return 2
    from .askcli import refuse_in_sandbox
    if refuse_in_sandbox():
        write("  refused: connection drafts cannot be created from the agent sandbox\n")
        return 3
    try:
        result = create_draft(
            root, name=values["--name"], command=values["--command"],
            description=values["--description"], args=tuple(connector_args),
            confirmation=values["--confirm"],
        )
    except (ConnectionError, OSError) as error:
        write(f"  connection draft refused: {error}\n")
        return 1
    write(f"\n  disabled connection draft created: {_display(result['name'])}\n"
          f"  fingerprint: {result['fingerprint']}\n\n")
    return 0


def _read_drafts(root: Path) -> list[dict[str, object]]:
    root_fd = data_fd = file_fd = None
    try:
        root_fd = _open_directory_nofollow(root)
        data_fd = os.open("data", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW,
                          dir_fd=root_fd)
        file_fd = os.open(DRAFT_FILE.name, os.O_RDONLY | _NOFOLLOW, dir_fd=data_fd)
        info = os.fstat(file_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or info.st_size > MAX_FILE_BYTES
                or stat.S_IMODE(info.st_mode) & 0o077):
            return []
        with os.fdopen(file_fd, "r", encoding="utf-8") as handle:
            file_fd = None
            payload = json.load(handle)
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return []
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if data_fd is not None:
            os.close(data_fd)
        if root_fd is not None:
            os.close(root_fd)
    rows = payload.get("connections") if isinstance(payload, dict) and payload.get("version") == 1 else None
    if not isinstance(rows, list):
        return []
    clean: list[dict[str, object]] = []
    for item in rows[:MAX_DRAFTS]:
        if not isinstance(item, dict) or item.get("enabled") is not False:
            continue
        name, command, arguments, description = (
            item.get("name"), item.get("command"), item.get("args"), item.get("description")
        )
        if (not isinstance(name, str) or not SERVER_NAME.fullmatch(name)
                or not isinstance(command, str) or not os.path.isabs(command)
                or not isinstance(arguments, list)
                or len(arguments) > MAX_ARGS
                or not all(isinstance(arg, str) and len(arg) <= MAX_ARG_CHARS for arg in arguments)
                or not isinstance(description, str) or not 1 <= len(description) <= MAX_DESCRIPTION):
            continue
        candidate = {"name": name, "command": command, "args": arguments,
                     "description": description, "enabled": False}
        if looks_secret(json.dumps(candidate, sort_keys=True)):
            continue
        clean.append(candidate)
    return clean


def _profile_root(raw: str | os.PathLike[str]) -> Path:
    root = Path(raw)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ConnectionError("profile must be an existing absolute directory without symlinks")
    info = root.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise ConnectionError("profile must be owned by this user and not group/world writable")
    try:
        descriptor = _open_directory_nofollow(root)
    except OSError:
        raise ConnectionError("profile path contains a symlink or unsafe component") from None
    os.close(descriptor)
    return root


@contextlib.contextmanager
def _draft_lock(root: Path):
    """Serialize the read/append/replace transaction so concurrent CLIs lose no draft."""
    root_fd = _open_directory_nofollow(root)
    try:
        try:
            os.mkdir("data", 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        try:
            data_fd = os.open(
                "data", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW,
                dir_fd=root_fd,
            )
        except OSError:
            raise ConnectionError("connections data directory is unsafe") from None
    finally:
        os.close(root_fd)
    try:
        info = os.fstat(data_fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ConnectionError("connections data directory must be private and owned by this user")
        fcntl.flock(data_fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(data_fd)


def _atomic_json(root: Path, relative: Path, payload: dict[str, object]) -> None:
    if relative != DRAFT_FILE:
        raise ConnectionError("connection draft target is invalid")
    root_fd = _open_directory_nofollow(root)
    try:
        try:
            os.mkdir("data", 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        try:
            data_fd = os.open(
                "data", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW,
                dir_fd=root_fd,
            )
        except OSError:
            raise ConnectionError("connections data directory is unsafe") from None
    finally:
        os.close(root_fd)
    info = os.fstat(data_fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        os.close(data_fd)
        raise ConnectionError("connections data directory must be private and owned by this user")
    try:
        try:
            existing = os.stat(relative.name, dir_fd=data_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
            or existing.st_uid != os.getuid() or stat.S_IMODE(existing.st_mode) & 0o077
        ):
            raise ConnectionError("connection drafts file is unsafe")
        temp = f".{relative.name}.{uuid.uuid4().hex}.tmp"
        data = (json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()
        try:
            fd = os.open(
                temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600,
                dir_fd=data_fd,
            )
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, relative.name, src_dir_fd=data_fd, dst_dir_fd=data_fd)
            os.fsync(data_fd)
        finally:
            try:
                os.unlink(temp, dir_fd=data_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(data_fd)


def _fingerprint(item: dict[str, object]) -> str:
    data = json.dumps(item, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def _display(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=True)


__all__ = ["ConnectionError", "create_draft", "inventory", "run_connections"]
