import importlib.util
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from desktop_state import history
from desktop_connections import choose_model, ollama, save_connection


def test_history_is_read_only_and_scoped_to_this_mac(tmp_path):
    from talos.transcript import TranscriptStore
    db = tmp_path / "data/transcript.db"
    store = TranscriptStore(db)
    identity = f"cli:{os.getuid()}"
    store.record(identity, asked="My question", answered="My answer", now=1)
    store.record("telegram:999", asked="Another channel", answered="Must not show", now=2)
    store.close()
    before = db.read_bytes()
    result = history(tmp_path)
    assert result["total"] == 1 and result["available"]
    assert result["turns"][0]["asked"] == "My question"
    assert "Must not show" not in str(result)
    assert db.read_bytes() == before


def test_history_distinguishes_missing_archive_from_broken_database(tmp_path):
    assert history(tmp_path) == {"turns": [], "total": 0, "available": True}
    assert not (tmp_path / "data").exists()
    (tmp_path / "data").mkdir()
    (tmp_path / "data/transcript.db").write_text("broken file")
    assert not history(tmp_path)["available"]


def test_history_bounds_output_without_losing_archive(tmp_path):
    from talos.transcript import TranscriptStore
    store = TranscriptStore(tmp_path / "data/transcript.db")
    for i in range(120):
        store.record(f"cli:{os.getuid()}", asked=f"question {i}", answered="answer", now=i)
    store.close()
    result = history(tmp_path)
    assert result["total"] == 120 and len(result["turns"]) == 100
    assert result["turns"][0]["asked"] == "question 119"


def test_ollama_offline_preserves_working_model(tmp_path, monkeypatch):
    import requests
    from talos.configcli import write_keys
    path = tmp_path / "talos.env"
    write_keys(path, {"TALOS_MODEL_PROVIDER": "claude-cli", "TALOS_MODEL": "working-model"})
    before = path.read_bytes()
    def unavailable(*args, **kwargs):
        raise requests.ConnectionError("offline")
    monkeypatch.setattr(requests, "get", unavailable)
    with pytest.raises(RuntimeError, match="Your model is unchanged"):
        ollama(tmp_path)
    assert path.read_bytes() == before


def test_connection_recipe_keeps_existing_telegram_identity(tmp_path):
    from talos.configcli import read_file, write_keys
    write_keys(tmp_path / "talos.env", {"TALOS_ALLOWED_PRINCIPALS": "telegram:123"})
    save_connection(tmp_path, {"TALOS_MODEL_PROVIDER": "ollama", "TALOS_MODEL": "local"})
    values = read_file(tmp_path / "talos.env")
    assert set(values["TALOS_ALLOWED_PRINCIPALS"].split(",")) == {"telegram:123", f"cli:{os.getuid()}"}
    from talos.eventlog import EventLog
    log = EventLog(tmp_path / "data/eventlog.db")
    assert log.recent(1, ("model.selected",))[0]["payload"]["model"] == "local"
    log.close()


def test_model_picker_rejects_terminal_control_characters(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "malicious\x1b]52;clipboard")
    with pytest.raises(RuntimeError, match="valid model"):
        choose_model(("local",))


def test_hermes_compatibility_preserves_security_flags():
    from desktop_hermes import compatible_args
    args = ["-z", "prompt", "--provider", "openai-codex", "--model", "example", "--reasoning", "high", "--ignore-rules"]
    assert compatible_args(args, "help --reasoning LEVEL") == args
    adapted = compatible_args(args, "help without effort option")
    assert adapted == ["-z", "prompt", "--provider", "openai-codex", "--model", "example", "--ignore-rules"]


@pytest.mark.parametrize("login_ok", [True, False])
def test_missing_codex_auth_opens_oauth_and_only_saves_after_reprobe(tmp_path, monkeypatch, login_ok):
    from types import SimpleNamespace
    import desktop_connections as connections
    import talos.reasoner
    from talos.configcli import read_file, write_keys
    write_keys(tmp_path / "talos.env", {"TALOS_MODEL_PROVIDER": "claude-cli", "TALOS_MODEL": "working-model"})
    monkeypatch.setattr(connections.shutil, "which", lambda name: "/example/hermes")
    monkeypatch.setattr(connections, "choose_model", lambda *args: "selected-codex")
    monkeypatch.setattr(connections, "hermes_wrapper", lambda *args: tmp_path / "wrapper")
    created = []
    class FakeReasoner:
        def __init__(self, *args, **kwargs): created.append(self)
        def validate(self):
            if len(created) == 1: raise RuntimeError("No Codex credentials stored")
    monkeypatch.setattr(talos.reasoner, "HermesCliReasoner", FakeReasoner)
    commands = []
    def login(args):
        commands.append(args)
        return SimpleNamespace(returncode=0 if login_ok else 1)
    monkeypatch.setattr(connections.subprocess, "run", login)
    if login_ok:
        connections.codex(tmp_path)
        assert len(created) == 2
        assert read_file(tmp_path / "talos.env")["TALOS_MODEL"] == "selected-codex"
    else:
        with pytest.raises(RuntimeError, match="current model is unchanged"):
            connections.codex(tmp_path)
        assert read_file(tmp_path / "talos.env")["TALOS_MODEL"] == "working-model"
    assert commands == [[str(tmp_path / "wrapper"), "auth", "add", "openai-codex", "--type", "oauth"]]
