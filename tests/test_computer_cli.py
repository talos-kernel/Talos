"""Computer commands avoid false readiness claims or credential disclosure on status."""
import io
from talos.computer.cli import run_computer
from talos.computer.presentation import entry

def test_unconfigured_status_fails_with_next_step(monkeypatch):
    monkeypatch.delenv("TALOS_COMPUTER_SOCKET",raising=False)
    out=io.StringIO()
    assert run_computer(["status"],out)==1
    assert "talos computer setup" in out.getvalue()

def test_private_link_requires_valid_https_and_configured_key(tmp_path,monkeypatch):
    key=tmp_path/"view.key";key.write_text("test-only-access-value")
    monkeypatch.setenv("TALOS_COMPUTER_SOCKET","/run/example/control.sock")
    monkeypatch.setenv("TALOS_COMPUTER_VIEW_KEY_FILE",str(key))
    monkeypatch.setenv("TALOS_COMPUTER_VIEW_URL","http://example.test")
    assert "test-only" not in entry().text
    monkeypatch.setenv("TALOS_COMPUTER_VIEW_URL","https://example.test")
    assert entry().markdown and "#token=test-only-access-value" in entry().text

def test_status_does_not_print_access_key(tmp_path,monkeypatch):
    key=tmp_path/"view.key";key.write_text("keep-this-private")
    monkeypatch.setenv("TALOS_COMPUTER_VIEW_KEY_FILE",str(key))
    monkeypatch.setenv("TALOS_COMPUTER_SOCKET",str(tmp_path/"missing.sock"))
    out=io.StringIO()
    assert run_computer(["status"],out)==1
    assert "keep-this-private" not in out.getvalue()


def test_chat_link_is_bound_to_owner_private_conversation_and_full_trust(monkeypatch):
    import hashlib
    from types import SimpleNamespace
    from talos.channel import Principal,Trust
    from talos.computer.presentation import chat_authorized
    owner=Principal("telegram","123456789")
    monkeypatch.setenv("TALOS_COMPUTER_OWNER_SHA256",hashlib.sha256(str(owner).encode()).hexdigest())
    full=SimpleNamespace(trust_of=lambda _:Trust.FULL)
    assert chat_authorized(owner,str(owner),full)
    assert not chat_authorized(owner,"telegram:-100987654321",full)
    other=Principal("telegram","987654321")
    assert not chat_authorized(other,str(other),full)
    assert not chat_authorized(owner,str(owner),SimpleNamespace(trust_of=lambda _:Trust.ASK))
    assert not chat_authorized(owner,str(owner),None)
    monkeypatch.delenv("TALOS_COMPUTER_OWNER_SHA256")
    assert not chat_authorized(owner,str(owner),full)
