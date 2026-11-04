from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat

import pytest

from talos import connections
from talos.sandbox import MARKER


def _profile(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def test_disabled_connection_draft_is_atomic_private_and_visible(tmp_path: Path) -> None:
    profile = _profile(tmp_path / "talos")
    result = connections.create_draft(
        profile,
        name="calendar-reader",
        command="/usr/local/bin/calendar-mcp",
        args=("--read-only",),
        description="Read-only calendar connector",
        confirmation="DISABLED",
    )

    path = profile / "data/connection-drafts.json"
    payload = json.loads(path.read_text())
    assert result["enabled"] is False and payload["connections"][0]["enabled"] is False
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    listing = connections.inventory(profile)
    assert listing["disabled_drafts"][0]["name"] == "calendar-reader"
    assert listing["active_worker_connections"] == []
    assert "/usr/local/bin" not in json.dumps(listing)


def test_connection_draft_rejects_secrets_duplicates_and_relative_commands(tmp_path: Path) -> None:
    profile = _profile(tmp_path / "talos")
    with pytest.raises(connections.ConnectionError, match="absolute"):
        connections.create_draft(
            profile, name="reader", command="reader", description="Reader",
            confirmation="DISABLED",
        )
    with pytest.raises(connections.ConnectionError, match="credential"):
        connections.create_draft(
            profile, name="reader", command="/usr/bin/reader",
            args=("sk-" + "AbCd1234EfGh5678IjKl9012",), description="Reader",
            confirmation="DISABLED",
        )
    connections.create_draft(
        profile, name="reader", command="/usr/bin/reader", description="Reader",
        confirmation="DISABLED",
    )
    with pytest.raises(connections.ConnectionError, match="already exists"):
        connections.create_draft(
            profile, name="reader", command="/usr/bin/reader", description="Reader",
            confirmation="DISABLED",
        )


def test_inventory_reports_active_registry_without_command_or_arguments(tmp_path: Path) -> None:
    profile = _profile(tmp_path / "talos")
    data = profile / "data"
    data.mkdir(mode=0o700)
    registry = data / "mcp-servers.json"
    registry.write_text(json.dumps({
        "version": 1,
        "servers": [{
            "name": "files",
            "command": "/usr/local/bin/private-command",
            "args": ["/private/path"],
            "description": "Bounded files",
        }],
    }))

    listing = connections.inventory(profile)

    assert listing["active_worker_connections"] == [
        {"name": "files", "description": "Bounded files", "scope": "isolated-worker"}
    ]
    assert "private-command" not in json.dumps(listing) and "/private/path" not in json.dumps(listing)


def test_connections_cli_lists_and_refuses_sandboxed_mutation(tmp_path: Path, monkeypatch) -> None:
    profile = _profile(tmp_path / "talos")
    output = io.StringIO()
    assert connections.run_connections(["list"], out=output, profile=profile) == 0
    assert "Talos Connections" in output.getvalue() and "none" in output.getvalue()

    monkeypatch.setenv(MARKER, "1")
    result = connections.run_connections([
        "draft", "--name", "reader", "--command", "/usr/bin/reader",
        "--description", "Reader", "--confirm", "DISABLED",
    ], out=output, profile=profile)
    assert result == 3 and not (profile / "data/connection-drafts.json").exists()
