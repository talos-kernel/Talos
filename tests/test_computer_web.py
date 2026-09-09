"""Workbench session authenticity and shared login throttle."""
import pytest

def test_signed_session_rejects_tampering_wrong_secret_and_expiry():
    pytest.importorskip("websockify")
    from talos.computer.web import COOKIE,make_cookie,valid_cookie
    cookie=COOKIE+"="+make_cookie("test-secret",now=1000)
    assert valid_cookie(cookie,"test-secret",now=1001)
    assert not valid_cookie(cookie+"x","test-secret",now=1001)
    assert not valid_cookie(cookie,"wrong-secret",now=1001)
    assert not valid_cookie(cookie,"test-secret",now=30000)
    assert not valid_cookie("broken","test-secret",now=1001)

def test_login_rate_limit_survives_separate_database_connections(tmp_path):
    pytest.importorskip("websockify")
    from talos.computer.web import login_allowed
    path=tmp_path/"login.db"
    assert all(login_allowed(path,1000) for _ in range(8))
    assert not login_allowed(path,1001)
    assert login_allowed(path,1061)

@pytest.mark.parametrize('running', [False, True])
def test_dashboard_reads_preview_without_consuming_agent_capture_pool(tmp_path, monkeypatch, running):
    pytest.importorskip('websockify')
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
