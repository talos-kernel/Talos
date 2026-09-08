"""Kernel runner transport. No endpoints, identity or host paths from model arguments."""
import hashlib
import json
import os
import socket
from .contract import validate

LIMIT = 1024 * 1024


def call(frame):
    endpoint = os.environ.get("TALOS_COMPUTER_SOCKET", "")
    if not endpoint or not os.path.isabs(endpoint):
        raise RuntimeError("computer is not configured; run talos computer setup")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(12)
        sock.connect(endpoint)
        sock.sendall(json.dumps(frame, ensure_ascii=False).encode() + b"\n")
        data = sock.makefile("rb").readline(LIMIT + 1)
    if len(data) > LIMIT or not data.endswith(b"\n"):
        raise RuntimeError("computer returned an incomplete receipt; inspect status before retrying")
    result = json.loads(data)
    if result.get("error"):
        raise RuntimeError(result["error"])
    return result


def runner(req):
    read = req.tool == "computer_status"
    validate(req.args, read=read)
    owner = hashlib.sha256(str(req.identity).encode()).hexdigest()
    result = call({"kind": "read" if read else "action", "owner": owner, "args": req.args})
    return json.dumps(result, ensure_ascii=False)
