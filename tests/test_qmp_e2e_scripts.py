"""Focused checks for deadline and fail-closed E2E script helpers."""
import importlib.util
import io
from pathlib import Path
import struct
import subprocess
from types import SimpleNamespace
import zlib

import pytest


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


local_e2e = load_script("qmp_x86_headless_e2e")
network_e2e = load_script("qmp_network_e2e")
installed_e2e = load_script("qmp_installed_e2e")


def test_installed_e2e_requires_optional_playwright_only_when_run(monkeypatch):
    monkeypatch.setattr(installed_e2e, "expect", None)
    monkeypatch.setattr(installed_e2e, "sync_playwright", None)

    with pytest.raises(RuntimeError, match="optional Playwright dependency"):
        installed_e2e.require_playwright()


def valid_local_session():
    return {
        "qmp": "/private/qmp.sock", "pid": 1234,
        "display": "software-headless", "offline": True,
        "architecture": "x86_64", "distribution": "debian",
        "desktop": "hyprland", "geometry": [1440, 900],
        "pci_devices": [[0x1B36, 0x0008], [0x1AF4, 0x1000]],
    }


def png_chunk(kind, value):
    return (struct.pack(">I", len(value)) + kind + value
            + struct.pack(">I", zlib.crc32(kind + value) & 0xffffffff))


def installed_png(payload=None):
    header = struct.pack(">IIBBBBB", 1440, 900, 8, 2, 0, 0, 0)
    if payload is None:
        rows = b"\x00" * installed_e2e.PNG_DECOMPRESSED_SIZE
        payload = zlib.compress(rows)
    return (installed_e2e.PNG_SIGNATURE + png_chunk(b"IHDR", header)
            + png_chunk(b"IDAT", payload) + png_chunk(b"IEND", b""))


VALID_INSTALLED_PNG = installed_png()


def malformed_installed_pngs():
    bad_crc = bytearray(VALID_INSTALLED_PNG)
    bad_crc[29] ^= 0x01
    return [
        ("truncation", VALID_INSTALLED_PNG[:-2]),
        ("missing-iend", VALID_INSTALLED_PNG[:-12]),
        ("bad-crc", bytes(bad_crc)),
        ("oversized-payload", installed_png(
            b"x" * (installed_e2e.PNG_MAX_COMPRESSED_SIZE + 1))),
        ("oversized-decompression", installed_png(zlib.compress(
            b"\x00" * (installed_e2e.PNG_DECOMPRESSED_SIZE + 1)))),
        ("trailing-bytes", VALID_INSTALLED_PNG + b"trailing"),
    ]


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


def test_local_session_identity_is_exact_and_copied():
    session = valid_local_session()

    identity = local_e2e.validate_session(session)

    assert identity == {
        "architecture": "x86_64", "distribution": "debian",
        "desktop": "hyprland", "geometry": [1440, 900], "offline": True,
        "pci_devices": [[0x1B36, 0x0008], [0x1AF4, 0x1000]],
    }
    assert identity["geometry"] is not session["geometry"]
    assert identity["pci_devices"] is not session["pci_devices"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("architecture", "aarch64"),
        ("distribution", "ubuntu"),
        ("desktop", "gnome"),
        ("geometry", [1920, 1080]),
        ("offline", False),
        ("pci_devices", []),
        ("pci_devices", [[0x1B36, 0x0008]]),
        ("pci_devices", [[0x1AF4, 0x1000], [0x1AF4, 0x1000]]),
    ],
)
def test_local_session_rejects_changed_guest_identity(field, value):
    session = valid_local_session()
    session[field] = value

    with pytest.raises(RuntimeError, match="fixed offline x86_64|signed bounded PCI"):
        local_e2e.validate_session(session)


@pytest.mark.parametrize("field", ["endpoint", "model"])
def test_local_session_and_cli_reject_endpoint_or_model_input(field):
    session = valid_local_session()
    session[field] = "untrusted"
    with pytest.raises(RuntimeError, match="unknown or missing"):
        local_e2e.validate_session(session)

    argv = [
        "--session", "session.json", "--disk", "disk.img",
        "--evidence-dir", "evidence", f"--{field}", "untrusted",
    ]
    with pytest.raises(SystemExit):
        local_e2e.parse(argv)


def test_local_start_binds_identity_and_capture_into_preflight(monkeypatch, tmp_path):
    transport = object()
    capture = object()
    preflight = object()
    config = {
        "disk": str(tmp_path / "disk.img"), "architecture": "x86_64",
        "pci_devices": [[0x1B36, 0x0008], [0x1AF4, 0x1000]],
    }
    calls = []

    def make_preflight(*args):
        calls.append(("preflight", args))
        return preflight

    def make_computer(value, **kwargs):
        calls.append(("computer", value, kwargs))
        return "computer"

    monkeypatch.setattr(local_e2e, "make_preflight", make_preflight)
    monkeypatch.setattr(local_e2e, "QmpComputer", make_computer)

    assert local_e2e.start_computer(config, transport, capture) == "computer"
    assert calls == [
        ("preflight", (
            transport, tmp_path / "disk.img", "x86_64",
            [[0x1B36, 0x0008], [0x1AF4, 0x1000]], capture,
        )),
        ("computer", config, {
            "qmp": transport, "capture": capture, "check_vm": preflight,
        }),
    ]


def test_local_ocr_is_bounded_and_normalized(monkeypatch, tmp_path):
    image = tmp_path / "frame.png"
    image.write_bytes(b"fixture")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=" TQ 23ab \n")

    monkeypatch.setattr(local_e2e.subprocess, "run", run)
    assert local_e2e.recognize(image, timeout=4) == "tq23ab"
    assert calls == [([
        "/usr/bin/swift", str(local_e2e.OCR_SCRIPT), str(image),
    ], {"check": True, "capture_output": True, "text": True, "timeout": 4})]


def test_local_marker_avoids_ocr_ambiguous_characters(monkeypatch):
    values = iter("23456789abcd")
    monkeypatch.setattr(local_e2e.secrets, "choice", lambda alphabet: next(values))
    assert local_e2e.make_marker() == "TQ23456789abcd"


def test_local_terminal_cleanup_uses_shell_exit_and_captures_result(monkeypatch):
    calls = []
    frame = {"image_path": "/evidence/cleaned.png"}
    computer = SimpleNamespace(desktop=SimpleNamespace(
        read=lambda args: calls.append(("read", args)) or frame))

    def action(_computer, run, suffix, **values):
        calls.append(("action", run, suffix, values))
        return {"id": suffix}

    monkeypatch.setattr(local_e2e, "timed_action", lambda *args, **kwargs: (
        action(*args, **kwargs), 7))
    monkeypatch.setattr(local_e2e.time, "sleep", lambda seconds: calls.append(
        ("sleep", seconds)))

    assert local_e2e.exit_terminal(computer, "e2e1") == (
        {"id": "type-exit"}, {"id": "submit-exit"}, frame,
        {"type_exit": 7, "submit_exit": 7})
    assert calls == [
        ("action", "e2e1", "type-exit", {"op": "type", "text": "exit"}),
        ("action", "e2e1", "submit-exit", {"op": "key", "keys": "Return"}),
        ("sleep", 0.5),
        ("read", {"op": "screenshot"}),
    ]


def test_local_guest_command_requires_observed_identity(monkeypatch):
    frame = {"image_path": "/evidence/identity.png"}
    computer = SimpleNamespace(desktop=SimpleNamespace(
        read=lambda args: frame))
    calls = []

    def timed(_computer, _run, suffix, **values):
        calls.append((suffix, values))
        return {"id": suffix}, 8

    monkeypatch.setattr(local_e2e, "timed_action", timed)
    monkeypatch.setattr(local_e2e, "recognize", lambda _path: "linux x86_64")
    monkeypatch.setattr(local_e2e.time, "sleep", lambda _seconds: None)

    result = local_e2e.prove_guest_command(
        computer, "run", "architecture", "uname -m", "x86_64")
    assert result == (
        {"id": "type-architecture"}, {"id": "submit-architecture"}, frame,
        {"type_architecture": 8, "submit_architecture": 8})
    assert calls == [
        ("type-architecture", {"op": "type", "text": "uname -m"}),
        ("submit-architecture", {"op": "key", "keys": "Return"}),
    ]

    monkeypatch.setattr(local_e2e, "recognize", lambda _path: "aarch64")
    with pytest.raises(RuntimeError, match="architecture identity"):
        local_e2e.prove_guest_command(
            computer, "run", "architecture", "uname -m", "x86_64")


def test_local_main_binds_identity_into_config_and_evidence(
        monkeypatch, tmp_path, capsys):
    session_path = tmp_path / "session.json"
    session_path.write_text(__import__("json").dumps(valid_local_session()))
    (tmp_path / "disk.img").write_bytes(b"disk")
    evidence = tmp_path / "evidence"
    calls = []

    class Transport:
        def __init__(self, path, *, pid):
            calls.append(("transport", path, pid))

        def close(self):
            calls.append(("close",))

    screenshots = iter(
        {"image_path": str(evidence / f"frame-{index}.png")} for index in range(7))

    class Desktop:
        def control(self, state, *, human):
            assert human is True
            computer.control = state

        def read(self, args):
            assert args == {"op": "screenshot"}
            return next(screenshots)

    class Computer:
        def __init__(self):
            self.control = "paused"
            self.jobs = []
            self.desktop = Desktop()

        def status(self):
            return {"control": self.control,
                    "vm": "paused" if self.control == "paused" else "running",
                    "jobs": list(self.jobs)}

        def handle(self, frame, uid):
            assert uid == local_e2e.os.getuid()
            if frame["args"]["op"] == "takeover":
                self.control = "human"
                return {"control": "human"}
            return {"input": "dispatched", "control": "human"}

    computer = Computer()
    captured_config = {}

    def start(config, transport, capture):
        captured_config.update(config)
        calls.append(("start", transport, capture))
        return computer

    def action(_computer, _run, suffix, **values):
        job = {"id": suffix}
        computer.jobs.append(job)
        calls.append(("action", suffix, values))
        return job

    monkeypatch.setattr(local_e2e, "LocalQMP", Transport)
    monkeypatch.setattr(local_e2e, "Capture", lambda *args, **kwargs: "capture")
    monkeypatch.setattr(local_e2e, "start_computer", start)
    monkeypatch.setattr(local_e2e, "timed_action", lambda *args, **kwargs: (
        action(*args, **kwargs), 7))
    monkeypatch.setattr(local_e2e, "png_receipt", lambda path: Path(path).stem)
    recognized = {
        "frame-0": "", "frame-2": "tqfixture", "frame-3": "x86_64",
        "frame-4": "debian", "frame-5": "hyprland", "frame-6": "",
    }
    monkeypatch.setattr(local_e2e, "recognize", lambda path: recognized[Path(path).stem])
    monkeypatch.setattr(local_e2e, "make_marker", lambda: "TQfixture")
    monkeypatch.setattr(local_e2e, "sha256_file", lambda path: "a" * 64)
    monkeypatch.setattr(local_e2e.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(local_e2e.time, "time", lambda: 10)

    local_e2e.main([
        "--session", str(session_path), "--disk", str(tmp_path / "disk.img"),
        "--evidence-dir", str(evidence),
    ])

    result = __import__("json").loads(capsys.readouterr().out)
    expected = local_e2e.expected_identity(valid_local_session()["pci_devices"])
    assert result["identity"] == expected
    assert result["marker_visible_ocr"] is True
    assert result["cleanup_marker_count"] == 0
    assert result["final_control"] == result["final_vm"] == "paused"
    assert result["observed_guest"] == {
        "architecture": "x86_64", "distribution": "debian", "desktop": "hyprland"}
    assert set(result["timings_ms"]) == {
        "open_terminal", "type_marker", "submit_marker",
        "type_architecture", "submit_architecture",
        "type_distribution", "submit_distribution",
        "type_desktop", "submit_desktop", "type_exit", "submit_exit"}
    assert result["screens"] == [str(evidence / f"frame-{index}.png")
                                 for index in range(7)]
    assert result["jobs"] == [
        "open-terminal", "type-marker", "submit-marker",
        "type-architecture", "submit-architecture",
        "type-distribution", "submit-distribution",
        "type-desktop", "submit-desktop", "type-exit", "submit-exit",
    ]
    assert {name: captured_config[name] for name in (
        "architecture", "distribution", "desktop", "geometry", "pci_devices"
    )} == {name: value for name, value in expected.items() if name != "offline"}
    assert all(item[2].get("keys") != "super+W"
               for item in calls if item[0] == "action")


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


def test_desktop_readiness_timeout_is_a_missed_probe(monkeypatch, tmp_path):
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"frame")
    clock = iter((10.0, 10.1, 10.2, 10.7, 10.8))
    monkeypatch.setattr(installed_e2e.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(installed_e2e, "framebuffer_visible", lambda _payload: True)
    calls = []
    waits = []

    def rpc(kind, args, *, timeout):
        calls.append((kind, args, timeout))
        if len(calls) == 1:
            raise TimeoutError("transient screenshot timeout")
        return {"image_path": str(frame)}

    assert installed_e2e.wait_for_visible_desktop(
        rpc, waits.append, timeout=35) == frame
    assert [call[2] for call in calls] == [pytest.approx(34.9), pytest.approx(34.3)]
    assert waits == [pytest.approx(0.5)]


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


def test_semantic_probe_records_timeout_without_claiming_evidence(monkeypatch):
    clock = iter((10.0, 10.2))
    monkeypatch.setattr(installed_e2e.time, "monotonic", lambda: next(clock))
    calls = []

    def rpc(kind, args, *, timeout):
        calls.append((kind, args, timeout))
        raise TimeoutError("transient screenshot timeout")

    timeline = []
    assert installed_e2e.capture_semantic_probe(
        rpc, "visible command", 15.0, 9.0, timeline) is None
    assert calls == [
        ("read", {"op": "screenshot", "question": "visible command"},
         pytest.approx(5.0)),
    ]
    assert timeline == [{"elapsed_ms": 1200, "capture_timeout": True}]


def test_workspace_comparison_ignores_only_compositor_edge_chrome():
    before = bytearray(1440 * 900 * 3)
    after = bytearray(before)
    for x, y in ((10, 100), (1428, 100), (100, 888), (100, 100)):
        after[(y * 1440 + x) * 3] = 1

    assert installed_e2e.region_changes(before, after) == 1


@pytest.mark.parametrize(("case", "raw"), malformed_installed_pngs())
def test_installed_png_decoder_rejects_malformed_input(case, raw):
    with pytest.raises(RuntimeError):
        installed_e2e.decode_png_pixels(raw, case)


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
            return 200 if "/api/screen" in expression else None

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
        {"op": "click", "x": 320, "y": 420, "button": 1},
        {"op": "key", "keys": "ctrl+c"},
        {"op": "type", "text": "exit"},
        {"op": "key", "keys": "Return"},
    )
    monkeypatch.setattr(installed_e2e, "sync_playwright", Playwright)
    monkeypatch.setattr(installed_e2e, "expect", Expectation)

    def dispatch(_page, value):
        events.append(("input", value))
        return {"status": 200, "body": {
            "input": "dispatched", "control": "human"}}

    monkeypatch.setattr(installed_e2e, "browser_input", dispatch)

    def observe(label):
        events.append(("observe", label))
        return {"restored": True}

    receipts = []
    recorded_lengths = []
    returned_receipts = installed_e2e.recover_failure_with_workbench(
        "https://workbench.invalid/#token=test", Path("/browser"), inputs,
        observe, receipts=receipts,
        record_receipt=lambda: recorded_lengths.append(len(receipts)))

    assert returned_receipts is receipts
    assert [item["request"] for item in receipts] == list(inputs)
    assert recorded_lengths == [1, 2, 3, 4]
    takeover = events.index(("click", "#takeover"))
    resumed = events.index(("wait", 1000))
    screen = next(index for index, event in enumerate(events)
                  if event[0] == "evaluate" and "/api/screen" in event[1])
    first_input = next(index for index, event in enumerate(events)
                       if event[0] == "input")
    after_exit = events.index(("observe", "after-exit"))
    input_events = [index for index, event in enumerate(events)
                    if event[0] == "input"]
    paused = events.index(("click", "#pause"))
    assert takeover < resumed < screen < first_input
    assert input_events[-1] < after_exit < paused
    assert events[-2:] == [
        ("text", "Computer paused", 10000),
        ("close",),
    ]


def test_cleanup_succeeds_only_when_exit_restores_baseline(monkeypatch):
    dispatched = []
    inputs = (
        {"op": "click", "x": 320, "y": 420, "button": 1},
        {"op": "type", "text": "exit"},
        {"op": "key", "keys": "Return"},
    )

    def dispatch(_page, value):
        dispatched.append(value)
        return {"status": 200, "body": {
            "input": "dispatched", "control": "human"}}

    monkeypatch.setattr(installed_e2e, "browser_input", dispatch)
    receipts = []
    installed_e2e.dispatch_cleanup_and_verify(
        object(), inputs, receipts,
        lambda label: {"restored": label == "after-exit"})

    assert dispatched == list(inputs)
    assert [receipt["request"] for receipt in receipts] == list(inputs)


def test_cleanup_fails_without_sending_an_extra_close_input(monkeypatch):
    dispatched = []
    inputs = (
        {"op": "click", "x": 320, "y": 420, "button": 1},
        {"op": "type", "text": "exit"},
        {"op": "key", "keys": "Return"},
    )

    def dispatch(_page, value):
        dispatched.append(value)
        return {"status": 200, "body": {
            "input": "dispatched", "control": "human"}}

    monkeypatch.setattr(installed_e2e, "browser_input", dispatch)
    receipts = []
    with pytest.raises(RuntimeError, match="exit did not restore"):
        installed_e2e.dispatch_cleanup_and_verify(
            object(), inputs, receipts,
            lambda _label: {"restored": False})

    assert dispatched == list(inputs)
    assert [receipt["request"] for receipt in receipts] == list(inputs)


def test_cleanup_receipts_survive_visual_verification_exception(monkeypatch):
    inputs = (
        {"op": "click", "x": 320, "y": 420, "button": 1},
        {"op": "type", "text": "exit"},
        {"op": "key", "keys": "Return"},
    )
    receipts = []
    recorded = []
    monkeypatch.setattr(installed_e2e, "browser_input", lambda _page, _value: {
        "status": 200,
        "body": {"input": "dispatched", "control": "human"},
    })

    def record_receipt():
        recorded.append(list(receipts))

    def observe(_label):
        assert len(receipts) == len(inputs)
        raise RuntimeError("visual verification failed")

    with pytest.raises(RuntimeError, match="visual verification failed"):
        installed_e2e.dispatch_cleanup_and_verify(
            object(), inputs, receipts, observe, record_receipt)

    assert [len(snapshot) for snapshot in recorded] == [1, 2, 3]
    assert [receipt["request"] for receipt in receipts] == list(inputs)


def test_terminal_launch_deadline_is_absolute_from_dispatch_start():
    assert installed_e2e.terminal_launch_deadline(100.25) == 125.25


def test_installed_ocr_normalization_preserves_observation_boundaries():
    normalized = installed_e2e.normalize_ocr(
        " echo \n  tqcal1e5d3172 \r\n Pending migrations \n")
    assert normalized == "echo\ntqcal1e5d3172\nPendingmigrations"
    assert installed_e2e.ocr_has_echo_command(normalized, "tqcal1e5d3172")
    assert installed_e2e.ocr_has_echo_command(
        "echo|\ntqcal1e5d3172", "tqcal1e5d3172")
    assert installed_e2e.ocr_has_echo_command(
        "echotqcal1e5d3172", "tqcal1e5d3172")
    assert not installed_e2e.ocr_has_echo_command(
        "echo\nunrelated\ntqcal1e5d3172", "tqcal1e5d3172")
    assert installed_e2e.ocr_has_echo_output(
        "echotqcal1e5d3172\ntqcal1e5d3172", "tqcal1e5d3172")
    assert installed_e2e.ocr_has_echo_output(
        "echo\ntqcal1e5d3172\ntqcal1e5d3172", "tqcal1e5d3172")
    assert not installed_e2e.ocr_has_echo_output(
        "echo|\ntqcal1e5d3172\ntqcal1e5d3172", "tqcal1e5d3172")
    assert not installed_e2e.ocr_has_echo_output(
        "echotqcal1e5d3172\ncommandnotfound:tqcal1e5d3172", "tqcal1e5d3172")


def test_recovery_refuses_input_barrier_without_a_fresh_screen():
    class Page:
        def __init__(self):
            self.events = []

        def wait_for_timeout(self, milliseconds):
            self.events.append(("wait", milliseconds))

        def evaluate(self, expression):
            self.events.append(("evaluate", expression))
            return 503

    page = Page()
    with pytest.raises(RuntimeError, match="could not prove resumed guest output"):
        installed_e2e.prove_recovery_guest_output(page)
    assert page.events[0] == ("wait", 1000)
    assert len(page.events) == 2 and "/api/screen" in page.events[1][1]


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
