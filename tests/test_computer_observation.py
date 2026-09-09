import json
from pathlib import PurePosixPath

import pytest

from talos.agent_loop import AgentStatus, run_agent
from talos.computer import observation
from talos.computer.contract import validate
from test_agent_loop import _executor, OWNER


def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("TALOS_COMPUTER_SOCKET", "/run/example/control.sock")
    root = tmp_path / "captures"
    root.mkdir()
    monkeypatch.setattr(observation, "CAPTURE_ROOT", PurePosixPath(root))
    target = root / ("screen-" + "a" * 32 + ".png")
    target.write_bytes(b"fixture")
    executor = _executor(tmp_path)
    calls = []
    def capture(req):
        calls.append("capture")
        return json.dumps({"image_path": str(target), "sha256": "a" * 64})
    def see(req):
        calls.append("vision")
        assert req.args == {"path": str(target), "question": "Read the visible total."}
        return "Visible total: 42."
    executor.runner.runners.update(computer_status=capture, see_image=see)
    proposal = 'TOOL_CALL: ' + json.dumps({"tool": "computer_status", "args": {
        "op": "screenshot", "question": "Read the visible total."}})
    return executor, calls, target, proposal


def test_capture_question_reaches_vision_before_next_model_call(tmp_path, monkeypatch):
    executor, calls, _, proposal = rig(tmp_path, monkeypatch)
    model_calls = []
    def propose(history):
        model_calls.append(list(history))
        if len(model_calls) == 1:
            return proposal
        assert calls == ["capture", "vision"]
        assert "Visible total: 42." in "\n".join(history)
        return "The visible total is 42."
    result = run_agent(propose, executor, OWNER, "observe")
    assert result.status is AgentStatus.ANSWERED
    assert len(model_calls) == 2
    assert calls == ["capture", "vision"]


def test_cancel_prevents_automatic_image_read(tmp_path, monkeypatch):
    executor, calls, _, proposal = rig(tmp_path, monkeypatch)
    result = run_agent(lambda history: proposal, executor, OWNER, "cancel",
                       should_stop=lambda: bool(calls))
    assert calls == ["capture"]
    assert "Stopped" in result.text


def test_automatic_image_read_consumes_step_budget(tmp_path, monkeypatch):
    executor, calls, _, proposal = rig(tmp_path, monkeypatch)
    result = run_agent(lambda history: proposal, executor, OWNER, "budget", max_steps=1)
    assert result.status is AgentStatus.STEP_LIMIT
    assert calls == ["capture"]


def test_operator_correction_reconsiders_pending_image_read(tmp_path, monkeypatch):
    from talos.redirect import Redirect
    executor, calls, _, proposal = rig(tmp_path, monkeypatch)
    redirect = Redirect()
    redirect.open("owner", "chat")
    capture = executor.runner.runners["computer_status"]
    def corrected_capture(req):
        result = capture(req)
        assert redirect.offer("owner", "chat", "Do not inspect this image; finish now.")
        return result
    executor.runner.runners["computer_status"] = corrected_capture
    def propose(history):
        if not history:
            return proposal
        assert "Do not inspect this image" in "\n".join(history)
        return "Stopped before image analysis."
    result = run_agent(propose, executor, OWNER, "corrected", redirect=redirect)
    assert result.status is AgentStatus.ANSWERED and calls == ["capture"]


def test_file_only_screenshot_does_not_trigger_image_analysis(tmp_path, monkeypatch):
    executor, calls, _, _ = rig(tmp_path, monkeypatch)
    turns = iter(['TOOL_CALL: {"tool":"computer_status","args":{"op":"screenshot"}}',
                  'The screenshot file is ready.'])
    result = run_agent(lambda history: next(turns), executor, OWNER, "file-only")
    assert result.status is AgentStatus.ANSWERED and calls == ["capture"]


def test_failed_vision_does_not_replay_capture_or_prior_action(tmp_path, monkeypatch):
    executor, calls, _, proposal = rig(tmp_path, monkeypatch)
    def click(req):
        calls.append("click")
        return json.dumps({"status":"queued", "job_id":"example"})
    def unavailable(req):
        calls.append("vision")
        raise RuntimeError("image provider unavailable")
    executor.runner.runners.update(computer_run=click, see_image=unavailable)
    # A reversible test write represents the already completed action: its receipt
    # cannot be repeated as a side effect of the automatic read or its failure.
    executor.runner.runners["write_file"] = click
    turns = iter(['TOOL_CALL: ' + json.dumps({"tool":"write_file", "args":{
        "path":str(tmp_path / "action-receipt"), "content":"checked"}}), proposal,
        "The image analysis failed; the action was not replayed."])
    result = run_agent(lambda history: next(turns), executor, OWNER, "vision-error")
    assert result.status is AgentStatus.ANSWERED
    assert calls == ["click", "capture", "vision"]
    assert "image provider unavailable" in "\n".join(result.history)


def test_automatic_image_read_cannot_bypass_secret_symlink_floor(tmp_path, monkeypatch):
    from talos import policy
    executor, calls, target, proposal = rig(tmp_path, monkeypatch)
    secret = tmp_path / ".secrets" / "private.png"
    secret.parent.mkdir(); secret.write_bytes(b"must not reach vision")
    monkeypatch.setattr(policy, "SECRET_PREFIXES", (*policy.SECRET_PREFIXES, str(secret.parent.resolve())))
    target.unlink(); target.symlink_to(secret)
    model_calls = []
    def propose(history):
        model_calls.append(True)
        return proposal if len(model_calls) == 1 else "The image read was denied."
    result = run_agent(propose, executor, OWNER, "secret")
    assert calls == ["capture"]
    assert "denied" in "\n".join(result.history)


@pytest.mark.parametrize("result", [None, "broken", "[]", {"image_path":"/etc/passwd"},
                                    {"image_path":"/var/lib/talos-computer-captures/../secret.png"}])
def test_untrusted_receipt_cannot_schedule_arbitrary_file_reads(result):
    assert observation.image_followup({"op":"screenshot","question":"Read"}, result) is None


@pytest.mark.parametrize("question", ["", "x" * 501, 1, None, "bad\x00question"])
def test_invalid_image_question_is_rejected(question):
    with pytest.raises(ValueError):
        validate({"op":"screenshot","question":question}, read=True)


def test_unchanged_observations_get_a_progress_warning():
    progress = observation.ObservationProgress()
    args = {"op":"screenshot", "question":"Read total"}
    receipt = {"sha256":"b" * 64}
    assert progress.record("computer_status", args, receipt) == ""
    assert "unchanged" in progress.record("computer_status", args, receipt)
    assert progress.record("computer_status", args | {"question":"Read date"}, receipt) == ""
    progress.record("computer_run", {"op":"scroll"}, {})
    assert progress.record("computer_status", args, receipt) == ""
