import io
import json
import os
from pathlib import Path
import sys
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from desktop_chat import ChatMirror
from talos.chatcli import ChatChannel
from talos.agent_loop import AgentProgress, ProgressStage


def test_live_chat_preserves_markdown_and_measures_tools(tmp_path):
    mirror = ChatMirror(tmp_path)
    channel = ChatChannel(os.getuid(), out=io.StringIO(), observer=mirror.emit)
    channel.feed("Check the version")
    channel.poll()
    activity = channel.begin_activity(channel.conversation)
    activity.progress(AgentProgress(ProgressStage.TOOL, tool="run_shell", status="running", preview="python --version"))
    activity.progress(AgentProgress(ProgressStage.RESULT, tool="run_shell", status="done"))
    channel.send(channel.conversation, "**Ready**\n```python\nprint(1)\n```")
    activity.succeed()
    channel.observe({"kind": "ready"})
    state = json.loads(mirror.path.read_text())
    assert state["phase"] == "ready"
    assert [item["kind"] for item in state["items"]] == ["user", "tool", "assistant"]
    assert state["items"][1]["status"] == "done"
    assert state["items"][1]["duration"] >= 0
    assert state["items"][2]["text"].startswith("**Ready**\n```python\n")
    assert mirror.path.stat().st_mode & 0o777 == 0o600


def test_waiting_approval_is_visible_and_never_consumed(tmp_path):
    mirror = ChatMirror(tmp_path)
    channel = ChatChannel(os.getuid(), out=io.StringIO(), observer=mirror.emit)
    activity = channel.begin_activity(channel.conversation)
    channel.send(channel.conversation, "Needs your approval: write_file. Reply yes or no.")
    activity.succeed(receipt=False)
    channel.observe({"kind": "ready"})
    assert json.loads(mirror.path.read_text())["phase"] == "approval"
    assert channel.poll() == []  # The display never feeds a decision to the kernel.
    channel.feed("no")
    assert channel.poll()[0].text == "no"
    assert json.loads(mirror.path.read_text())["phase"] == "working"


def test_display_failure_does_not_break_reply_or_terminal(tmp_path):
    def broken(event):
        raise OSError("disk unavailable")
    out = io.StringIO()
    channel = ChatChannel(os.getuid(), out=out, observer=broken)
    channel.send(channel.conversation, "Still visible")
    assert out.getvalue() == "Still visible\n"


def test_multiline_message_is_one_turn_beyond_terminal_line_limit(tmp_path, monkeypatch):
    nonce = str(uuid.uuid4())
    path = tmp_path / f".desktop-input-{nonce}.json"
    message = "First line\nSecond line\n" + "long input " * 600 + "\n/help is quoted text"
    path.write_text(json.dumps({"text": message}))
    path.chmod(0o600)
    monkeypatch.setattr("builtins.input", lambda prompt: "TALOS_MESSAGE:" + nonce)
    mirror = ChatMirror(tmp_path)
    channel = ChatChannel(os.getuid(), observer=mirror.emit)
    channel.feed(mirror.read("prompt"))
    turns = channel.poll()
    assert len(turns) == 1 and turns[0].text == message
    assert not path.exists()


@pytest.mark.parametrize("invalid", ["symlink", "public", "oversize", "wrong_type"])
def test_desktop_message_rejects_unsafe_payload(tmp_path, monkeypatch, invalid):
    nonce = str(uuid.uuid4())
    path = tmp_path / f".desktop-input-{nonce}.json"
    path.write_text(json.dumps({"text": "ok" if invalid != "oversize" else "x" * 20001}))
    path.chmod(0o600)
    if invalid == "public":
        path.chmod(0o644)
    if invalid == "wrong_type":
        path.write_text('{"text": ["not", "a", "message"]}')
    if invalid == "symlink":
        other = tmp_path / "other"
        path.rename(other)
        path.symlink_to(other)
    monkeypatch.setattr("builtins.input", lambda prompt: "TALOS_MESSAGE:" + nonce)
    with pytest.raises((RuntimeError, OSError)):
        ChatMirror(tmp_path).read("prompt")
