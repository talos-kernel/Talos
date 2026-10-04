#!/usr/bin/env python3
"""Prove installed Omarchy outbound HTTPS without opening a host TCP listener.

This macOS E2E drives the installed guest through Talos' bounded control socket,
requires a success marker to appear as terminal output, checks the production
QEMU/network-helper command lines and confirms neither process listens on TCP.
It never reads the raw QMP socket, guest disk, workbench secret or host files.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
import uuid


def profile_values(profile: Path) -> dict[str, str]:
    values = {}
    for raw in (profile / "talos.env").read_text(encoding="utf-8").splitlines():
        if "=" in raw and not raw.lstrip().startswith("#"):
            key, value = raw.split("=", 1)
            values[key] = value
    required = {"TALOS_COMPUTER_SOCKET", "TALOS_COMPUTER_OWNER_SHA256"}
    if not required <= values.keys():
        raise RuntimeError("Talos profile does not contain the Omarchy binding")
    return values


class Client:
    def __init__(self, endpoint: str, owner: str):
        self.endpoint, self.owner = endpoint, owner

    def call(self, kind: str, args: dict, *, timeout: float = 20) -> dict:
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("RPC timeout must be a positive finite number")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(self.endpoint)
            frame = {"kind": kind, "owner": self.owner, "args": args}
            connection.sendall(json.dumps(frame).encode() + b"\n")
            result = json.loads(connection.makefile("rb").readline())
        if result.get("error"):
            raise RuntimeError(result["error"])
        return result

    def action(self, args: dict, timeout=120) -> dict:
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("action timeout must be a positive finite number")
        deadline = time.monotonic() + timeout
        timeout_message = "desktop input did not settle"
        result = self.call(
            "action", args,
            timeout=remaining_time(deadline, timeout_message))
        remaining_time(deadline, timeout_message)
        job = result.get("job")
        if job is None:
            return result
        while True:
            state = self.call(
                "read", {"op": "job", "job_id": job["id"]},
                timeout=remaining_time(deadline, timeout_message))
            remaining = remaining_time(deadline, timeout_message)
            if state["state"] not in {"queued", "running"}:
                if state["state"] != "needs_review":
                    raise RuntimeError("desktop input ended as " + state["state"])
                return state
            time.sleep(min(0.2, remaining))


def launch_pid(label: str) -> int:
    value = subprocess.run(
        ["/bin/launchctl", "print", "system/" + label], check=True,
        capture_output=True, text=True).stdout
    match = re.search(r"^\s*pid = (\d+)$", value, re.MULTILINE)
    if not match:
        raise RuntimeError(label + " is not running")
    return int(match.group(1))


def process_command(pid: int) -> str:
    return subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "command="], check=True,
        capture_output=True, text=True).stdout.strip()


def launchd_label_is_absent(label: str) -> bool:
    result = subprocess.run(
        ["/bin/launchctl", "print", "system/" + label], check=False,
        capture_output=True, text=True)
    diagnostic = f'Could not find service "{label}" in domain for system'
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    # A generic launchctl failure does not prove that the requested label is absent.
    return result.returncode == 113 and diagnostic in output


def tcp_listeners(pid: int) -> str:
    output = subprocess.run(
        ["/usr/sbin/lsof", "-Pan", "-p", str(pid)],
        check=True, capture_output=True, text=True).stdout
    # Filtering in Python keeps an empty listener set distinct from lsof failing:
    # lsof's own filtered query exits nonzero when it simply finds no matches.
    return "\n".join(
        line for line in output.splitlines()
        if re.search(r"\bTCP\b.*\(LISTEN\)\s*$", line)
    )


def input_args(run: str, op: str, name: str, **extra) -> dict:
    return {
        "op": op, "project": "omarchy-network-e2e", "key": f"{run}-{name}",
        "title": "Installed Omarchy network proof", "checks": [], **extra,
    }


def type_in_bounded_chunks(client: Client, run: str, text: str) -> None:
    """Compose one command without exceeding a virtio keyboard report."""
    for index, offset in enumerate(range(0, len(text), 10)):
        client.action(input_args(
            run, "type", f"type-{index:03d}", text=text[offset:offset + 10]))


def remaining_time(
        deadline: float,
        message: str = "network evidence deadline expired") -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(message)
    return remaining


def capture_and_recognize(client: Client, ocr: Path, deadline: float) -> tuple[Path, str]:
    """Capture and OCR without letting either operation exceed the deadline."""
    receipt = client.call(
        "read", {"op": "screenshot", "question": "Verify DNS and HTTPS markers"},
        timeout=remaining_time(deadline))
    capture = Path(receipt["image_path"])
    recognized = subprocess.run(
        ["/usr/bin/swift", str(ocr), str(capture)], check=True,
        capture_output=True, text=True,
        timeout=remaining_time(deadline)).stdout.replace(" ", "")
    # A subprocess can return just after its timeout budget. Such evidence is
    # useful for diagnostics, but it is not proof within the semantic deadline.
    remaining_time(deadline)
    return capture, recognized


def marker_occurrences(recognized: str, marker: str) -> int:
    """Count a visible marker while tolerating Vision's O/0 glyph ambiguity."""
    normalize = str.maketrans({"0": "o"})
    return recognized.casefold().translate(normalize).count(
        marker.casefold().translate(normalize))


def leave_paused(client: Client) -> dict:
    """Request pause, then independently read back control and VM state."""
    pause_error = None
    try:
        client.action({"op": "pause"})
    except Exception as error:
        pause_error = error

    try:
        status = client.call("read", {"op": "status"})
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
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument(
        "--install-evidence", type=Path,
        default=Path("/Library/Application Support/TalosOmarchy/install-evidence.json"))
    parser.add_argument("--marker", default="TALOSNETOK")
    options = parser.parse_args()

    profile = options.profile.resolve(strict=True)
    evidence = options.evidence_dir.resolve()
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    installed = json.loads(options.install_evidence.read_text(encoding="utf-8"))
    if installed.get("network") != "qemu-user-nat" or installed.get("host_forwards") != []:
        raise RuntimeError("installed network evidence does not match QEMU user NAT")

    values = profile_values(profile)
    client = Client(values["TALOS_COMPUTER_SOCKET"],
                    values["TALOS_COMPUTER_OWNER_SHA256"])
    run = "net-" + uuid.uuid4().hex[:10]
    marker = options.marker + run[-4:].upper()
    end_marker = "TALOSPROBEEND" + run[-4:].upper()
    capture = None
    recognized = ""
    result = {}
    try:
        status = client.call("read", {"op": "status"})
        if status.get("control") != "paused" or status.get("vm") != "paused":
            raise RuntimeError("installed guest is not in the expected fail-closed state")
        client.action({"op": "resume"})
        time.sleep(1)
        client.action(input_args(run, "key", "cancel-stale", keys="ctrl+c"))
        client.action(input_args(run, "key", "terminal", keys="super+Return"))
        time.sleep(3)
        command = (
            "timeout 10 getent hosts example.com&&curl -4 -fsS --max-time 15 "
            f"https://example.com>/dev/null&&echo {marker};echo {end_marker}")
        type_in_bounded_chunks(client, run, command)
        time.sleep(1)
        client.action(input_args(run, "key", "run", keys="Return"))

        deadline = time.monotonic() + 35
        ocr = Path(__file__).with_name("vision_ocr.swift")
        completed_in_time = False
        while True:
            try:
                time.sleep(min(1, remaining_time(deadline)))
                capture, recognized = capture_and_recognize(client, ocr, deadline)
            except (TimeoutError, subprocess.TimeoutExpired):
                break
            if marker_occurrences(recognized, end_marker) >= 2:
                completed_in_time = True
                break
        if not completed_in_time:
            raise TimeoutError("installed guest did not finish within the evidence deadline")
        shutil.copy2(capture, evidence / "online-terminal.png")
        (evidence / "ocr.txt").write_text(recognized, encoding="utf-8")
        occurrences = marker_occurrences(recognized, marker)
        if occurrences < 2:
            raise RuntimeError("visible terminal did not prove outbound DNS and HTTPS")

        vm_pid = launch_pid("org.talos.omarchy.vm")
        vm_command = process_command(vm_pid)
        if ("-netdev user,id=talos-omarchy-net,ipv6=off" not in vm_command
                or "hostfwd" in vm_command or "-nic none" in vm_command):
            raise RuntimeError("installed QEMU command violates the NAT boundary")
        retired_label = "org.talos.omarchy.network"
        if (not launchd_label_is_absent(retired_label)
                or Path("/Library/LaunchDaemons",
                        retired_label + ".plist").exists()):
            raise RuntimeError("retired root network helper is still installed")
        if tcp_listeners(vm_pid):
            raise RuntimeError("Omarchy opened an unexpected host TCP listener")
        client.action(input_args(run, "key", "close-terminal", keys="super+W"))
        result = {
            "ok": True, "dns_https_visible": True, "host_tcp_listeners": 0,
            "host_forwarding": False, "root_network_helper": False,
            "marker_occurrences": occurrences,
        }
    finally:
        leave_paused(client)
    (evidence / "result.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
