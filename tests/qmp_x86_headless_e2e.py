#!/usr/bin/env python3
"""Run the real QMP VM backend against an already started offline lab VM.

This is intentionally not part of pytest: it requires the signed local QEMU app
and a booted guest. The script leaves the VM paused and writes only bounded PNG
evidence plus the private job database beneath ``--evidence-dir``.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talos.computer.qmp_service import Capture, QmpComputer, make_preflight
from talos.computer.qmp import LocalQMP


SESSION_FIELDS = frozenset({
    "qmp", "pid", "display", "offline", "architecture", "distribution",
    "desktop", "geometry", "pci_devices",
})
OCR_SCRIPT = Path(__file__).with_name("vision_ocr.swift")


def expected_identity(pci_devices):
    return {
        "architecture": "x86_64", "distribution": "debian",
        "desktop": "hyprland", "geometry": [1440, 900], "offline": True,
        "pci_devices": [list(item) for item in pci_devices],
    }


def validate_session(session):
    """Accept only the fixed, offline x86 guest contract used by this E2E."""
    if not isinstance(session, dict) or set(session) != SESSION_FIELDS:
        raise RuntimeError("E2E session has unknown or missing fields")
    pci_devices = session.get("pci_devices")
    if (not isinstance(pci_devices, list) or not 1 <= len(pci_devices) <= 32
            or any(not isinstance(item, list) or len(item) != 2
                   or any(type(number) is not int or not 0 <= number <= 0xffff
                          for number in item)
                   for item in pci_devices)
            or pci_devices.count([0x1AF4, 0x1000]) != 1):
        raise RuntimeError(
            "E2E requires the signed bounded PCI profile with one virtio NIC")
    identity = expected_identity(pci_devices)
    if (session.get("display") != "software-headless"
            or session.get("offline") is not True
            or any(session.get(name) != value for name, value in identity.items()
                   if name != "offline")):
        raise RuntimeError(
            "E2E requires the fixed offline x86_64 Debian Hyprland 1440x900 VM identity"
        )
    if (not isinstance(session.get("qmp"), str) or not session["qmp"]
            or type(session.get("pid")) is not int or session["pid"] <= 0):
        raise RuntimeError("E2E session QMP path or PID is invalid")
    return identity


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


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def recognize(path, *, timeout=10):
    try:
        completed = subprocess.run(
            ["/usr/bin/swift", str(OCR_SCRIPT), str(path)], check=True,
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("bounded Vision OCR failed") from error
    return "".join(completed.stdout.casefold().split())


def make_marker():
    alphabet = "23456789abcdefgh"
    return "TQ" + "".join(secrets.choice(alphabet) for _ in range(12))


def action(computer, run, suffix, **values):
    args = {"project": "qmp-e2e", "key": f"{run}-{suffix}",
            "title": f"QMP VM E2E {suffix}"} | values
    receipt = computer.desktop.submit(args)
    job = wait_job(computer, receipt["job"]["id"])
    if job["state"] != "needs_review":
        raise RuntimeError(f"{suffix} ended as {job['state']}")
    return job


def timed_action(computer, run, suffix, **values):
    started = time.monotonic()
    job = action(computer, run, suffix, **values)
    elapsed = round((time.monotonic() - started) * 1000)
    if not 0 <= elapsed <= 10_000:
        raise RuntimeError(f"{suffix} exceeded its 10 second input deadline")
    return job, elapsed


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


def start_computer(config, transport, capture):
    preflight = make_preflight(
        transport, Path(config["disk"]), config["architecture"],
        config["pci_devices"], capture,
    )
    return QmpComputer(
        config, qmp=transport, capture=capture, check_vm=preflight)


def exit_terminal(computer, run):
    """Exit the known shell and capture the resulting desktop before claiming cleanup."""
    typed, type_ms = timed_action(computer, run, "type-exit", op="type", text="exit")
    submitted, return_ms = timed_action(
        computer, run, "submit-exit", op="key", keys="Return")
    time.sleep(0.5)
    return (typed, submitted, computer.desktop.read({"op": "screenshot"}),
            {"type_exit": type_ms, "submit_exit": return_ms})


def prove_guest_command(computer, run, suffix, command, expected):
    typed, type_ms = timed_action(
        computer, run, "type-" + suffix, op="type", text=command)
    submitted, return_ms = timed_action(
        computer, run, "submit-" + suffix, op="key", keys="Return")
    time.sleep(0.5)
    frame = computer.desktop.read({"op": "screenshot"})
    if expected.casefold() not in recognize(frame["image_path"]):
        raise RuntimeError(f"guest {suffix} identity was not visible in OCR")
    return (typed, submitted, frame,
            {"type_" + suffix: type_ms, "submit_" + suffix: return_ms})


def parse(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--disk", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    options = parse(argv)

    session = json.loads(options.session.resolve().read_text())
    identity = validate_session(session)
    qmp_path = Path(session["qmp"]).resolve()
    disk = options.disk.resolve(strict=True)
    evidence = options.evidence_dir.resolve()
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    state, captures, scratch, web_state = [evidence / name for name in
                                            ("state", "captures", "scratch", "web-state")]
    for folder in (state, captures, scratch, web_state):
        folder.mkdir(mode=0o700, exist_ok=True)
    owner = hashlib.sha256(b"talos-qmp-local-e2e").hexdigest()
    config = {
        "root": str(evidence), "owner": owner, "agent_uid": os.getuid() + 1000,
        "client_gid": os.getgid(), "qmp": str(qmp_path),
        "pid_file": str(evidence / "vm.pid"), "disk": str(disk),
        "state": str(state), "captures": str(captures), "scratch": str(scratch),
        "control_socket": str(evidence / "control.sock"),
        "view_url": "https://computer.e2e.invalid",
        "origin": "https://computer.e2e.invalid", "view_secret": "e" * 32,
        "viewer": "snapshot", "web_state": str(web_state),
        "architecture": identity["architecture"],
        "distribution": identity["distribution"], "desktop": identity["desktop"],
        "geometry": list(identity["geometry"]),
        "pci_devices": [list(item) for item in identity["pci_devices"]],
    }
    Path(config["pid_file"]).write_text(str(session["pid"]) + "\n")
    transport = LocalQMP(qmp_path, pid=session["pid"])
    computer = None
    result = {}
    try:
        capture = Capture(transport, scratch, settle=0.35)
        computer = start_computer(config, transport, capture)
        if computer.status()["control"] != "paused":
            raise RuntimeError("restart fail-closed state was not paused")
        computer.desktop.control("agent", human=True)
        before = computer.desktop.read({"op": "screenshot"})
        baseline_ocr = recognize(before["image_path"])
        run = "e2e" + str(int(time.time()))
        marker = make_marker()
        if marker.casefold() in baseline_ocr:
            raise RuntimeError("fresh E2E marker already appears in the baseline")
        opened, open_ms = timed_action(
            computer, run, "open-terminal", op="key", keys="super+Return")
        time.sleep(0.5)
        terminal = computer.desktop.read({"op": "screenshot"})
        typed, type_ms = timed_action(
            computer, run, "type-marker", op="type", text="echo " + marker)
        entered, return_ms = timed_action(
            computer, run, "submit-marker", op="key", keys="Return")
        time.sleep(0.5)
        final = computer.desktop.read({"op": "screenshot"})
        final_ocr = recognize(final["image_path"])
        if marker.casefold() not in final_ocr:
            raise RuntimeError("typed E2E marker was not visible in guest OCR")
        hashes = [png_receipt(value["image_path"]) for value in (before, terminal, final)]
        if len(set(hashes)) != 3:
            raise RuntimeError("visible desktop did not change across E2E actions")

        identity_jobs = []
        identity_screens = []
        identity_timings = {}
        for suffix, command, expected in (
                ("architecture", "uname -m", "x86_64"),
                ("distribution", ". /etc/os-release;echo $ID", "debian"),
                ("desktop", "hyprctl version", "hyprland")):
            command_typed, command_submitted, command_frame, command_timings = (
                prove_guest_command(computer, run, suffix, command, expected))
            identity_jobs.extend((command_typed["id"], command_submitted["id"]))
            identity_screens.append(command_frame["image_path"])
            identity_timings.update(command_timings)
            hashes.append(png_receipt(command_frame["image_path"]))

        exit_typed, exit_submitted, cleaned, cleanup_timings = exit_terminal(computer, run)
        cleanup_hash = png_receipt(cleaned["image_path"])
        cleaned_ocr = recognize(cleaned["image_path"])
        if cleanup_hash == hashes[-1] or marker.casefold() in cleaned_ocr:
            raise RuntimeError("terminal exit did not visibly restore a marker-free desktop")
        hashes.append(cleanup_hash)

        job_count = len(computer.status()["jobs"])
        computer.handle({"kind": "human", "args": {"op": "takeover"}}, os.getuid())
        human = computer.handle({"kind": "human", "args": {"op": "input", "input": {
            "op": "click", "x": 720, "y": 450}}}, os.getuid())
        if human != {"input": "dispatched", "control": "human"}:
            raise RuntimeError("human input receipt changed")
        if len(computer.status()["jobs"]) != job_count:
            raise RuntimeError("human input incorrectly created a model job")
        result = {"ok": True, "control": "human", "identity": identity,
                  "marker_visible_ocr": True, "cleanup_marker_count": 0,
                  "observed_guest": {"architecture": "x86_64",
                                     "distribution": "debian",
                                     "desktop": "hyprland"},
                  "timings_ms": {"open_terminal": open_ms,
                                 "type_marker": type_ms,
                                 "submit_marker": return_ms}
                                | identity_timings | cleanup_timings,
                  "disk_sha256": sha256_file(disk),
                  "harness_sha256": sha256_file(Path(__file__)),
                  "hashes": hashes,
                  "screens": [before["image_path"], terminal["image_path"],
                              final["image_path"], *identity_screens,
                              cleaned["image_path"]],
                  "jobs": [opened["id"], typed["id"], entered["id"],
                           *identity_jobs,
                           exit_typed["id"], exit_submitted["id"]],
                  "isolation": "exact-pci-one-nic-one-disk-no-host-bridge"}
    finally:
        try:
            if computer is not None:
                final_status = leave_paused(computer)
                if result:
                    result["final_control"] = final_status["control"]
                    result["final_vm"] = final_status["vm"]
        finally:
            transport.close()
    (evidence / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
