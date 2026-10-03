"""Visual desktop contract; real kernel grants, synthetic VM transport."""
import json
import threading
import time
from unittest.mock import Mock

import pytest

from talos.capability import CapabilityError, CapabilityMint, GrantedRunner
from talos.channel import Principal
from talos.computer.omarchy import OfflineDesktop, SIZE, key_event, text_keys
from talos.eventlog import EventLog
from talos.executor import Executor, Status
from talos.policy import PolicyKernel, ToolRequest
from talos.snapshot import Snapshotter
from talos.tools import default_manifest

OWNER = Principal("cli", "12345")
ACTION = {"op": "type", "project": "fixture", "key": "type-one", "title": "Type fixture", "text": "Talos"}


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    monkeypatch.setenv("TALOS_COMPUTER_SOCKET", str(tmp_path / "control.sock"))
    monkeypatch.setenv("TALOS_COMPUTER_ROOT", str(tmp_path / "target"))
    monkeypatch.setenv("TALOS_COMPUTER_AUTOAPPROVE", "0")
    qmp = Mock(return_value={"status": "running"})
    value = OfflineDesktop(root=tmp_path / "desk", owner=str(OWNER), qmp=qmp,
                           capture=Mock(), check_vm=Mock())
    value.control("agent", human=True)
    qmp.reset_mock()
    return value


@pytest.fixture
def executor(desktop, tmp_path):
    policy = PolicyKernel(default_manifest(), frozenset({OWNER}), shell_needs_human=False)
    mint = CapabilityMint(policy)
    runner = GrantedRunner(mint, {name: desktop.runner for name in ("computer_run", "computer_status")})
    return Executor(policy, EventLog(tmp_path / "events.db"), Snapshotter(tmp_path / "snapshots"), runner, mint)


def test_effect_needs_real_kernel_approval(executor, desktop):
    req = ToolRequest("computer_run", OWNER, ACTION)
    assert executor.run(req, "without-consent").status is Status.NEEDS_HUMAN
    desktop.qmp.assert_not_called()
    assert executor.run(req, "approved", human_approved=True).status is Status.DONE
    assert json.loads(executor.run(req, "duplicate", human_approved=True).result)["reused"]
    assert desktop.store.jobs(str(OWNER))[0]["state"] == "needs_review"


def test_grant_cannot_be_changed_or_replayed(executor, desktop):
    req = ToolRequest("computer_run", OWNER, ACTION)
    grant = executor.mint.issue(req, human_approved=True)
    with pytest.raises(CapabilityError):
        executor.runner(ToolRequest("computer_run", OWNER, ACTION | {"text": "other"}), grant)
    desktop.qmp.assert_not_called()
    fresh = executor.mint.issue(req, human_approved=True)
    executor.runner(req, fresh)
    count = desktop.qmp.call_count
    with pytest.raises(CapabilityError):
        executor.runner(req, fresh)
    assert desktop.qmp.call_count == count


@pytest.mark.parametrize("changes", [{"host": "other"}, {"socket": "/tmp/other.sock"}, {"op": "host_shell"}, {"x": True}])
def test_model_cannot_select_transport(executor, desktop, changes):
    assert executor.run(ToolRequest("computer_run", OWNER, ACTION | changes), "invalid", human_approved=True).status is Status.DENIED
    desktop.qmp.assert_not_called()


def test_stranger_never_reaches_vm(executor, desktop):
    stranger = Principal("cli", "67890")
    assert executor.run(ToolRequest("computer_run", stranger, ACTION), "stranger", human_approved=True).status is Status.DENIED
    desktop.qmp.assert_not_called()


@pytest.mark.parametrize("value", ["hello\nwhoami", "hello\tworld", "hello\x00world", "helloé", "", "a" * 2001])
def test_unsupported_text_has_zero_partial_input(desktop, value):
    with pytest.raises(ValueError):
        desktop.action(ACTION | {"text": value})
    desktop.qmp.assert_not_called()
    assert desktop.store.jobs(str(OWNER)) == []


def test_text_mapping_and_key_release(desktop):
    assert text_keys("aA! ") == [("a",), ("shift", "a"), ("shift", "1"), ("spc",)]
    result = desktop.action(ACTION | {"text": "A"})
    assert result["job"]["state"] == "needs_review"
    assert desktop.held == set()
    events = [call.args[1]["events"] for call in desktop.qmp.call_args_list]
    assert events == [
        [key_event("shift", True), key_event("a", True),
         key_event("a", False), key_event("shift", False)],
    ]


def test_printable_key_uses_short_press_and_settle(desktop, monkeypatch):
    sleeps = []
    clock = Mock(wraps=time)
    clock.sleep.side_effect = sleeps.append
    monkeypatch.setattr("talos.computer.omarchy.time", clock)
    desktop.action(ACTION | {"text": "e"})
    assert sleeps == [0.06]


def test_omarchy_terminal_shortcut_is_bounded_and_releases_super(desktop):
    args = {k: v for k, v in ACTION.items() if k != "text"}
    result = desktop.action(args | {"op": "key", "key": "open-terminal",
                                    "keys": "super+Return"})
    assert result["job"]["state"] == "needs_review"
    events = [call.args[1]["events"] for call in desktop.qmp.call_args_list]
    assert events == [
        [key_event("meta_l", True), key_event("ret", True)],
        [key_event("ret", False), key_event("meta_l", False)],
    ]
    assert not desktop.held


def test_omarchy_menu_shortcut_is_bounded(desktop):
    args = {k: v for k, v in ACTION.items() if k != "text"}
    desktop.action(args | {"op": "key", "key": "open-menu", "keys": "super+Space"})
    events = [call.args[1]["events"] for call in desktop.qmp.call_args_list]
    assert events == [
        [key_event("meta_l", True), key_event("spc", True)],
        [key_event("spc", False), key_event("meta_l", False)],
    ]


def test_return_is_one_longer_press_without_replay(desktop, monkeypatch):
    sleeps = []
    clock = Mock(wraps=time)
    clock.sleep.side_effect = sleeps.append
    monkeypatch.setattr("talos.computer.omarchy.time", clock)
    args = {k: v for k, v in ACTION.items() if k != "text"}
    desktop.action(args | {"op": "key", "key": "submit-once", "keys": "Return"})
    events = [call.args[1]["events"] for call in desktop.qmp.call_args_list]
    assert events == [[key_event("ret", True)], [key_event("ret", False)]]
    assert sleeps == [0.12, 0.20]


def test_special_key_up_failure_pauses_without_replaying_press(desktop):
    desktop.qmp.side_effect = [None, RuntimeError("key-up failed"), None, None]
    args = {k: v for k, v in ACTION.items() if k != "text"}
    result = desktop.action(args | {"op": "key", "key": "failed-up", "keys": "Return"})
    assert result["job"]["state"] == "interrupted"
    assert desktop.store.control() == "paused"
    event_calls = [call.args[1]["events"] for call in desktop.qmp.call_args_list
                   if call.args[0] == "input-send-event"]
    assert event_calls == [
        [key_event("ret", True)],
        [key_event("ret", False)],
        [key_event("ret", False)],
    ]
    assert sum(event["data"]["down"] for events in event_calls for event in events) == 1
    assert desktop.held == set()


def test_click_coordinates_are_guest_only(desktop):
    args = {k: v for k, v in ACTION.items() if k != "text"}
    desktop.action(args | {"op": "click", "x": SIZE[0] - 1, "y": 0})
    events = desktop.qmp.call_args_list[0].args[1]["events"]
    assert events[:2] == [{"type": "abs", "data": {"axis": "x", "value": 32767}},
                          {"type": "abs", "data": {"axis": "y", "value": 0}}]
    assert desktop.qmp.call_args.args[1]["events"][-1]["data"]["down"] is False


def test_repeat_returns_receipt_without_new_input(desktop):
    first = desktop.action(ACTION)
    desktop.qmp.reset_mock()
    assert desktop.action(ACTION)["job"]["id"] == first["job"]["id"]
    desktop.qmp.assert_not_called()
    with pytest.raises(ValueError, match="another request"):
        desktop.action(ACTION | {"text": "different"})


def test_submit_returns_queued_receipt_and_takeover_interrupts_worker(desktop, monkeypatch):
    entered = threading.Event()
    proceed = threading.Event()
    original = desktop._perform
    def delayed(*args, **kwargs):
        entered.set()
        assert proceed.wait(2)
        return original(*args, **kwargs)
    monkeypatch.setattr(desktop, "_perform", delayed)
    receipt = desktop.submit(ACTION | {"key": "async-type"})
    assert receipt["job"]["state"] == "queued"
    assert entered.wait(2)
    desktop.control("human", human=True)
    proceed.set()
    for _ in range(100):
        job = desktop.store.get(str(OWNER), receipt["job"]["id"])
        if job["state"] == "interrupted":
            break
        threading.Event().wait(0.01)
    assert job["state"] == "interrupted"
    assert desktop.store.control() == "human"


def test_human_input_requires_takeover_and_creates_no_model_job(desktop):
    with pytest.raises(RuntimeError, match="interrupted"):
        desktop.human_input({"op": "click", "x": 10, "y": 20})
    desktop.qmp.reset_mock()
    desktop.control("human", human=True)
    desktop.qmp.reset_mock()
    assert desktop.human_input({"op": "type", "text": "A"}) == {
        "input": "delivered", "control": "human"}
    assert desktop.store.jobs(str(OWNER)) == []
    assert desktop.held == set()
    assert desktop.qmp.called


def test_capture_can_be_separated_from_private_state(tmp_path):
    state = tmp_path / "state"
    captures = tmp_path / "captures"
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" +
           SIZE[0].to_bytes(4, "big") + SIZE[1].to_bytes(4, "big") +
           b"\x08\x02\x00\x00\x00fixture")
    value = OfflineDesktop(root=state, capture_root=captures, capture_mode=0o640,
                           owner=str(OWNER), qmp=Mock(return_value={"status": "running"}),
                           capture=Mock(return_value=png), check_vm=Mock())
    receipt = value.read({"op": "screenshot"})
    path = value.capture_root / receipt["image_path"].rsplit("/", 1)[-1]
    assert path.parent == captures
    assert path.stat().st_mode & 0o777 == 0o640
    assert not list(state.glob("screen-*.png"))


def test_failed_isolation_preflight_has_no_input_or_job(desktop):
    desktop.check_vm.side_effect = ValueError("NIC appeared")
    with pytest.raises(ValueError):
        desktop.action(ACTION)
    desktop.qmp.assert_not_called()
    assert desktop.store.jobs(str(OWNER)) == []


def test_takeover_interrupts_typing_and_preserves_human_control(desktop, monkeypatch):
    entered = threading.Event()
    proceed = threading.Event()
    def pause(_):
        entered.set()
        assert proceed.wait(2)
    monkeypatch.setattr("talos.computer.omarchy.time.sleep", pause)
    thread = threading.Thread(target=desktop.action, args=(ACTION | {"text": "abc"},))
    thread.start()
    assert entered.wait(2)
    def assert_released_before_preflight():
        assert not desktop.held
    desktop.check_vm.side_effect = assert_released_before_preflight
    desktop.control("human", human=True)
    count = desktop.qmp.call_count
    proceed.set()
    thread.join(2)
    assert not thread.is_alive()
    assert desktop.qmp.call_count == count
    assert desktop.store.control() == "human"
    assert desktop.store.jobs(str(OWNER))[0]["state"] == "interrupted"
    with pytest.raises(ValueError, match="only the operator"):
        desktop.action({"op": "resume"})


def test_restart_does_not_replay_uncertain_input(desktop):
    job, _ = desktop.store.begin(str(OWNER), ACTION)
    desktop.store.finish(job["id"], "running")
    restarted = OfflineDesktop(root=desktop.root, owner=str(OWNER), qmp=Mock(),
                               capture=Mock(), check_vm=Mock())
    assert restarted.store.control() == "paused"
    restarted.control("agent", human=True)
    restarted.qmp.reset_mock()
    assert restarted.action(ACTION)["job"]["state"] == "interrupted"
    restarted.qmp.assert_not_called()


@pytest.mark.parametrize("op", ["exec", "browser", "files", "routines"])
def test_unsupported_surfaces_do_not_fall_back_to_host(desktop, op):
    args = {"op": op}
    if op == "exec":
        args |= {k: v for k, v in ACTION.items() if k not in {"op", "text"}}
        args["command"] = "true"
    with pytest.raises(ValueError):
        (desktop.read if op in {"files", "routines"} else desktop.action)(args)
    desktop.qmp.assert_not_called()


def test_capture_refuses_missing_or_wrong_geometry(desktop):
    desktop.capture_source.return_value = b"not a screenshot"
    with pytest.raises(ValueError, match="guest-only"):
        desktop.read({"op": "screenshot"})
    assert list(desktop.root.glob("screen-*.png")) == []
