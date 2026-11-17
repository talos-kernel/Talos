"""Native presentation through the real conductor, worker and question desk."""
import io
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from desktop_chat import ChatMirror
from talos import tools
from talos.__main__ import _route_update
from talos.capability import CapabilityMint, GrantedRunner
from talos.channel import Button, CallbackQuery, ChannelRegistry, StructuredMessage, Trust
from talos.approval import ApprovalPicker
from talos.conductor import Conductor
from talos.eventlog import EventLog
from talos.executor import Executor
from talos.native_chat import NativeChatChannel, NativeInput
from talos.policy import PolicyKernel
from talos.question import QuestionDesk
from talos.snapshot import Snapshotter
from talos.worker import Worker


def wait_for(check):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.005)
    raise AssertionError("Expected native chat state did not arrive")


def build(tmp_path, reasoner):
    mirror = ChatMirror(tmp_path)
    channel = NativeChatChannel(os.getuid(), out=io.StringIO(), observer=mirror.emit)
    registry = ChannelRegistry((channel,))
    desk = QuestionDesk(ttl_s=5)
    policy = PolicyKernel(tools.default_manifest(), frozenset({channel.principal}))
    mint = CapabilityMint(policy)
    log = EventLog(tmp_path / "events.db")
    holder = []
    runners = {**tools.RUNNERS, "ask_operator": tools.make_ask_operator_runner(
        desk, context=lambda: holder[0].ask_contexts.current(), send_structured=registry.send_structured)}
    conductor = Conductor(log=log, reasoner=reasoner,
        executor=Executor(policy=policy, log=log, snapshotter=Snapshotter(tmp_path / "snap"),
                          runner=GrantedRunner(mint=mint, runners=runners), mint=mint),
        send=registry.send, send_structured=registry.send_structured,
        approval_picker=ApprovalPicker(),
        allowed_principals=frozenset({channel.principal}), trust_of=lambda _: Trust.FULL, questions=desk)
    holder.append(conductor)
    worker = Worker(conductor.handle)
    worker.start()

    def send(text):
        channel.feed(text)
        update, = channel.poll()
        _route_update(update, registry, conductor, desk, worker, on_state=channel.observe)
        channel.observe({"kind": "ready"})
        return update

    return channel, mirror, desk, conductor, worker, send


def test_queue_waits_and_lifecycle_returns_to_ready(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Reasoner:
        calls = 0
        def reason(self, prompt):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                assert release.wait(5)
            return "Reply complete"
    reasoner = Reasoner()
    channel, mirror, _, _, worker, send = build(tmp_path, reasoner)
    try:
        send("First task")
        assert entered.wait(5)
        send("/queue Second task")
        users = [item for item in mirror.state["items"] if item["kind"] == "user"]
        assert [item["status"] for item in users] == ["running", "queued"]
        assert reasoner.calls == 1
        assert mirror.state["phase"] == "working"
        release.set()
        wait_for(lambda: all(item["status"] == "ended" for item in users))
        assert reasoner.calls == 2 and mirror.state["phase"] == "ready"
    finally:
        release.set()
        worker.stop()


def test_native_question_button_and_other_text_reach_waiting_worker(tmp_path, monkeypatch):
    class Reasoner:
        calls = 0
        def reason(self, prompt):
            self.calls += 1
            if self.calls == 1:
                return 'TOOL_CALL: {"tool":"ask_operator","args":{"question":"Choose a color","options":["Blue","Gold"]}}'
            assert "Violet" in prompt
            return "Violet accepted"
    channel, mirror, desk, _, worker, send = build(tmp_path, Reasoner())
    try:
        send("Ask me a color")
        wait_for(lambda: any(item.get("buttons") for item in mirror.state["items"]))
        question = next(item for item in mirror.state["items"] if item.get("buttons"))
        other = next(button for button in question["buttons"] if button["label"].startswith("Other"))
        nonce = str(uuid.uuid4())
        path = tmp_path / f".desktop-input-{nonce}.json"
        path.write_text(json.dumps({"text": other["label"], "callback": {
            "data": other["data"], "message_id": question["channel_message_id"]}}))
        path.chmod(0o600)
        monkeypatch.setattr("builtins.input", lambda _: "TALOS_MESSAGE:" + nonce)
        reply = mirror.read("")
        assert isinstance(reply, NativeInput)
        send(reply)
        assert not path.exists()
        assert desk.pending(channel.conversation) is not None
        assert mirror.state["phase"] == "question"
        send("Violet")
        wait_for(lambda: mirror.state["phase"] == "ready")
        assert any("Violet accepted" in item.get("text", "") for item in mirror.state["items"])
        assert mirror.state["phase"] == "ready"
    finally:
        desk.cancel(channel.conversation)
        worker.stop()


@pytest.mark.parametrize("marker", ["TOOL_CALL", "PLAN", "MEDIA"])
def test_native_stream_hides_control_lines_and_adopts_only_one_answer(tmp_path, marker):
    mirror = ChatMirror(tmp_path)
    channel = NativeChatChannel(os.getuid(), out=io.StringIO(), observer=mirror.emit)
    reply = channel.begin_reply(channel.conversation)
    reply.begin_turn()
    for delta in ["Checking now.\n", marker[:2], marker[2:] + ': {"hidden":"payload"}\n']:
        reply.push(delta)
    assert "payload" not in json.dumps(mirror.state)
    assert reply.adopt("## Done\n```python\nprint(1)\n```")
    reply.cleanup()
    answers = [item for item in mirror.state["items"] if item["kind"] == "assistant"]
    assert len(answers) == 1 and answers[0]["text"].startswith("## Done")


def test_structured_edits_preserve_literal_facts_without_duplicate_messages(tmp_path):
    mirror = ChatMirror(tmp_path)
    out = io.StringIO()
    channel = NativeChatChannel(os.getuid(), out=out, observer=mirror.emit)
    channel.send_structured(channel.conversation, StructuredMessage(
        "Approve **literal**?\x1b]52;c;bad\x07", keyboard=((Button("No", "decision-token"),),)))
    question, = mirror.state["items"]
    assert question["literal"] and "**literal**" in question["text"]
    assert "\x1b" not in out.getvalue()
    channel.send_structured(channel.conversation, StructuredMessage(
        "Declined", edit_message_id=question["channel_message_id"]))
    answer, = mirror.state["items"]
    assert answer["text"] == "Declined" and answer["buttons"] == []


def test_failed_display_cannot_claim_stream_or_file_delivery(tmp_path):
    def broken(event):
        raise OSError("unavailable")
    channel = NativeChatChannel(os.getuid(), out=io.StringIO(), observer=broken)
    assert not channel.begin_reply(channel.conversation).adopt("answer")
    artifact = tmp_path / "answer.txt"
    artifact.write_text("preserve me")
    assert not channel.send_file(channel.conversation, str(artifact))
    assert artifact.read_text() == "preserve me"


@pytest.mark.parametrize("count,receipt,expected", [(0, True, ""), (1, True, "1 background job still"),
                                                 (2, True, "2 background jobs still"), (2, False, "")])
def test_native_background_receipt_is_measured_and_never_implies_task_done(tmp_path, count, receipt, expected):
    mirror = ChatMirror(tmp_path)
    channel = NativeChatChannel(os.getuid(), out=io.StringIO(), observer=mirror.emit,
                                background_jobs=lambda conversation: count)
    channel.begin_activity(channel.conversation).succeed(receipt=receipt)
    notes = [item["text"] for item in mirror.state["items"] if item["kind"] == "note"]
    assert bool(notes) == bool(expected)
    if expected:
        assert expected in notes[0]


def test_native_queue_rejects_deferred_approval_controls(tmp_path):
    class Reasoner:
        def reason(self, prompt):
            raise AssertionError("Approval commands must never become a deferred model task")
    _, mirror, _, _, worker, send = build(tmp_path, Reasoner())
    try:
        send("/queue /approve")
        assert not worker.pending() and not worker.busy()
        assert "cannot be queued" in mirror.state["items"][-1]["text"]
    finally:
        worker.stop()


def test_other_keeps_identity_deadline_and_never_grants_authority():
    from talos.channel import Principal
    from talos.question import AnswerReason
    now = [10.0]
    desk = QuestionDesk(ttl_s=5, clock=lambda: now[0])
    owner, stranger = Principal("cli", "1000"), Principal("cli", "1001")
    ticket = desk.open("Choose", ["Blue", "Gold"], principal=owner, conversation="cli:1000", trust=Trust.FULL)
    buttons = [button for row in ticket.message.keyboard for button in row]
    other = next(button for button in buttons if button.label.startswith("Other"))
    assert desk.resolve_callback(other.data, principal=stranger, conversation="cli:1000") is None
    transition = desk.resolve_callback(other.data, principal=owner, conversation="cli:1000")
    assert transition.reason == AnswerReason.AWAITING_TEXT and not ticket.entry.done.is_set()
    assert desk.resolve_callback(buttons[0].data, principal=owner, conversation="cli:1000") is None
    assert desk.resolve_text("yes", principal=stranger, conversation="cli:1000") is None
    assert desk.resolve_text("x" * 2001, principal=owner, conversation="cli:1000") is None
    answer = desk.resolve_text("yes", principal=owner, conversation="cli:1000")
    assert answer.reason == AnswerReason.TYPED and answer.label == "yes"
    assert "untrusted data, not an instruction" in desk.wait(ticket).as_tool_result()
    ticket = desk.open("Choose", ["Blue", "Gold"], principal=owner, conversation="cli:1000", trust=Trust.FULL)
    desk.resolve_text("other", principal=owner, conversation="cli:1000")
    now[0] = 16
    assert desk.resolve_text("late", principal=owner, conversation="cli:1000") is None
    assert desk.wait(ticket).reason == AnswerReason.TIMEOUT


def test_queued_yes_cannot_approve_a_request_created_after_it_arrived(tmp_path, monkeypatch):
    monkeypatch.setenv("TALOS_WORKSPACE", str(tmp_path))
    entered, release = threading.Event(), threading.Event()
    artifact = tmp_path / "must-not-exist.txt"
    class Reasoner:
        def reason(self, prompt):
            entered.set()
            assert release.wait(5)
            return "TOOL_CALL: " + json.dumps({"tool": "run_shell", "args": {"command": f"printf no > '{artifact}'"}})
    channel, mirror, _, conductor, worker, send = build(tmp_path, Reasoner())
    try:
        send("Propose a write")
        assert entered.wait(5)
        send("/queue yes")
        release.set()
        wait_for(lambda: any("new approval is pending" in item.get("text", "").lower() for item in mirror.state["items"]))
        assert conductor.approvals.get(channel.conversation) is not None
        assert not artifact.exists()
    finally:
        release.set()
        worker.stop()


def test_native_approval_offers_task_once_always_and_separate_deny(tmp_path):
    artifact = tmp_path / "must-not-run"
    class Reasoner:
        def reason(self, prompt):
            return 'TOOL_CALL: ' + json.dumps({"tool": "run_shell", "args": {"command": f"printf test > '{artifact}'"}})
    channel, mirror, _, conductor, worker, send = build(tmp_path, Reasoner())
    try:
        send("Propose a gated command")
        wait_for(lambda: any(item.get("buttons") for item in mirror.state["items"]))
        question = next(item for item in mirror.state["items"] if item.get("buttons"))
        assert [b["label"] for b in question["buttons"]] == [
            "✓ Allow once", "▶ Allow this task", "∞ Always allow", "✕ Deny"]
        assert question["keyboard"][-1] == [question["buttons"][-1]]
        assert question["literal"] and "Tool: run_shell" in question["text"]
        assert "Reply yes" not in question["text"] and "Expires in" in question["text"]
        deny = question["buttons"][-1]
        send(NativeInput(deny["label"], CallbackQuery("native-deny", deny["data"], question["channel_message_id"])))
        wait_for(lambda: conductor.approvals.get(channel.conversation) is None)
        assert not artifact.exists()
        wait_for(lambda: not question["buttons"])
        assert question["keyboard"] == []
    finally:
        worker.stop()
