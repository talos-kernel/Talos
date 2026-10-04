"""Focused checks for deadline and fail-closed E2E script helpers."""
import importlib.util
import io
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


local_e2e = load_script("omarchy_local_e2e")
network_e2e = load_script("omarchy_network_e2e")
installed_e2e = load_script("omarchy_installed_e2e")


def test_client_call_applies_caller_rpc_timeout(monkeypatch):
    class Connection:
        def __init__(self):
            self.timeout = None

        def __enter__(self):
            return self

        def __exit__(self, *unused):
            pass

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, endpoint):
            assert endpoint == "/private/control.sock"

        def sendall(self, frame):
            assert b'"owner": "owner"' in frame

        def makefile(self, mode):
            assert mode == "rb"
            return io.BytesIO(b'{"status": "ok"}\n')

    connection = Connection()
    monkeypatch.setattr(network_e2e.socket, "socket", lambda *args: connection)

    result = network_e2e.Client("/private/control.sock", "owner").call(
        "read", {"op": "status"}, timeout=0.375)

    assert result == {"status": "ok"}
    assert connection.timeout == 0.375


def test_client_action_uses_one_deadline_and_rejects_late_completion(monkeypatch):
    clock = iter((10.0, 10.1, 10.2, 10.3, 10.4, 14.9, 15.001))
    monkeypatch.setattr(network_e2e.time, "monotonic", lambda: next(clock))
    sleeps = []
    monkeypatch.setattr(network_e2e.time, "sleep", sleeps.append)
    calls = []
    states = iter((
        {"state": "running"},
        {"state": "needs_review"},
    ))
    client = network_e2e.Client("/private/control.sock", "owner")

    def call(kind, args, *, timeout=20):
        calls.append((kind, args, timeout))
        if kind == "action":
            return {"job": {"id": "job-1"}}
        return next(states)

    monkeypatch.setattr(client, "call", call)

    with pytest.raises(TimeoutError, match="desktop input did not settle"):
        client.action({"op": "key", "keys": "Return"}, timeout=5)

    assert calls == [
        ("action", {"op": "key", "keys": "Return"}, pytest.approx(4.9)),
        ("read", {"op": "job", "job_id": "job-1"}, pytest.approx(4.7)),
        ("read", {"op": "job", "job_id": "job-1"}, pytest.approx(0.1)),
    ]
    assert sleeps == [pytest.approx(0.2)]


def test_local_wait_job_rejects_terminal_evidence_after_deadline(monkeypatch):
    clock = iter((10.0, 10.1, 25.001))
    monkeypatch.setattr(local_e2e.time, "monotonic", lambda: next(clock))
    computer = SimpleNamespace(
        desktop=SimpleNamespace(
            read=lambda args: {"id": args["job_id"], "state": "needs_review"}
        )
    )

    with pytest.raises(TimeoutError, match="durable terminal state"):
        local_e2e.wait_job(computer, "job-1", timeout=15.0)


def test_capture_and_ocr_use_remaining_budget_and_reject_late_evidence(monkeypatch):
    clock = iter((65.0, 70.0, 100.001))
    monkeypatch.setattr(network_e2e.time, "monotonic", lambda: next(clock))
    calls = []

    class Client:
        def call(self, kind, args, *, timeout):
            calls.append((kind, args, timeout))
            return {"image_path": "/tmp/frame.png"}

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "TALOSPROBEEND1234", "")

    monkeypatch.setattr(network_e2e.subprocess, "run", run)

    with pytest.raises(TimeoutError, match="deadline expired"):
        network_e2e.capture_and_recognize(
            Client(), Path("/tmp/vision.swift"), deadline=100.0)

    assert calls[0][2] == pytest.approx(35.0)
    assert calls[1][1]["timeout"] == pytest.approx(30.0)


def test_desktop_readiness_rejects_late_framebuffer_evidence(monkeypatch, tmp_path):
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"frame")
    clock = iter((10.0, 10.1, 44.8, 44.9, 45.001))
    monkeypatch.setattr(installed_e2e.time, "monotonic", lambda: next(clock))
    visible = iter((False, True))
    monkeypatch.setattr(
        installed_e2e, "framebuffer_visible", lambda payload: next(visible))
    calls = []
    waits = []

    def rpc(kind, args, *, timeout):
        calls.append((kind, args, timeout))
        return {"image_path": str(frame)}

    with pytest.raises(TimeoutError, match="visible desktop"):
        installed_e2e.wait_for_visible_desktop(rpc, waits.append, timeout=35)

    assert calls == [
        ("read", {"op": "screenshot", "question": "installed desktop readiness"},
         pytest.approx(34.9)),
        ("read", {"op": "screenshot", "question": "installed desktop readiness"},
         pytest.approx(0.1)),
    ]
    assert waits == [pytest.approx(0.2)]


def test_semantic_capture_uses_frame_time_and_rejects_a_late_frame(
        monkeypatch, tmp_path):
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"frame")
    calls = []

    def rpc(kind, args, *, timeout):
        calls.append((kind, args, timeout))
        return {"image_path": str(frame)}

    clock = iter((10.0, 14.9))
    monkeypatch.setattr(installed_e2e.time, "monotonic", lambda: next(clock))
    captured, captured_at = installed_e2e.capture_semantic_frame(
        rpc, "visible command", 15.0)
    assert captured == frame and captured_at == 14.9
    assert calls[-1][2] == pytest.approx(5.0)

    clock = iter((20.0, 25.001))
    monkeypatch.setattr(installed_e2e.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError, match="after its deadline"):
        installed_e2e.capture_semantic_frame(rpc, "late command", 25.0)


def test_failure_cleanup_reopens_trusted_workbench_before_input_and_pause(monkeypatch):
    events = []

    class Page:
        url = "about:blank"
        state = "Computer paused"

        def goto(self, link, *, wait_until):
            events.append(("goto", link, wait_until))
            self.url = "https://workbench.invalid/"

        def evaluate(self, expression):
            events.append(("evaluate", expression))

        def locator(self, selector):
            page = self

            class Locator:
                def inner_text(self):
                    return page.state

                def click(self):
                    events.append(("click", selector))
                    if selector == "#takeover":
                        page.state = "You have control"
                    elif selector == "#pause":
                        page.state = "Computer paused"

            return Locator()

        def wait_for_timeout(self, milliseconds):
            events.append(("wait", milliseconds))

    page = Page()

    class Browser:
        def new_page(self, *, viewport):
            events.append(("new_page", viewport))
            return page

        def close(self):
            events.append(("close",))

    class Playwright:
        class Chromium:
            def launch(self, *, headless, executable_path):
                events.append(("launch", headless, executable_path))
                return Browser()

        chromium = Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *unused):
            pass

    class Expectation:
        def __init__(self, locator):
            self.locator = locator

        def to_be_visible(self, *, timeout):
            events.append(("visible", timeout))

        def to_have_text(self, text, *, timeout):
            assert self.locator.inner_text() == text
            events.append(("text", text, timeout))

    inputs = (
        {"op": "key", "keys": "ctrl+c"},
        {"op": "key", "keys": "super+W"},
    )
    monkeypatch.setattr(installed_e2e, "sync_playwright", Playwright)
    monkeypatch.setattr(installed_e2e, "expect", Expectation)

    def dispatch(_page, value):
        events.append(("input", value))
        return {"status": 200, "body": {
            "input": "dispatched", "control": "human"}}

    monkeypatch.setattr(installed_e2e, "browser_input", dispatch)

    receipts = installed_e2e.recover_failure_with_workbench(
        "https://workbench.invalid/#token=test", Path("/browser"), inputs)

    assert [item["request"] for item in receipts] == list(inputs)
    assert ("click", "#takeover") in events
    assert ("wait", 1800) in events
    assert events[-2:] == [
        ("text", "Computer paused", 10000),
        ("close",),
    ]


def test_local_pause_failure_is_not_swallowed_and_status_is_read_back():
    class Desktop:
        def control(self, state, *, human):
            assert (state, human) == ("paused", True)
            raise RuntimeError("pause RPC failed")

    class Computer:
        desktop = Desktop()
        status_reads = 0

        def status(self):
            self.status_reads += 1
            return {"control": "paused", "vm": "paused"}

    computer = Computer()
    with pytest.raises(RuntimeError, match="final pause could not be proven"):
        local_e2e.leave_paused(computer)
    assert computer.status_reads == 1


@pytest.mark.parametrize(
    ("status", "message"),
    [
        ({"control": "agent", "vm": "paused"}, "control=paused"),
        ({"control": "paused", "vm": "running"}, "vm=paused"),
    ],
)
def test_local_pause_requires_both_independent_readbacks(status, message):
    computer = SimpleNamespace(
        desktop=SimpleNamespace(control=lambda state, human: None),
        status=lambda: status,
    )
    with pytest.raises(RuntimeError, match=message):
        local_e2e.leave_paused(computer)


def test_network_pause_failure_is_not_swallowed_and_status_is_read_back():
    class Client:
        status_reads = 0

        def action(self, args):
            assert args == {"op": "pause"}
            raise RuntimeError("pause RPC failed")

        def call(self, kind, args):
            assert (kind, args) == ("read", {"op": "status"})
            self.status_reads += 1
            return {"control": "paused", "vm": "paused"}

    client = Client()
    with pytest.raises(RuntimeError, match="final pause could not be proven"):
        network_e2e.leave_paused(client)
    assert client.status_reads == 1


@pytest.mark.parametrize(
    "status",
    [
        {"control": "human", "vm": "paused"},
        {"control": "paused", "vm": "running"},
    ],
)
def test_network_pause_requires_both_independent_readbacks(status):
    client = SimpleNamespace(
        action=lambda args: None,
        call=lambda kind, args: status,
    )
    with pytest.raises(RuntimeError, match="control=paused and vm=paused"):
        network_e2e.leave_paused(client)
