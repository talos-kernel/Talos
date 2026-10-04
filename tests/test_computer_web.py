"""Workbench session authenticity and shared login throttle.

Diese Faelle liefen bis zum 12.09. nirgends: `pytest.importorskip("websockify")` stand
ueber jedem, und die Bibliothek gehoert dem Computer-Host, nicht dem Agenten-venv. Damit
war ausgerechnet die Sitzungssignatur und die Login-Bremse ueberall ungeprueft. Der
Import in `talos/computer/web.py` ist jetzt traege — nur der Proxy braucht ihn —, also
laufen sie hier ohne Ausnahme.
"""
from io import BytesIO
import json
from unittest.mock import Mock

import pytest


class Headers(dict):
    def get(self, name, default=None):
        return super().get(name, default)


def post_handler(web, *, path="/api/input", body=None, origin=None,
                 authenticated=True):
    raw = json.dumps(body if body is not None else {}).encode()
    handler = object.__new__(web.SnapshotHandler)
    handler.path = path
    handler.headers = Headers({
        "Origin": origin if origin is not None else web.CONFIG["origin"],
        "Content-Type": "application/json",
        "Content-Length": str(len(raw)),
    })
    handler.rfile = BytesIO(raw)
    handler.authenticated = lambda: authenticated
    response = []
    handler.respond = lambda *args, **kwargs: response.append((args, kwargs))
    return handler, response

def test_signed_session_rejects_tampering_wrong_secret_and_expiry():
    from talos.computer.web import COOKIE,make_cookie,valid_cookie
    cookie=COOKIE+"="+make_cookie("test-secret",now=1000)
    assert valid_cookie(cookie,"test-secret",now=1001)
    assert not valid_cookie(cookie+"x","test-secret",now=1001)
    assert not valid_cookie(cookie,"wrong-secret",now=1001)
    assert not valid_cookie(cookie,"test-secret",now=30000)
    assert not valid_cookie("broken","test-secret",now=1001)

def test_login_rate_limit_survives_separate_database_connections(tmp_path):
    from talos.computer.web import login_allowed
    path=tmp_path/"login.db"
    assert all(login_allowed(path,1000) for _ in range(8))
    assert not login_allowed(path,1001)
    assert login_allowed(path,1061)


@pytest.mark.parametrize("handler_name", ["Handler", "SnapshotHandler"])
def test_login_rate_limit_429_sends_retry_after_window(tmp_path, monkeypatch,
                                                        handler_name):
    from talos.computer import web
    path = tmp_path / "login.db"
    assert all(web.login_allowed(path, 1000) for _ in range(8))
    monkeypatch.setattr(web, "CONFIG", {
        "origin": "https://computer.example.test", "view_secret": "s" * 32})
    monkeypatch.setattr(web, "LOGIN_DB", path)
    monkeypatch.setattr(web.time, "time", lambda: 1001)

    raw = json.dumps({"token": "s" * 32}).encode()
    handler = object.__new__(getattr(web, handler_name))
    handler.path = "/api/login"
    handler.headers = Headers({
        "Origin": web.CONFIG["origin"],
        "Content-Type": "application/json",
        "Content-Length": str(len(raw)),
    })
    handler.rfile = BytesIO(raw)
    handler.wfile = BytesIO()
    statuses = []
    headers = []
    handler.send_response = statuses.append
    handler.send_header = lambda name, value: headers.append((name, value))
    handler.end_headers = lambda: None

    handler.do_POST()

    assert statuses == [429]
    assert ("Retry-After", "60") in headers
    assert json.loads(handler.wfile.getvalue()) == {"error": "Bitte warte eine Minute."}

@pytest.mark.parametrize('running', [False, True])
def test_dashboard_reads_preview_without_consuming_agent_capture_pool(tmp_path, monkeypatch, running):
    from talos.computer import web
    image = tmp_path/'preview-fixture.png'
    image.write_bytes(b'preview fixture')
    calls = []
    def rpc(kind, args):
        calls.append((kind, args))
        if args['op'] == 'status':
            return {'vm':'running' if running else 'paused'}
        assert args == {'op':'preview'}
        return {'image_path':str(image)}
    monkeypatch.setattr(web, 'CAPTURES', tmp_path)
    monkeypatch.setattr(web, 'rpc', rpc)
    handler = object.__new__(web.Handler)
    handler.path = '/api/screen'
    handler.authenticated = lambda: True
    response = []
    handler.respond = lambda *args: response.append(args)
    handler.do_GET()
    assert response == [(200, b'preview fixture', 'image/png')]
    assert calls == [('read', {'op':'status'})] + ([('read', {'op':'preview'})] if running else [])


def test_boot_frame_without_final_geometry_is_not_reported_as_server_failure(monkeypatch):
    from talos.computer import web
    def rpc(_kind, args):
        if args == {"op": "status"}:
            return {"vm": "running"}
        raise ValueError("QMP framebuffer must be 1440x900 RGB")
    monkeypatch.setattr(web, "rpc", rpc)
    handler = object.__new__(web.SnapshotHandler)
    handler.path = "/api/screen"
    handler.authenticated = lambda: True
    response = []
    handler.respond = lambda *args: response.append(args)
    handler.do_GET()
    assert response == [(204, b"", "image/png")]


def test_snapshot_input_requires_origin_session_and_human_rpc(monkeypatch):
    from talos.computer import web
    monkeypatch.setattr(web, "CONFIG", {"origin": "https://computer.example.test",
                                        "view_secret": "s" * 32})
    calls = []
    monkeypatch.setattr(web, "rpc", lambda kind, args: calls.append((kind, args)) or {
        "input": "dispatched", "control": "human"})

    handler, response = post_handler(web, body={"op": "click", "x": 4, "y": 8})
    handler.do_POST()
    assert response == [((200, {"input": "dispatched", "control": "human"}), {})]
    assert calls == [("human", {"op": "input", "input": {
        "op": "click", "x": 4, "y": 8}})]

    handler, response = post_handler(web, body={"op": "click", "x": 4, "y": 8},
                                     origin="https://evil.example")
    handler.do_POST()
    assert response == [((403, {"error": "origin refused"}), {})]
    assert len(calls) == 1

    handler, response = post_handler(web, body={"op": "click", "x": 4, "y": 8},
                                     authenticated=False)
    handler.do_POST()
    assert response == [((401, {"error": "Anmeldung erforderlich."}), {})]
    assert len(calls) == 1


def test_snapshot_input_maps_broken_service_stream_to_503(monkeypatch):
    from talos.computer import web
    monkeypatch.setattr(web, "CONFIG", {"origin": "https://computer.example.test",
                                        "view_secret": "s" * 32})
    monkeypatch.setattr(web, "rpc", Mock(side_effect=ConnectionError("service ended")))
    handler, response = post_handler(web, body={"op": "key", "keys": "Return"})
    handler.do_POST()
    assert response == [((503, {"error": "Computer gerade nicht erreichbar."}), {})]


def test_snapshot_input_rejects_oversized_or_non_object_body(monkeypatch):
    from talos.computer import web
    monkeypatch.setattr(web, "CONFIG", {"origin": "https://computer.example.test",
                                        "view_secret": "s" * 32})
    monkeypatch.setattr(web, "rpc", lambda *_: pytest.fail("invalid input reached RPC"))

    handler, response = post_handler(web, body=["not", "an", "object"])
    handler.do_POST()
    assert response == [((400, {"error": "invalid request"}), {})]

    handler, response = post_handler(web, body={"op": "type", "text": "x"})
    handler.headers["Content-Length"] = "2001"
    handler.do_POST()
    assert response == [((413, {"error": "request too large"}), {})]


def test_snapshot_frontend_serializes_input_and_is_valid_javascript():
    from pathlib import Path
    import shutil
    import subprocess

    source = Path(__file__).parents[1] / "talos" / "computer" / "web" / "app.js"
    text = source.read_text()
    assert "snapshotInputQueue=result.then" in text
    assert 'api("/api/input",input)' in text
    assert "reportEvents>63" in text and "one bounded keyboard batch" in text
    assert "response.status===204" in text
    assert "snapshotViewer()||(desktopConnected&&rfb)" in text
    assert 'state.backend==="omarchy"' in text
    assert "No inbound forwarding, host files, clipboard or credentials." in text
    html = source.with_name("index.html").read_text()
    assert 'id="intro-copy"' in html and 'id="bottom-grid"' in html
    if node := shutil.which("node"):
        subprocess.run([node, "--check", str(source)], check=True,
                       capture_output=True, text=True)
