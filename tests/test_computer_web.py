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
