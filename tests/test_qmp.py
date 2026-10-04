"""Peer authentication and uncertain-response handling against a real local socket."""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading

import pytest

from talos.computer.qmp import LocalQMP


@pytest.fixture
def endpoint():
    directory = Path(tempfile.mkdtemp(prefix="talos-qmp-")).resolve()
    path = directory / "qmp.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    path.chmod(0o600)
    server.listen(1)
    server.settimeout(2)
    threads = []
    def start(respond):
        def serve():
            try:
                connection, _ = server.accept()
                with connection, connection.makefile("rb") as stream:
                    connection.sendall(b'{"QMP":{}}\n')
                    for line in stream:
                        req = json.loads(line)
                        if req["execute"] == "qmp_capabilities":
                            connection.sendall(json.dumps({"return": {}, "id": req["id"]}).encode() + b"\n")
                        else:
                            response = respond(req)
                            if response is None:
                                return
                            connection.sendall(response)
            except (OSError, ValueError):
                pass
        thread = threading.Thread(target=serve, daemon=True)
        threads.append(thread)
        thread.start()
    yield path, start
    server.close()
    for thread in threads:
        thread.join(3)
    path.unlink(missing_ok=True)
    directory.rmdir()


def test_real_peer_and_interleaved_event(endpoint):
    path, start = endpoint
    start(lambda req: b'{"event":"RESUME"}\n' + json.dumps({"return": {"running": True}, "id": req["id"]}).encode() + b"\n")
    qmp = LocalQMP(path, pid=os.getpid())
    try:
        assert qmp("query-status") == {"running": True}
    finally:
        qmp.close()


def test_wrong_process_is_rejected_before_command(endpoint):
    path, start = endpoint
    start(lambda req: pytest.fail("untrusted peer was sent a command"))
    with pytest.raises(ValueError, match="pinned process"):
        LocalQMP(path, pid=os.getpid() + 1)


@pytest.mark.parametrize("response", [
    lambda req: b'{"return":{},"id":-1}\n',
    lambda req: json.dumps({"error": {"desc": "failed"}, "id": req["id"]}).encode() + b"\n",
    lambda req: b'[]\n',
    lambda req: b'{"event":"NOISE"}\n' * 65,
    lambda req: b'x' * 262145,
])
def test_uncertain_reply_closes_session_without_retry(endpoint, response):
    path, start = endpoint
    start(response)
    qmp = LocalQMP(path, pid=os.getpid())
    with pytest.raises((ValueError, RuntimeError)):
        qmp("input-send-event")
    with pytest.raises(RuntimeError, match="explicit reattachment"):
        qmp("input-send-event")


def test_uncertain_reply_notifies_fail_stop_exactly_once(endpoint):
    path, start = endpoint
    start(lambda _req: b'{"return":{},"id":-1}\n')
    disconnects = []
    qmp = LocalQMP(path, pid=os.getpid(),
                   on_disconnect=lambda: disconnects.append("uncertain"))
    with pytest.raises(RuntimeError, match="identity changed"):
        qmp("input-send-event")
    with pytest.raises(RuntimeError, match="explicit reattachment"):
        qmp("query-status")
    assert disconnects == ["uncertain"]


def test_eof_notifies_once_and_explicit_close_does_not_notify(endpoint):
    path, start = endpoint
    start(lambda _req: None)
    eof = []
    qmp = LocalQMP(path, pid=os.getpid(),
                   on_disconnect=lambda: eof.append(qmp.closed))
    with pytest.raises(ValueError, match="incomplete"):
        qmp("query-status")
    with pytest.raises(RuntimeError, match="explicit reattachment"):
        qmp("query-status")
    assert eof == [True]

    start(lambda req: json.dumps({"return": {}, "id": req["id"]}).encode() + b"\n")
    explicit = []
    healthy = LocalQMP(path, pid=os.getpid(),
                       on_disconnect=lambda: explicit.append(True))
    healthy.close()
    assert explicit == []


def test_world_accessible_socket_refused(endpoint):
    path, _ = endpoint
    path.chmod(0o666)
    with pytest.raises(ValueError, match="private"):
        LocalQMP(path, pid=os.getpid())


def test_world_accessible_parent_refused(endpoint):
    path, _ = endpoint
    path.parent.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        LocalQMP(path, pid=os.getpid())


def test_symlink_endpoint_refused(endpoint):
    path, _ = endpoint
    link = path.parent / "alias.sock"
    link.symlink_to(path)
    try:
        with pytest.raises(ValueError, match="canonical"):
            LocalQMP(link, pid=os.getpid())
    finally:
        link.unlink()
