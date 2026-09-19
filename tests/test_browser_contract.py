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


GOOD_GUARD="a"*32

@pytest.mark.parametrize("expect", [
    "nothex"*5, "A"*32, "a"*31, "a"*33, 42, None, ["a"*32],
])
def test_a_malformed_expect_never_reaches_the_browser(expect):
    with pytest.raises((ValueError,TypeError)):validate(BASE|{"expect":expect})


def test_a_valid_expect_guard_passes_and_inspect_navigate_have_no_target_yet():
    assert validate(BASE|{"expect":GOOD_GUARD})=='browser'
    bare={k:v for k,v in BASE.items() if k not in ('value','selector')}
    with pytest.raises(ValueError):validate(bare|{"action":"inspect","expect":GOOD_GUARD})
    with pytest.raises(ValueError):validate(bare|{"action":"navigate","url":"https://example.com","expect":GOOD_GUARD})


def test_the_guard_is_a_truncated_sha256_that_masks_secret_values():
    from talos.computer.browser import guard
    assert guard("field state")==__import__("hashlib").sha256(b"field state").hexdigest()[:32]
