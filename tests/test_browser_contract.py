import pytest
from talos.computer.contract import validate

BASE={"op":"browser","project":"form","key":"fill-1","title":"Fill form","action":"fill","selector":"#email","value":"test@example.invalid"}


@pytest.mark.parametrize("change", [
    {"action":"evaluate","script":"arbitrary javascript"}, {"endpoint":"http://host:9222"},
    {"page":True}, {"page":-1}, {"page":31}, {"frame":[]}, {"selector":""},
    {"value":{}}, {"value":"x\x00"}, {"action":"upload","path":"../key"},
    {"action":"navigate","url":"file:///etc/passwd"},
    {"action":"navigate","url":"https://user:password@example.com"},
])
def test_malformed_browser_requests_cannot_reach_guest(change):
    with pytest.raises((ValueError,TypeError)):validate(BASE|change)


def test_headless_semantic_browser_has_no_desktop_dependency():
    from talos.computer.contract import DESKTOP_OPS
    assert validate(BASE)=='browser' and 'browser' not in DESKTOP_OPS


def test_submission_is_explicit_not_inferred_from_fill():
    assert validate({k:v for k,v in BASE.items() if k!='value'}|{'action':'submit'})=='browser'
    with pytest.raises(ValueError):validate(BASE|{'submit':True})
