"""Private computer service. Agent requests and human control have distinct peer roles."""
import base64
import json
import os
from pathlib import Path
import select
import socket
import socketserver
import struct
import subprocess
import threading
import time
import uuid

from .contract import validate, DESKTOP_OPS
from .state import Store

CONFIG = Path("/etc/talos-computer.json")
DATA = Path("/var/lib/talos-computer")
RUNTIME = Path("/run/talos-computer-api")
CAPTURES = Path("/var/lib/talos-computer-captures")


def qmp(command):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(4)
        sock.connect("/run/talos-computer-vm/qmp.sock")
        stream = sock.makefile("rwb")
        json.loads(stream.readline())
        for op in ("qmp_capabilities", command):
            stream.write(json.dumps({"execute": op}).encode() + b"\n"); stream.flush()
            while True:
                reply = json.loads(stream.readline())
                if "error" in reply:
                    raise RuntimeError("virtual machine control unavailable")
                if "return" in reply:
                    break
        return reply["return"]


def ssh_argv(command):
    argv = ["ssh", "-T", "-p", "22241", "-i", str(DATA / "desk_key"),
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=yes", "-o", "HostKeyAlias=talos-computer",
            "-o", f"UserKnownHostsFile={DATA / 'known_hosts'}",
            "desk@127.0.0.1", "python3 /opt/talos/guest.py"]
    argv[-1] = command
    return argv


def decode_guest(raw):
    if len(raw) > 2200000:
        raise RuntimeError("guest receipt exceeds limit")
    if raw.startswith(b"TALOS_READY\n"):
        raw = raw[len(b"TALOS_READY\n"):]
    data = json.loads(raw)
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data


def guest(args, timeout=135):
    result = subprocess.run(ssh_argv("python3 /opt/talos/guest.py"),
                            input=json.dumps(args, ensure_ascii=False).encode() + b"\n",
                            capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("guest action unavailable; inspect before retrying")
    return decode_guest(result.stdout)


def cancel_units():
    command = "XDG_RUNTIME_DIR=/run/user/1000 systemctl --user stop 'talos-action-*'"
    result = subprocess.run(ssh_argv(command), capture_output=True, timeout=12)
    if result.returncode:
        raise RuntimeError("could not confirm job cancellation; computer remains paused")


class Computer:
    def __init__(self, config):
        self.config = config
        self.store = Store(DATA / "jobs.db")
        self.lock = threading.RLock()
        self.store.recover()
        # The host service cannot silently resume agent activity after a crash.
        qmp("stop")

    def require_desktop(self, op):
        if not self.config.get("desktop", True) and op in DESKTOP_OPS | {"takeover"}:
            raise ValueError("desktop is disabled; use exec, files and job receipts in headless mode")

    def status(self, owner):
        return {"desktop": self.config.get("desktop", True),
                "mode": "desktop" if self.config.get("desktop", True) else "headless",
                "control": self.store.control(), "vm": qmp("query-status")["status"],
                "jobs": self.store.jobs(owner), "view_url": self.config["view_url"],
                "routines": self.store.routines(owner)}

    def capture(self):
        self.require_desktop("screenshot")
        data = guest({"op": "screenshot"}, timeout=12)
        filename = "screen-" + uuid.uuid4().hex + ".png"
        path = CAPTURES / filename
        raw = base64.b64decode(data["png"], validate=True)
        if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) > 1500000:
            raise RuntimeError("invalid desktop capture")
        path.write_bytes(raw)
        path.chmod(0o640)
        for old in sorted(CAPTURES.glob("screen-*.png"), key=lambda p: p.stat().st_mtime)[:-12]:
            old.unlink()
        return {"image_path": str(path), "captured_at": data["captured_at"]}

    def action(self, owner, args):
        op = validate(args)
        self.require_desktop(op)
        with self.lock:
            if op in {"pause", "resume", "stop"}:
                state = {"pause": "paused", "resume": "agent", "stop": "stopped"}[op]
                if self.store.control() == "human" and op == "resume":
                    raise ValueError("the operator owns the desktop; only they can return control")
                return self.control(state)
            if self.store.control() != "agent":
                raise ValueError("computer input is paused or owned by the operator")
            job, created = self.store.begin(owner, args)
            if created:
                threading.Thread(target=self.run, args=(job["id"], args), daemon=True).start()
            return {"job": job, "reused": not created}

    def control(self, state):
        with self.lock:
            self.store.control("paused")
            try:
                qmp("cont")
                cancel_units()
                if state in {"paused", "stopped"}:
                    qmp("stop")
            except Exception:
                qmp("stop")
                raise
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET state='interrupted',updated=? WHERE state IN ('queued','running')", (time.time(),))
            self.store.control(state)
            return {"control": state}

    def run(self, job_id, args):
        proc = None
        try:
            with self.lock:
                if (self.store.control() != "agent"
                        or self.store.get(self.config["owner"], job_id)["state"] != "queued"):
                    raise RuntimeError("computer is no longer available to the agent")
                self.store.finish(job_id, "running")
                # READY is emitted from INSIDE the transient unit before its payload
                # is read. Human takeover can therefore cancel a known cgroup,
                # never race a not-yet-started remote command.
                command = ("XDG_RUNTIME_DIR=/run/user/1000 systemd-run --user --quiet "
                           f"--unit=talos-action-{job_id} --collect --wait --pipe "
                           "-p KillMode=control-group -p TasksMax=128 -p MemoryMax=1G "
                           f"-p RuntimeMaxSec={args.get('timeout', 60) + 10}s "
                           "-- /usr/bin/python3 /opt/talos/guest.py")
                proc = subprocess.Popen(ssh_argv(command), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                ready, _, _ = select.select([proc.stdout], [], [], 10)
                if not ready or proc.stdout.readline() != b"TALOS_READY\n":
                    raise RuntimeError("guest unit did not become ready")
                proc.stdin.write(json.dumps(args, ensure_ascii=False).encode() + b"\n")
                proc.stdin.close(); proc.stdin = None
            stdout, _ = proc.communicate(timeout=135)
            if proc.returncode:
                raise RuntimeError("guest unit interrupted")
            result = decode_guest(stdout)
            with self.lock:
                if (self.store.control() != "agent"
                        or self.store.get(self.config["owner"], job_id)["state"] != "running"):
                    raise RuntimeError("operator interrupted the job")
                self.store.finish(job_id, result.pop("state"), result)
        except Exception:
            self.store.finish(job_id, "interrupted", {"verification": "no reliable terminal receipt; inspect before retrying"})
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill(); proc.wait()

    def handle(self, frame, uid):
        kind = frame.get("kind")
        human = uid in {0, os.getuid()}
        if uid not in {0, os.getuid(), self.config["agent_uid"]}:
            raise ValueError("peer is not authorized")
        owner = self.config["owner"]
        if not human and frame.get("owner") != owner:
            raise ValueError("identity does not own this computer")
        args = frame.get("args", {})
        if kind == "human":
            if not human:
                raise ValueError("human control requires the trusted web service")
            if set(args) - {"op", "job_id", "name"}:
                raise ValueError("unknown control field")
            op = args.get("op")
            self.require_desktop(op)
            with self.lock:
                if op in {"takeover", "pause", "stop", "release"}:
                    self.control({"takeover": "human", "pause": "paused", "stop": "stopped", "release": "agent"}[op])
                elif op == "save_routine":
                    self.store.save_routine(owner, args.get("job_id"), args.get("name"))
                else:
                    raise ValueError("unknown human operation")
            return self.status(owner)
        if kind == "action":
            return self.action(owner, args)
        if kind != "read":
            raise ValueError("unknown request kind")
        op = validate(args, read=True)
        if op == "status":
            return self.status(owner)
        if op == "job":
            return self.store.get(owner, args["job_id"])
        if op == "routine":
            return self.store.routine(owner, args["name"])
        if op == "routines":
            return {"routines": self.store.routines(owner)}
        if op == "screenshot":
            return self.capture()
        return guest(args, timeout=12)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(15)
        try:
            raw = self.rfile.readline(40001)
            if len(raw) > 40000 or not raw.endswith(b"\n"):
                raise ValueError("request too large or incomplete")
            _, uid, _ = struct.unpack("3i", self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            result = self.server.computer.handle(json.loads(raw), uid)
        except Exception as error:
            result = {"error": str(error)[:180]}
        self.wfile.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")


class SocketServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class VNC(socketserver.BaseRequestHandler):
    def handle(self):
        if not self.server.computer.config.get("desktop", True):
            return
        with socket.create_connection(("127.0.0.1", 5901), timeout=8) as remote:
            while True:
                ready, _, _ = select.select([self.request, remote], [], [], 0.5)
                if self.server.computer.store.control() != "human":
                    return
                if not ready:
                    continue
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    with self.server.computer.lock:
                        if self.server.computer.store.control() != "human":
                            return
                        (remote if source is self.request else self.request).sendall(data)


def main():
    config = json.loads(CONFIG.read_text())
    computer = Computer(config)
    for name, handler in (("control.sock", Handler), ("vnc.sock", VNC)):
        path = RUNTIME / name
        path.unlink(missing_ok=True)
        server = SocketServer(str(path), handler)
        server.computer = computer
        path.chmod(0o660 if name == "control.sock" else 0o600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Event().wait()


if __name__ == "__main__":
    main()
