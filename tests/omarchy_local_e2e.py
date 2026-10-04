#!/usr/bin/env python3
"""Run the real Omarchy backend against an already started offline lab VM.

This is intentionally not part of pytest: it requires the signed local QEMU app
and a booted guest. The script leaves the VM paused and writes only bounded PNG
evidence plus the private job database beneath ``--evidence-dir``.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talos.computer.omarchy_service import Capture, OmarchyComputer, make_preflight
from talos.computer.qmp import LocalQMP


def wait_job(computer, job_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = computer.desktop.read({"op": "job", "job_id": job_id})
        if time.monotonic() >= deadline:
            break
        if job["state"] not in {"queued", "running"}:
            return job
        time.sleep(0.05)
    raise TimeoutError(f"job {job_id} did not reach a durable terminal state")


def png_receipt(path):
    raw = Path(path).read_bytes()
    if (raw[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            or int.from_bytes(raw[16:20], "big") != 1440
            or int.from_bytes(raw[20:24], "big") != 900):
        raise RuntimeError("E2E capture is not a 1440x900 PNG")
    return hashlib.sha256(raw).hexdigest()


def action(computer, run, suffix, **values):
    args = {"project": "omarchy-e2e", "key": f"{run}-{suffix}",
            "title": f"Omarchy E2E {suffix}"} | values
    receipt = computer.desktop.submit(args)
    job = wait_job(computer, receipt["job"]["id"])
    if job["state"] != "needs_review":
        raise RuntimeError(f"{suffix} ended as {job['state']}")
    return job


def leave_paused(computer):
    """Pause the guest and independently prove both durable control states."""
    pause_error = None
    try:
        computer.desktop.control("paused", human=True)
    except Exception as error:
        pause_error = error

    try:
        status = computer.status()
    except Exception as error:
        raise RuntimeError("final pause status read-back failed") from error
    if (pause_error is not None or status.get("control") != "paused"
            or status.get("vm") != "paused"):
        raise RuntimeError(
            "final pause could not be proven as control=paused and vm=paused"
        ) from pause_error
    return status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--disk", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    options = parser.parse_args()

    session = json.loads(options.session.resolve().read_text())
    if session.get("offline") is not True or session.get("display") != "software-headless":
        raise RuntimeError("E2E requires the software-headless offline VM profile")
    qmp_path = Path(session["qmp"]).resolve()
    disk = options.disk.resolve()
    evidence = options.evidence_dir.resolve()
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    state, captures, scratch, web_state = [evidence / name for name in
                                            ("state", "captures", "scratch", "web-state")]
    for folder in (state, captures, scratch, web_state):
        folder.mkdir(mode=0o700, exist_ok=True)
    owner = hashlib.sha256(b"talos-omarchy-local-e2e").hexdigest()
    config = {
        "root": str(evidence), "owner": owner, "agent_uid": os.getuid() + 1000,
        "client_gid": os.getgid(), "qmp": str(qmp_path),
        "pid_file": str(evidence / "vm.pid"), "disk": str(disk),
        "state": str(state), "captures": str(captures), "scratch": str(scratch),
        "control_socket": str(evidence / "control.sock"),
        "view_url": "https://computer.e2e.invalid",
        "origin": "https://computer.e2e.invalid", "view_secret": "e" * 32,
        "viewer": "snapshot", "web_state": str(web_state),
    }
    Path(config["pid_file"]).write_text(str(session["pid"]) + "\n")
    transport = LocalQMP(qmp_path, pid=session["pid"])
    computer = None
    result = {}
    try:
        capture = Capture(transport, scratch, settle=0.35)
        computer = OmarchyComputer(config, qmp=transport, capture=capture,
                                   check_vm=make_preflight(transport, disk))
        if computer.status()["control"] != "paused":
            raise RuntimeError("restart fail-closed state was not paused")
        computer.desktop.control("agent", human=True)
        before = computer.desktop.read({"op": "screenshot"})
        run = "e2e" + str(int(time.time()))
        opened = action(computer, run, "open-terminal", op="key", keys="super+Return")
        time.sleep(0.5)
        terminal = computer.desktop.read({"op": "screenshot"})
        typed = action(computer, run, "type-marker", op="type",
                       text="echo TALOSE2E")
        entered = action(computer, run, "submit-marker", op="key", keys="Return")
        time.sleep(0.5)
        final = computer.desktop.read({"op": "screenshot"})
        hashes = [png_receipt(value["image_path"]) for value in (before, terminal, final)]
        if len(set(hashes)) != 3:
            raise RuntimeError("visible desktop did not change across E2E actions")
        closed = action(computer, run, "close-terminal", op="key", keys="super+W")

        job_count = len(computer.status()["jobs"])
        computer.handle({"kind": "human", "args": {"op": "takeover"}}, os.getuid())
        human = computer.handle({"kind": "human", "args": {"op": "input", "input": {
            "op": "click", "x": 720, "y": 450}}}, os.getuid())
        if human != {"input": "dispatched", "control": "human"}:
            raise RuntimeError("human input receipt changed")
        if len(computer.status()["jobs"]) != job_count:
            raise RuntimeError("human input incorrectly created a model job")
        result = {"ok": True, "control": "human", "hashes": hashes,
                  "screens": [before["image_path"], terminal["image_path"], final["image_path"]],
                  "jobs": [opened["id"], typed["id"], entered["id"], closed["id"]],
                  "isolation": "exact-pci-no-nic-one-disk-no-host-bridge"}
    finally:
        try:
            if computer is not None:
                leave_paused(computer)
        finally:
            transport.close()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
