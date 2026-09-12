"""Workbench session authenticity and shared login throttle.

Diese Faelle liefen bis zum 12.09. nirgends: `pytest.importorskip("websockify")` stand
ueber jedem, und die Bibliothek gehoert dem Computer-Host, nicht dem Agenten-venv. Damit
war ausgerechnet die Sitzungssignatur und die Login-Bremse ueberall ungeprueft. Der
Import in `talos/computer/web.py` ist jetzt traege — nur der Proxy braucht ihn —, also
laufen sie hier ohne Ausnahme.
"""
import pytest

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
