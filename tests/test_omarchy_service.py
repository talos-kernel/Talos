"""macOS service composition: strict config, peer roles and bounded QMP capture."""
import json
import os
from pathlib import Path
import socket
from unittest.mock import Mock

import pytest

from talos.computer.omarchy import SIZE
from talos.computer.omarchy_service import (
    Capture, OmarchyComputer, connect_computer, framebuffer_visible, load_config, make_preflight,
    peer_uid, ppm_to_png, wait_for_framebuffer,
)


def ppm(pixel=b"\x10\x20\x30"):
    return f"P6\n{SIZE[0]} {SIZE[1]}\n255\n".encode() + pixel * (SIZE[0] * SIZE[1])


def paths(tmp_path):
    names = {name: tmp_path / name for name in
             ("qmp", "pid_file", "disk", "state", "captures", "scratch", "control_socket")}
    for name in ("state", "captures", "scratch"):
        names[name].mkdir(mode=0o700)
    for name in ("qmp", "disk", "control_socket"):
        names[name].touch()
    names["pid_file"].write_text(str(os.getpid()))
    return names


def config(tmp_path):
    value = {"root": str(tmp_path), "owner": "a" * 64, "agent_uid": os.getuid() + 1000,
             "client_gid": os.getgid(), "view_url": "https://computer.example.test",
             "origin": "https://computer.example.test", "view_secret": "s" * 32,
             "viewer": "snapshot", "web_state": str(tmp_path / "web-state")}
    value.update({name: str(path) for name, path in paths(tmp_path).items()})
    return value


def png_fixture():
    return ppm_to_png(ppm())


class StatefulQMP:
    def __init__(self, *, ignore_stop=False, fail_stops=0):
        self.status = "paused"
        self.ignore_stop = ignore_stop
        self.fail_stops = fail_stops
        self.commands = []

    def __call__(self, command, _args=None):
        self.commands.append(command)
        if command == "query-status":
            return {"status": self.status}
        if command == "cont":
            self.status = "running"
        elif command == "stop":
            if self.fail_stops:
                self.fail_stops -= 1
                raise RuntimeError("pause failed")
            if not self.ignore_stop:
                self.status = "paused"
        return {}


def test_ppm_conversion_is_exact_bounded_png():
    result = ppm_to_png(ppm())
    assert result.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    assert int.from_bytes(result[16:20], "big") == SIZE[0]
    assert int.from_bytes(result[20:24], "big") == SIZE[1]
    assert len(result) < 100000
    with pytest.raises(ValueError):
        ppm_to_png(b"P6\n1 1\n255\n\x00\x00\x00")


def test_first_boot_visibility_rejects_black_frame():
    assert not framebuffer_visible(ppm_to_png(ppm(b"\x00\x00\x00")))
    assert not framebuffer_visible(ppm_to_png(ppm(b"\x11\x11\x11")))
    assert framebuffer_visible(png_fixture())


def test_preflight_accepts_one_fixed_virtio_nic_and_rejects_another(tmp_path):
    disk = tmp_path / "rootfs.ext4"
    disk.touch()
    ids = [(6966, 8), (6900, 4096), (6900, 4097), (6900, 4176),
           (6900, 4178), (6900, 4178), (6900, 4101), (6900, 4099)]

    def device(vendor, product):
        return {"id": {"vendor": vendor, "device": product,
                       "subsystem-vendor": 6900, "subsystem": 1},
                "class_info": {"class": 512 if product == 4096 else 256}}

    devices = [device(*item) for item in ids]

    def qmp(command):
        if command == "query-pci":
            return [{"devices": devices}]
        if command == "query-chardev":
            return [{"label": "console"}, {"label": "compat_monitor0"}]
        if command == "query-block":
            return [{"inserted": {"file": str(disk), "ro": False}}]
        if command == "query-mice":
            return [{"absolute": True}]
        raise AssertionError(command)

    check = make_preflight(qmp, disk)
    check()
    devices.append(device(6900, 4096))
    with pytest.raises(ValueError, match="network boundary"):
        check()


def test_capture_uses_service_scratch_and_removes_ppm(tmp_path):
    def qmp(command, args):
        assert command == "screendump" and args["format"] == "ppm"
        Path(args["filename"]).write_bytes(ppm())
    capture = Capture(qmp, tmp_path, settle=0)
    assert capture().startswith(b"\x89PNG")
    assert list(tmp_path.iterdir()) == []


def test_fresh_start_waits_for_real_framebuffer_before_initial_pause():
    states = iter(({"status": "paused"}, {"status": "running"}))
    qmp = Mock(side_effect=lambda command, _args=None:
               next(states) if command == "query-status" else {})
    captures = Mock(side_effect=(ppm_to_png(ppm(b"\x00\x00\x00")), png_fixture()))
    moments = iter((0.0, 0.1, 0.2))
    result = wait_for_framebuffer(qmp, captures, timeout=1,
                                  clock=lambda: next(moments), sleep=lambda _n: None)
    assert result.startswith(b"\x89PNG")
    assert any(call.args[0] == "cont" for call in qmp.call_args_list)


def test_new_qemu_generation_boots_before_pause_but_api_restart_does_not(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    qmp = StatefulQMP()
    capture = Mock(return_value=png_fixture())
    OmarchyComputer(cfg, qmp=qmp, capture=capture, check_vm=Mock())
    assert qmp.status == "paused"
    assert qmp.commands[-2:] == ["stop", "query-status"]
    assert (Path(cfg["state"]) / "vm-generation").is_file()

    wait = Mock(wraps=wait_for_framebuffer)
    monkeypatch.setattr("talos.computer.omarchy_service.wait_for_framebuffer", wait)
    restart_call = len(qmp.commands)
    OmarchyComputer(cfg, qmp=qmp, capture=capture, check_vm=Mock())
    wait.assert_not_called()
    assert "cont" not in qmp.commands[restart_call:]
    assert qmp.commands[restart_call:] == ["stop", "query-status"]

    Path(cfg["pid_file"]).write_text("99999")
    OmarchyComputer(cfg, qmp=qmp, capture=capture, check_vm=Mock())
    wait.assert_called_once_with(qmp, capture)
    assert qmp.status == "paused"


def test_first_boot_wait_failure_best_effort_pauses_without_marker(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    qmp = StatefulQMP()

    def fail_after_resume(qmp, _capture):
        qmp("cont")
        raise TimeoutError("framebuffer failed")

    monkeypatch.setattr("talos.computer.omarchy_service.wait_for_framebuffer",
                        fail_after_resume)
    with pytest.raises(TimeoutError, match="framebuffer failed"):
        OmarchyComputer(cfg, qmp=qmp, capture=Mock(), check_vm=Mock())

    assert qmp.status == "paused"
    assert qmp.commands[-3:] == ["cont", "stop", "query-status"]
    assert not (Path(cfg["state"]) / "vm-generation").exists()


def test_first_boot_construction_and_preflight_failures_repause_without_marker(
        tmp_path, monkeypatch):
    for failure in ("construction", "preflight"):
        root = tmp_path / failure
        root.mkdir()
        cfg = config(root)
        qmp = StatefulQMP()
        check_vm = Mock(side_effect=(None, ValueError("preflight failed")))
        with monkeypatch.context() as patch:
            if failure == "construction":
                check_vm = Mock()
                patch.setattr("talos.computer.omarchy_service.OfflineDesktop",
                              Mock(side_effect=RuntimeError("construction failed")))

            with pytest.raises((RuntimeError, ValueError), match=f"{failure} failed"):
                OmarchyComputer(cfg, qmp=qmp, capture=Mock(return_value=png_fixture()),
                                check_vm=check_vm)

        assert qmp.status == "paused"
        assert qmp.commands[-2:] == ["stop", "query-status"]
        assert not (Path(cfg["state"]) / "vm-generation").exists()


def test_first_boot_pause_failure_retries_stop_without_marker(tmp_path):
    cfg = config(tmp_path)
    qmp = StatefulQMP(fail_stops=1)

    with pytest.raises(RuntimeError, match="pause failed"):
        OmarchyComputer(cfg, qmp=qmp, capture=Mock(return_value=png_fixture()),
                        check_vm=Mock())

    assert qmp.status == "paused"
    assert qmp.commands.count("stop") == 2
    assert qmp.commands[-2:] == ["stop", "query-status"]
    assert not (Path(cfg["state"]) / "vm-generation").exists()


def test_first_boot_does_not_persist_marker_without_verified_pause(tmp_path):
    cfg = config(tmp_path)
    qmp = StatefulQMP(ignore_stop=True)

    with pytest.raises(RuntimeError, match="could not be proven paused"):
        OmarchyComputer(cfg, qmp=qmp, capture=Mock(return_value=png_fixture()),
                        check_vm=Mock())

    assert qmp.status == "running"
    assert qmp.commands[-4:] == ["stop", "query-status", "stop", "query-status"]
    assert not (Path(cfg["state"]) / "vm-generation").exists()


def test_marker_read_failure_attempts_and_verifies_pause(tmp_path):
    cfg = config(tmp_path)
    marker = Path(cfg["state"]) / "vm-generation"
    marker.mkdir()
    qmp = StatefulQMP()

    with pytest.raises(IsADirectoryError):
        OmarchyComputer(cfg, qmp=qmp, capture=Mock(), check_vm=Mock())

    assert qmp.status == "paused"
    assert qmp.commands[-2:] == ["stop", "query-status"]


def test_same_generation_initialization_failures_attempt_verified_pause(
        tmp_path, monkeypatch):
    for failure in ("construction", "preflight"):
        root = tmp_path / failure
        root.mkdir()
        cfg = config(root)
        qmp = StatefulQMP()
        capture = Mock(return_value=png_fixture())
        OmarchyComputer(cfg, qmp=qmp, capture=capture, check_vm=Mock())
        qmp.commands.clear()
        wait = Mock(side_effect=AssertionError("API restart must not wait"))

        with monkeypatch.context() as patch:
            patch.setattr("talos.computer.omarchy_service.wait_for_framebuffer", wait)
            check_vm = Mock(side_effect=ValueError("restart preflight failed"))
            if failure == "construction":
                check_vm = Mock()
                patch.setattr("talos.computer.omarchy_service.OfflineDesktop",
                              Mock(side_effect=RuntimeError("restart construction failed")))

            with pytest.raises((RuntimeError, ValueError), match=f"restart {failure} failed"):
                OmarchyComputer(cfg, qmp=qmp, capture=capture, check_vm=check_vm)

        wait.assert_not_called()
        assert "cont" not in qmp.commands
        assert qmp.status == "paused"
        assert qmp.commands[-2:] == ["stop", "query-status"]


def test_cleanup_stop_failure_escalates_even_if_status_was_paused(tmp_path):
    cfg = config(tmp_path)
    (Path(cfg["state"]) / "vm-generation").mkdir()
    qmp = StatefulQMP(fail_stops=1)

    with pytest.raises(RuntimeError, match="could not be proven paused"):
        OmarchyComputer(cfg, qmp=qmp, capture=Mock(), check_vm=Mock())

    assert qmp.commands[-2:] == ["stop", "query-status"]


def test_service_waits_for_fixed_qmp_startup_without_retrying_rejections():
    sentinel = object()
    factory = Mock(side_effect=(FileNotFoundError(), ConnectionRefusedError(), sentinel))
    moments = iter((0.0, 0.1, 0.2, 0.3))
    assert connect_computer({}, timeout=1, factory=factory,
                            clock=lambda: next(moments), sleep=lambda _n: None) is sentinel
    rejecting = Mock(side_effect=ValueError("peer mismatch"))
    with pytest.raises(ValueError, match="peer mismatch"):
        connect_computer({}, factory=rejecting)
    assert rejecting.call_count == 1


def test_config_rejects_unknown_fields_and_paths_outside_root(tmp_path):
    value = config(tmp_path)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    assert load_config(path)["disk"] == value["disk"]
    path.write_text(json.dumps(value | {"extra": True}))
    with pytest.raises(ValueError, match="unknown or missing"):
        load_config(path)
    path.write_text(json.dumps(value | {"disk": "/tmp/outside"}))
    with pytest.raises(ValueError, match="canonical and service-owned"):
        load_config(path)


def test_config_allows_exact_loopback_http_but_not_arbitrary_cleartext(tmp_path):
    value = config(tmp_path)
    path = tmp_path / "config.json"
    value |= {"view_url": "http://127.0.0.1:8830",
              "origin": "http://127.0.0.1:8830"}
    path.write_text(json.dumps(value))
    assert load_config(path)["viewer"] == "snapshot"
    value |= {"view_url": "http://computer.example.test",
              "origin": "http://computer.example.test"}
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="exact loopback"):
        load_config(path)


def test_darwin_or_linux_peer_identity_comes_from_unix_socket():
    left, right = socket.socketpair()
    try:
        assert peer_uid(left) == os.getuid()
    finally:
        left.close(); right.close()


@pytest.fixture
def computer(tmp_path):
    cfg = config(tmp_path)
    qmp = StatefulQMP()
    value = OmarchyComputer(cfg, qmp=qmp, capture=Mock(return_value=png_fixture()),
                            check_vm=Mock())
    value.desktop.control("agent", human=True)
    qmp.commands.clear()
    return value


def test_agent_owner_and_human_roles_stay_separate(computer):
    args = {"op": "click", "project": "fixture", "key": "click-one",
            "title": "Click fixture", "x": 10, "y": 20}
    receipt = computer.handle({"kind": "action", "owner": "a" * 64, "args": args},
                              computer.config["agent_uid"])
    assert receipt["job"]["state"] == "queued"
    with pytest.raises(ValueError, match="identity"):
        computer.handle({"kind": "read", "owner": "b" * 64,
                         "args": {"op": "status"}}, computer.config["agent_uid"])
    with pytest.raises(ValueError, match="trusted workbench"):
        computer.handle({"kind": "human", "owner": "a" * 64,
                         "args": {"op": "takeover"}}, computer.config["agent_uid"])


def test_human_takeover_allows_only_bounded_desktop_input(computer):
    computer.handle({"kind": "human", "args": {"op": "takeover"}}, os.getuid())
    assert computer.desktop.store.control() == "human"
    result = computer.handle({"kind": "human", "args": {
        "op": "input", "input": {"op": "click", "x": 50, "y": 60}}}, os.getuid())
    assert result == {"input": "delivered", "control": "human"}
    with pytest.raises(ValueError, match="unknown human"):
        computer.handle({"kind": "human", "args": {
            "op": "input", "input": {"op": "click", "x": 1, "y": 1},
            "owner": "forged"}}, os.getuid())


def test_preview_is_human_only_and_does_not_create_agent_job(computer):
    with pytest.raises(ValueError, match="trusted workbench"):
        computer.handle({"kind": "read", "owner": "a" * 64,
                         "args": {"op": "preview"}}, computer.config["agent_uid"])
    result = computer.handle({"kind": "read", "args": {"op": "preview"}}, os.getuid())
    assert Path(result["image_path"]).is_file()
    assert computer.desktop.store.jobs("a" * 64) == []


def test_agent_screenshot_gets_client_group_without_exposing_preview(computer):
    receipt = computer.handle({"kind": "read", "owner": "a" * 64,
                               "args": {"op": "screenshot"}},
                              computer.config["agent_uid"])
    screen = Path(receipt["image_path"])
    assert screen.stat().st_gid == computer.config["client_gid"]
    assert screen.stat().st_mode & 0o777 == 0o640
    preview = Path(computer.handle({"kind": "read", "args": {"op": "preview"}},
                                   os.getuid())["image_path"])
    assert preview.stat().st_uid == os.getuid()


def test_guest_and_host_file_surfaces_are_not_exposed(computer):
    with pytest.raises(ValueError, match="does not expose"):
        computer.handle({"kind": "read", "owner": "a" * 64,
                         "args": {"op": "files", "project": "fixture"}},
                        computer.config["agent_uid"])
