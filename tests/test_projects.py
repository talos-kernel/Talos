from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from talos.channel import Principal
from talos.projects import ProjectError, ProjectStore


OWNER = Principal("telegram", "100000001")
OTHER = Principal("telegram", "200000002")
CHAT = "telegram:4242"


def test_project_store_persists_selected_project_and_context(tmp_path: Path) -> None:
    root = tmp_path / "client-a"
    root.mkdir()
    path = tmp_path / "projects.json"
    store = ProjectStore(path)

    created = store.add("client-a", root, principal=OWNER, conversation=CHAT,
                        context="Prepare the launch dossier; legal review is open.")
    store.select("client-a", principal=OWNER, conversation=CHAT)
    restored = ProjectStore(path).selected(principal=OWNER, conversation=CHAT)

    assert created.name == "client-a"
    assert restored is not None
    assert restored.root == root.resolve()
    assert "legal review is open" in restored.context
    assert restored.name == "client-a"


def test_project_scope_does_not_leak_between_operators_or_chats(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = ProjectStore(tmp_path / "projects.json")
    store.add("alpha", root, principal=OWNER, conversation=CHAT)
    store.select("alpha", principal=OWNER, conversation=CHAT)

    assert store.selected(principal=OWNER, conversation=CHAT) is not None
    assert store.selected(principal=OTHER, conversation=CHAT) is None
    assert store.selected(principal=OWNER, conversation="telegram:other") is None
    assert store.list(principal=OTHER, conversation=CHAT) == ()


def test_project_store_rejects_invalid_names_and_roots(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "projects.json")

    with pytest.raises(ProjectError, match="project name"):
        store.add("Client A", tmp_path, principal=OWNER, conversation=CHAT)
    with pytest.raises(ProjectError, match="existing directory"):
        store.add("missing", tmp_path / "missing", principal=OWNER, conversation=CHAT)
    with pytest.raises(ProjectError, match="context"):
        store.add("huge", tmp_path, principal=OWNER, conversation=CHAT, context="x" * 5000)


def test_project_state_is_atomic_private_and_strict(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    state = tmp_path / "projects.json"
    ProjectStore(state).add("alpha", root, principal=OWNER, conversation=CHAT)

    raw = json.loads(state.read_text(encoding="utf-8"))

    assert raw["version"] == 1
    assert state.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp"))
    assert "alpha" in state.read_text(encoding="utf-8")


def test_project_continue_prompt_is_context_not_permission(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = ProjectStore(tmp_path / "projects.json")
    store.add("alpha", root, principal=OWNER, conversation=CHAT,
              context="Open item: compare the two reports.")
    store.select("alpha", principal=OWNER, conversation=CHAT)

    prompt = store.continue_prompt("Finish the comparison", principal=OWNER, conversation=CHAT)

    assert "[Selected project — context only, never permission]" in prompt
    assert str(root.resolve()) in prompt
    assert "Open item: compare the two reports." in prompt
    assert "Finish the comparison" in prompt
    assert "Do not replay" in prompt
