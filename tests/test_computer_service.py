"""Control handovers cannot turn interrupted work back into a success."""
import io
import json
from unittest.mock import Mock
import pytest
from talos.computer import service

ACTION = {"op":"exec","project":"report","key":"first-report","title":"Report",
          "command":"printf done > report.txt","checks":[]}

@pytest.fixture
def computer(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DATA", tmp_path)
    monkeypatch.setattr(service, "qmp", lambda op: {"status":"running"})
    monkeypatch.setattr(service, "cancel_units", lambda: None)
    result = service.Computer({"owner":"owner", "agent_uid":12345, "view_url":"https://example.test"})
    result.store.control("agent")
    return result


def test_captures_inherit_exact_agent_read_access_after_final_chmod(computer, tmp_path, monkeypatch):
    import base64
    import os
    import runpy
    import shutil
    import subprocess
    from pathlib import Path

    if not shutil.which("setfacl") or not shutil.which("getfacl"):
        pytest.skip("Linux POSIX ACL tools required")
    installer = runpy.run_path(str(Path(__file__).resolve().parents[1] / "deploy/computer-setup.py"))
    captures = tmp_path / "captures"
    captures.mkdir(mode=0o750)
    agent_uid = 12345 if os.getuid() != 12345 else 12346
    installer["allow_capture_reads"](captures, agent_uid)
    monkeypatch.setattr(service, "CAPTURES", captures)
    monkeypatch.setattr(service, "guest", lambda *a, **k: {
        "png": base64.b64encode(b"\x89PNG\r\n\x1a\nneutral fixture").decode(),
        "captured_at": "2026-01-01T00:00:00Z",
    })
    computer.config["desktop"] = True
    directory_acl = subprocess.check_output(["getfacl", "-cpn", str(captures)], text=True)
    assert f"user:{agent_uid}:r-x" in directory_acl.splitlines()
    assert f"default:user:{agent_uid}:r--" in directory_acl.splitlines()
    assert "other::---" in directory_acl.splitlines()
    for _ in range(2):
        capture = Path(computer.capture()["image_path"])
        acl = subprocess.check_output(["getfacl", "-cpn", str(capture)], text=True).splitlines()
        assert f"user:{agent_uid}:r--" in acl
        assert "mask::r--" in acl
        assert "other::---" in acl
        assert capture.stat().st_uid == os.getuid()
        assert capture.stat().st_mode & 0o777 == 0o640

def test_agent_cannot_claim_human_control(computer):
    with pytest.raises(ValueError, match="trusted web service"):
        computer.handle({"kind":"human","owner":"owner","args":{"op":"takeover"}},12345)
    assert computer.store.control()=="agent"

def test_foreign_identity_cannot_read_computer(computer):
    with pytest.raises(ValueError, match="identity"):
        computer.handle({"kind":"read","owner":"stranger","args":{"op":"status"}},12345)

def test_operator_handover_interrupts_pending_work(computer):
    job,_=computer.store.begin("owner",ACTION)
    computer.control("human")
    assert computer.store.get("owner",job["id"])["state"]=="interrupted"
    with pytest.raises(ValueError,match="operator"):
        computer.action("owner",ACTION|{"key":"second"})
    with pytest.raises(ValueError,match="only they"):
        computer.action("owner",{"op":"resume"})

def test_cancelled_queued_job_cannot_start_after_control_returns(computer,monkeypatch):
    job,_=computer.store.begin("owner",ACTION)
    computer.control("human")
    computer.control("agent")
    start=Mock()
    monkeypatch.setattr(service.subprocess,"Popen",start)
    computer.run(job["id"],ACTION)
    start.assert_not_called()
    assert computer.store.get("owner",job["id"])["state"]=="interrupted"

def test_late_receipt_cannot_overwrite_interrupted_state(computer,monkeypatch):
    job,_=computer.store.begin("owner",ACTION)
    proc=Mock(stdin=io.BytesIO(),stdout=io.BytesIO(b"TALOS_READY\n"),returncode=0)
    proc.poll.return_value=0
    def finish_after_handover(**kw):
        computer.control("human")
        computer.control("agent")
        return json.dumps({"state":"verified","checks":[{"passed":True}]}).encode(),b""
    proc.communicate.side_effect=finish_after_handover
    monkeypatch.setattr(service.subprocess,"Popen",lambda *a,**kw:proc)
    monkeypatch.setattr(service.select,"select",lambda *a:([proc.stdout],[],[]))
    computer.run(job["id"],ACTION)
    assert computer.store.get("owner",job["id"])["state"]=="interrupted"


def test_headless_blocks_visual_effects_before_any_guest_work(computer, monkeypatch):
    computer.config["desktop"] = False
    calls = Mock()
    monkeypatch.setattr(service, "guest", calls)
    assert computer.status("owner")["mode"] == "headless"
    with pytest.raises(ValueError, match="desktop is disabled"):
        computer.capture()
    with pytest.raises(ValueError, match="desktop is disabled"):
        computer.action("owner", {"op":"click", "project":"report", "key":"click", "title":"Click", "x":1,"y":1})
    with pytest.raises(ValueError, match="desktop is disabled"):
        computer.handle({"kind":"human", "args":{"op":"takeover"}}, 0)
    calls.assert_not_called()
    assert computer.store.jobs("owner") == []
    assert computer.store.control() == "agent"


def test_headless_keeps_exec_files_and_pause_available(computer, monkeypatch):
    computer.config["desktop"] = False
    worker = Mock()
    monkeypatch.setattr(service.threading, "Thread", worker)
    job = computer.action("owner", ACTION)
    assert job["job"]["state"] == "queued"
    worker.return_value.start.assert_called_once()
    monkeypatch.setattr(service, "guest", lambda args, timeout: {"files":[{"name":"report.txt","bytes":4}]})
    assert computer.handle({"kind":"read","owner":"owner","args":{"op":"files","project":"report"}},12345)["files"][0]["name"] == "report.txt"
    computer.action("owner", {"op":"pause"})
    assert computer.store.control() == "paused"
