"""Bounded, peer-pinned QMP session for the visual desktop backend.

One connection per VM lifetime. Never reconnect or replay an uncertain command.
Only operator code constructs this transport; model arguments are not endpoints.
"""
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import threading
import time


class LocalQMP:
    def __init__(self, path, *, pid, on_disconnect=None):
        path = Path(path)
        if type(pid) is not int or pid < 2 or not path.is_absolute() or path.resolve() != path:
            raise ValueError("a fixed canonical VM socket and PID are required")
        if on_disconnect is not None and not callable(on_disconnect):
            raise ValueError("VM disconnect handler must be callable")
        for candidate, predicate in ((path.parent, stat.S_ISDIR), (path, stat.S_ISSOCK)):
            props = candidate.lstat()
            if not predicate(props.st_mode) or props.st_uid != os.getuid() or props.st_mode & 0o077:
                raise ValueError("VM control must be private and owned by the operator")
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.lock = threading.RLock()
        self.counter = 0
        self.closed = False
        self.on_disconnect = on_disconnect
        self.disconnect_notified = False
        self.buffer = b""
        try:
            self.socket.settimeout(4)
            self.socket.connect(str(path))
            if sys.platform == "darwin":
                # Darwin sys/un.h: SOL_LOCAL=0, LOCAL_PEERPID=2.
                actual = struct.unpack("i", self.socket.getsockopt(0, 2, 4))[0]
            elif sys.platform.startswith("linux"):
                actual, _, _ = struct.unpack("3i", self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            else:
                raise RuntimeError("peer authentication is not implemented on this host")
            if actual != pid:
                raise ValueError("VM control peer does not match the pinned process")
            if "QMP" not in self._read(time.monotonic() + 4):
                raise ValueError("VM control did not provide a QMP greeting")
            self("qmp_capabilities")
        except Exception:
            self.close()
            raise

    def close(self):
        self.closed = True
        self.socket.close()

    def _read(self, deadline):
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("VM control response timed out")
            if len(self.buffer) >= 262144:
                raise ValueError("VM control response exceeds its limit")
            self.socket.settimeout(remaining)
            chunk = self.socket.recv(min(8192, 262144 - len(self.buffer)))
            if not chunk:
                raise ValueError("VM control response is incomplete")
            self.buffer += chunk
        raw, self.buffer = self.buffer.split(b"\n", 1)
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("VM control response must be an object")
        return result

    def __call__(self, command, params=None):
        with self.lock:
            if self.closed:
                raise RuntimeError("VM session ended; explicit reattachment required")
            self.counter += 1
            request = {"execute": command, "arguments": params or {}, "id": self.counter}
            try:
                self.socket.sendall(json.dumps(request).encode() + b"\n")
                deadline = time.monotonic() + 4
                for _ in range(64):
                    reply = self._read(deadline)
                    if "event" in reply:
                        continue
                    if reply.get("id") != self.counter or "error" in reply or "return" not in reply:
                        raise RuntimeError("VM command failed or response identity changed; no retry")
                    return reply["return"]
                raise RuntimeError("too many VM events while awaiting response")
            except Exception:
                self.close()
                if self.on_disconnect is not None and not self.disconnect_notified:
                    self.disconnect_notified = True
                    self.on_disconnect()
                raise
