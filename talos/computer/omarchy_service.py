"""macOS service boundary for the headless Omarchy Computer backend.

QEMU, its disk, raw QMP socket and durable state belong to a dedicated no-login
service identity. The ordinary Talos process can reach only ``control.sock``;
model arguments never select a process, path, socket or backend.
"""
from __future__ import annotations

import binascii
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import struct
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit
import zlib

from .omarchy import OfflineDesktop, SIZE
from .qmp import LocalQMP

DEFAULT_CONFIG = Path("/Library/Application Support/TalosOmarchy/config.json")
MAX_PPM = SIZE[0] * SIZE[1] * 3 + 4096
MAX_PNG = 5 * 1024 * 1024
PREVIEW_LIMIT = 12
STARTUP_TIMEOUT = 90


def _inside(path, root):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or not path.is_relative_to(root):
        raise ValueError("Omarchy service paths must be canonical and service-owned")
    return path


def load_config(path=DEFAULT_CONFIG):
    path = Path(path)
    value = json.loads(path.read_text())
    required = {"root", "owner", "agent_uid", "client_gid", "qmp", "pid_file",
                "disk", "state", "captures", "scratch", "control_socket", "view_url",
                "origin", "view_secret", "viewer", "web_state"}
    if set(value) != required:
        raise ValueError("Omarchy service configuration has unknown or missing fields")
    root = Path(value["root"])
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError("Omarchy service root must be canonical")
    for name in {"qmp", "pid_file", "disk", "state", "captures", "scratch",
                 "control_socket", "web_state"}:
        value[name] = str(_inside(value[name], root))
    if not re.fullmatch(r"[a-f0-9]{64}", str(value["owner"])):
        raise ValueError("owner must be a SHA-256 identity binding")
    if type(value["agent_uid"]) is not int or value["agent_uid"] < 1:
        raise ValueError("agent UID is invalid")
    if type(value["client_gid"]) is not int or value["client_gid"] < 1:
        raise ValueError("client GID is invalid")
    parsed = urlsplit(value["view_url"]) if isinstance(value["view_url"], str) else None
    local = (parsed is not None and parsed.scheme == "http"
             and parsed.hostname in {"127.0.0.1", "::1"})
    remote = parsed is not None and parsed.scheme == "https" and bool(parsed.hostname)
    if (not (local or remote) or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("view URL must use HTTPS or exact loopback HTTP")
    if value["origin"] != value["view_url"].rstrip("/"):
        raise ValueError("workbench origin must match its public view URL")
    if value["viewer"] != "snapshot":
        raise ValueError("Omarchy uses the snapshot workbench")
    if not isinstance(value["view_secret"], str) or len(value["view_secret"]) < 32:
        raise ValueError("workbench secret is invalid")
    return value


def peer_uid(connection):
    if sys.platform.startswith("linux"):
        _, uid, _ = struct.unpack("3i", connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return uid
    if sys.platform == "darwin":
        uid = ctypes.c_uint()
        gid = ctypes.c_uint()
        function = ctypes.CDLL(None, use_errno=True).getpeereid
        function.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
                             ctypes.POINTER(ctypes.c_uint)]
        function.restype = ctypes.c_int
        if function(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)):
            raise OSError(ctypes.get_errno(), "getpeereid failed")
        return uid.value
    raise RuntimeError("peer authentication is not implemented on this host")


def _chunk(name, data):
    return (struct.pack(">I", len(data)) + name + data +
            struct.pack(">I", binascii.crc32(name + data) & 0xffffffff))


def ppm_to_png(raw):
    """Convert QEMU's bounded P6 framebuffer without a mutable image dependency."""
    if not isinstance(raw, bytes) or len(raw) > MAX_PPM:
        raise ValueError("QMP framebuffer exceeds its limit")
    position = 0

    def token():
        nonlocal position
        while position < len(raw):
            if raw[position] == 35:
                end = raw.find(b"\n", position)
                if end < 0:
                    raise ValueError("invalid PPM comment")
                position = end + 1
            elif raw[position] in b" \t\r\n":
                position += 1
            else:
                break
        start = position
        while position < len(raw) and raw[position] not in b" \t\r\n#":
            position += 1
        if start == position:
            raise ValueError("invalid PPM header")
        return raw[start:position]

    if token() != b"P6":
        raise ValueError("QMP framebuffer is not a P6 image")
    try:
        width, height, maximum = int(token()), int(token()), int(token())
    except ValueError as error:
        raise ValueError("invalid PPM dimensions") from error
    if (width, height, maximum) != (*SIZE, 255):
        raise ValueError("QMP framebuffer must be 1440x900 RGB")
    if position >= len(raw) or raw[position] not in b" \t\r\n":
        raise ValueError("invalid PPM pixel delimiter")
    if raw[position:position + 2] == b"\r\n":
        position += 2
    else:
        position += 1
    pixels = raw[position:]
    if len(pixels) != width * height * 3:
        raise ValueError("QMP framebuffer has incomplete pixels")
    stride = width * 3
    scanlines = b"".join(b"\x00" + pixels[offset:offset + stride]
                         for offset in range(0, len(pixels), stride))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) +
           _chunk(b"IDAT", zlib.compress(scanlines, 6)) + _chunk(b"IEND", b""))
    if len(png) > MAX_PNG:
        raise ValueError("encoded framebuffer exceeds its limit")
    return png


def framebuffer_visible(png):
    """Reject a correctly sized but still blank first-boot framebuffer."""
    if not isinstance(png, bytes) or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    position, payload = 8, []
    while position + 12 <= len(png):
        size = struct.unpack(">I", png[position:position + 4])[0]
        name = png[position + 4:position + 8]
        value = png[position + 8:position + 8 + size]
        if position + 12 + size > len(png):
            return False
        position += 12 + size
        if name == b"IDAT":
            payload.append(value)
        elif name == b"IEND":
            break
    try:
        rows = zlib.decompress(b"".join(payload))
    except zlib.error:
        return False
    stride = SIZE[0] * 3
    if len(rows) != (stride + 1) * SIZE[1]:
        return False
    visible = 0
    for offset in range(0, len(rows), stride + 1):
        if rows[offset] != 0:
            return False
        visible += sum(value > 40 for value in rows[offset + 1:offset + stride + 1])
        if visible >= 100_000:
            return True
    return False


class Capture:
    def __init__(self, qmp, scratch, *, settle=0.2):
        self.qmp = qmp
        self.scratch = Path(scratch)
        self.settle = settle
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            path = self.scratch / ("frame-" + uuid.uuid4().hex + ".ppm")
            try:
                if self.settle:
                    time.sleep(self.settle)
                self.qmp("screendump", {"filename": str(path), "format": "ppm"})
                props = path.lstat()
                if not path.is_file() or path.is_symlink() or props.st_uid != os.getuid():
                    raise ValueError("QMP framebuffer path is not service-owned")
                return ppm_to_png(path.read_bytes())
            finally:
                path.unlink(missing_ok=True)


def make_preflight(qmp, disk):
    expected = sorted([(6966, 8), (6900, 4097), (6900, 4176),
                       (6900, 4178), (6900, 4178), (6900, 4101), (6900, 4099)])

    def check():
        devices = [device for bus in qmp("query-pci") for device in bus["devices"]]
        observed = sorted((device["id"]["vendor"], device["id"]["device"])
                          for device in devices)
        if observed != expected or any(device["class_info"]["class"] >> 8 == 2
                                       for device in devices):
            raise ValueError("unexpected VM device or network adapter")
        if {item["label"] for item in qmp("query-chardev")} != {"console", "compat_monitor0"}:
            raise ValueError("unexpected host bridge channel")
        blocks = qmp("query-block")
        if (len(blocks) != 1 or blocks[0]["inserted"]["file"] != str(disk)
                or blocks[0]["inserted"].get("ro") is not False):
            raise ValueError("unexpected or read-only VM disk")
        mice = qmp("query-mice")
        if len(mice) != 1 or not mice[0].get("absolute"):
            raise ValueError("absolute guest pointer is unavailable")
    return check


def wait_for_framebuffer(qmp, capture, *, timeout=STARTUP_TIMEOUT,
                         clock=time.monotonic, sleep=time.sleep):
    """Let a fresh guest finish booting before the fail-closed initial pause.

    On service restart the durable state already exists, so callers skip this
    function and preserve the paused/stopped state without a transient resume.
    """
    deadline = clock() + timeout
    last = None
    while clock() < deadline:
        try:
            status = qmp("query-status").get("status")
            if status == "paused":
                qmp("cont")
            elif status != "running":
                raise ValueError("Omarchy guest is not running during first boot")
            image = capture()
            if not framebuffer_visible(image):
                raise ValueError("Omarchy framebuffer is still blank during first boot")
            return image
        except (OSError, RuntimeError, TimeoutError, ValueError) as error:
            last = error
            sleep(0.5)
    raise TimeoutError("Omarchy framebuffer did not become 1440x900 before timeout") from last


class OmarchyComputer:
    def __init__(self, config, *, qmp=None, capture=None, check_vm=None):
        self.config = config
        pid = int(Path(config["pid_file"]).read_text().strip()) if qmp is None else None
        self.qmp = qmp or LocalQMP(config["qmp"], pid=pid)
        self.capture_source = capture or Capture(self.qmp, config["scratch"])
        self.check_vm = check_vm or make_preflight(self.qmp, config["disk"])
        state_db = Path(config["state"]) / "jobs.db"
        if not state_db.exists():
            self.check_vm()
            wait_for_framebuffer(self.qmp, self.capture_source)
        self.desktop = OfflineDesktop(
            root=config["state"], capture_root=config["captures"], capture_mode=0o640,
            owner=config["owner"], qmp=self.qmp, capture=self.capture_source,
            check_vm=self.check_vm)
        self.preview_lock = threading.Lock()

    def status(self):
        value = self.desktop.read({"op": "status"})
        value["vm"] = value["vm"]["status"]
        value.update(desktop=True, mode="desktop", viewer="snapshot",
                     view_url=self.config["view_url"], routines=[])
        return value

    def preview(self):
        raw = self.capture_source()
        with self.preview_lock:
            folder = Path(self.config["captures"])
            path = folder / ("preview-" + uuid.uuid4().hex + ".png")
            with path.open("xb") as stream:
                stream.write(raw)
            path.chmod(0o640)
            previews = sorted(folder.glob("preview-*.png"), key=lambda item: item.stat().st_mtime)
            for old in previews[:-PREVIEW_LIMIT]:
                old.unlink()
        return {"image_path": str(path), "captured_at": time.time(),
                "sha256": hashlib.sha256(raw).hexdigest()}

    def screenshot(self, args):
        """Give the agent read access to this receipt, never to web previews."""
        receipt = self.desktop.read(args)
        path = Path(receipt["image_path"])
        if path.parent != Path(self.config["captures"]) or path.is_symlink():
            path.unlink(missing_ok=True)
            raise ValueError("invalid agent capture path")
        os.chown(path, -1, self.config["client_gid"])
        path.chmod(0o640)
        return receipt

    def handle(self, frame, uid):
        if not isinstance(frame, dict) or set(frame) - {"kind", "owner", "args"}:
            raise ValueError("unknown service frame")
        human = uid in {0, os.getuid()}
        if uid not in {0, os.getuid(), self.config["agent_uid"]}:
            raise ValueError("peer is not authorized")
        kind = frame.get("kind")
        if not human and frame.get("owner") != self.config["owner"]:
            raise ValueError("identity does not own this computer")
        args = frame.get("args")
        if not isinstance(args, dict):
            raise ValueError("computer arguments must be an object")
        if kind == "human":
            if not human:
                raise ValueError("human control requires the trusted workbench")
            op = args.get("op")
            if op in {"takeover", "pause", "stop", "release"} and set(args) == {"op"}:
                state = {"takeover": "human", "pause": "paused",
                         "stop": "stopped", "release": "agent"}[op]
                self.desktop.control(state, human=True)
                return self.status()
            if op == "input" and set(args) == {"op", "input"}:
                return self.desktop.human_input(args["input"])
            raise ValueError("unknown human operation")
        if kind == "action":
            return self.desktop.submit(args)
        if kind != "read":
            raise ValueError("unknown request kind")
        if args == {"op": "preview"}:
            if not human:
                raise ValueError("preview requires the trusted workbench")
            return self.preview()
        if args.get("op", "status") == "status":
            return self.status()
        if args.get("op") == "screenshot":
            return self.screenshot(args)
        if args.get("op") == "job":
            return self.desktop.read(args)
        raise ValueError("Omarchy Computer does not expose host or guest files")


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(15)
        try:
            raw = self.rfile.readline(40001)
            if len(raw) > 40000 or not raw.endswith(b"\n"):
                raise ValueError("request too large or incomplete")
            result = self.server.computer.handle(json.loads(raw), peer_uid(self.connection))
        except Exception as error:
            result = {"error": str(error)[:180]}
        self.wfile.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")


class SocketServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def connect_computer(config, *, timeout=30, factory=OmarchyComputer,
                     clock=time.monotonic, sleep=time.sleep):
    """Wait only for the fixed local QEMU socket; never retry a rejected peer/config."""
    deadline = clock() + timeout
    last = None
    while clock() < deadline:
        try:
            return factory(config)
        except (FileNotFoundError, ConnectionRefusedError) as error:
            last = error
            sleep(0.1)
    raise TimeoutError("fixed Omarchy VM endpoint did not appear before timeout") from last


def main():
    config_path = Path(os.environ.get("TALOS_OMARCHY_CONFIG", str(DEFAULT_CONFIG)))
    config = load_config(config_path)
    computer = connect_computer(config)
    path = Path(config["control_socket"])
    path.unlink(missing_ok=True)
    server = SocketServer(str(path), Handler)
    server.computer = computer
    os.chown(path, os.getuid(), config["client_gid"])
    path.chmod(0o660)
    server.serve_forever()


if __name__ == "__main__":
    main()
