#!/usr/bin/env python3
"""Prove an installed API restart is fail-closed and does not replay input."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talos.configcli import read_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    options = parser.parse_args()
    values = read_file(options.profile.resolve(strict=True) / "talos.env")
    socket_path = values["TALOS_COMPUTER_SOCKET"]
    owner = values["TALOS_COMPUTER_OWNER_SHA256"]

    def rpc():
        with socket.socket(socket.AF_UNIX) as channel:
            channel.settimeout(5)
            channel.connect(socket_path)
            channel.sendall(json.dumps({
                "kind": "read", "owner": owner, "args": {"op": "status"},
            }).encode() + b"\n")
            response = json.loads(channel.makefile("rb").readline())
        if "error" in response:
            raise RuntimeError(response["error"])
        return response

    def api_process():
        output = subprocess.run(
            ["/bin/launchctl", "print", "system/org.talos.omarchy.api"],
            check=True, capture_output=True, text=True, timeout=10).stdout
        pid = re.search(r"^\s*pid = (\d+)$", output, re.MULTILINE)
        state = re.search(r"^\s*state = (\w+)$", output, re.MULTILINE)
        if not pid or not state:
            raise RuntimeError("installed API launchd state is incomplete")
        return int(pid.group(1)), state.group(1)

    before = rpc()
    before_pid, before_state = api_process()
    if before["control"] != "paused" or before["vm"] != "paused" or before_state != "running":
        raise RuntimeError("restart E2E must begin fail-closed")

    apple_script = (
        'do shell script "/bin/launchctl kickstart -k system/org.talos.omarchy.api" '
        "with administrator privileges"
    )
    subprocess.run(["/usr/bin/osascript", "-e", apple_script], check=True, timeout=60)

    deadline = time.monotonic() + 30
    last_error = None
    while time.monotonic() < deadline:
        try:
            after_pid, after_state = api_process()
            if after_pid != before_pid and after_state == "running":
                after = rpc()
                break
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            last_error = error
        time.sleep(0.25)
    else:
        raise RuntimeError(f"restarted API did not become healthy: {last_error}")

    if after["control"] != "paused" or after["vm"] != "paused":
        raise RuntimeError("API restart resumed the fail-closed desktop")
    if after["jobs"] != before["jobs"]:
        raise RuntimeError("API restart changed or replayed durable jobs")

    result = {
        "ok": True,
        "api_pid_changed": True,
        "launchd_state": after_state,
        "jobs_before": len(before["jobs"]),
        "jobs_after": len(after["jobs"]),
        "jobs_unchanged": True,
        "control_after": after["control"],
        "vm_after": after["vm"],
    }
    options.evidence.parent.mkdir(parents=True, exist_ok=True)
    options.evidence.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
