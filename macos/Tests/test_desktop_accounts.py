"""Discovery reports local evidence, never imports credentials or activates tools."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import desktop_accounts as accounts


@pytest.fixture
def installed(tmp_path, monkeypatch):
    locations = {name: str(tmp_path / name) for name in ("claude", "codex", "agy", "kimi", "cline", "hermes")}
    monkeypatch.setattr(accounts.shutil, "which", lambda name, **kwargs: locations.get(name))
    calls = []
    def status(binary, args, env):
        calls.append((Path(binary).name, args, env))
        if Path(binary).name == "claude":
            return 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "PRIVATE", "accessToken": "SECRET"})
        return 0, "Logged in using ChatGPT"
    monkeypatch.setattr(accounts, "_local_status", status)
    return tmp_path, calls


def write(home, name, data):
    path = home / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def test_detects_all_five_without_starting_agents_or_copying_logins(installed):
    home, calls = installed
    (home / ".kimi-code").mkdir()
    (home / ".kimi-code/config.toml").write_text('[providers."managed:kimi-code".oauth]\nstorage="file"\nkey="kimi-code"\n')
    write(home, ".kimi-code/credentials/kimi-code.json", {"access_token": "KIMI_SECRET"})
    write(home, ".cline/data/settings/providers.json", {"providers": {"cline": {"settings": {"auth": {"refreshToken": "CLINE_SECRET"}}}}})
    write(home, ".gemini/antigravity-cli/antigravity-oauth-token", {"refresh_token": "AGY_SECRET"})
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    result = accounts.discover(home, {"PATH": "/usr/bin", "OPENAI_API_KEY": "FOREIGN_SECRET"})
    assert [a["id"] for a in result] == ["claude", "codex", "agy", "kimi", "cline"]
    assert [a["status"] for a in result] == ["signed_in", "signed_in", "login_found", "login_found", "login_found"]
    assert {c[0] for c in calls} == {"claude", "codex"}
    assert all(c[1] in (["auth", "status", "--json"], ["login", "status"]) for c in calls)
    assert all("OPENAI_API_KEY" not in c[2] for c in calls)
    assert before == {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    assert not any(s in json.dumps(result) for s in ("SECRET", "PRIVATE"))
    assert all(not a["action"] for a in result if a["id"] in {"agy", "kimi", "cline"})


def test_missing_clis_do_not_run_status_or_claim_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts.shutil, "which", lambda *args, **kwargs: None)
    monkeypatch.setattr(accounts, "_local_status", lambda *args: pytest.fail("must not launch missing CLI"))
    result = accounts.discover(tmp_path, {})
    assert all(a["status"] == "not_installed" and not a["installed"] for a in result)


@pytest.mark.parametrize("code,text,expected", [
    (0, "Logged in using ChatGPT", "signed_in"),
    (0, "Logged in using an API key", "signed_in"),
    (1, "Not logged in", "signed_out"),
    (0, "Not logged in", "signed_out"),
    (-1, "", "unknown"),
    (1, "private error: token SECRET", "unknown"),
])
def test_codex_status_is_not_inferred_from_installation(installed, monkeypatch, code, text, expected):
    home, _ = installed
    monkeypatch.setattr(accounts, "_local_status", lambda *args: (code, text))
    result = accounts._account("codex", "Codex", home, {})
    assert result["status"] == expected
    assert "SECRET" not in json.dumps(result)
    if "API key" in text:
        assert result["auth_kind"] == "api_key"


@pytest.mark.parametrize("data", ["not JSON", "[]", '{"loggedIn":"yes"}', '{}'])
def test_malformed_claude_status_is_unknown(installed, monkeypatch, data):
    home, _ = installed
    monkeypatch.setattr(accounts, "_local_status", lambda *args: (0, data))
    assert accounts._account("claude", "Claude", home, {})["status"] == "unknown"


def test_signout_and_login_changes_are_reflected_on_next_refresh(installed, monkeypatch):
    home, _ = installed
    monkeypatch.setattr(accounts, "_local_status", lambda *args: (0, '{"loggedIn":false}'))
    assert accounts._account("claude", "Claude", home, {})["status"] == "signed_out"
    monkeypatch.setattr(accounts, "_local_status", lambda *args: (0, '{"loggedIn":true,"authMethod":"claude.ai"}'))
    assert accounts._account("claude", "Claude", home, {})["status"] == "signed_in"


@pytest.mark.parametrize("data", [{}, {"providers": []}, {"providers": {"cline": None}}, {"providers": {"cline": {"settings": []}}}])
def test_unrelated_or_malformed_provider_state_never_claims_oauth(installed, data):
    home, _ = installed
    write(home, ".cline/data/settings/providers.json", data)
    assert accounts._account("cline", "Cline", home, {})["status"] == "unknown"


def test_private_document_rejects_symlinks_oversize_and_nonobjects(tmp_path):
    real = write(tmp_path, "actual.json", {"access_token": "SECRET"})
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert accounts._private_document(link) == {}
    real.write_text('"SECRET"')
    assert accounts._private_document(real) == {}
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    assert accounts._private_document(fifo) == {}
    real.write_text(" " * 262145)
    assert accounts._private_document(real) == {}


def test_known_cli_install_paths_are_available_without_shell_startup(tmp_path):
    path = accounts.cli_path(tmp_path, ".:relative:/custom/bin:/usr/bin")
    assert str(tmp_path / ".kimi-code/bin") in path.split(os.pathsep)
    assert str(tmp_path / ".npm-global/bin") in path.split(os.pathsep)
    assert "." not in path.split(os.pathsep) and "relative" not in path.split(os.pathsep)
    assert path.split(os.pathsep).count("/usr/bin") == 1


def test_status_command_timeout_is_unknown_without_error_leak(monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 4 and kwargs["stdin"] == accounts.subprocess.DEVNULL
        raise accounts.subprocess.TimeoutExpired(args[0], 4, output="SECRET")
    monkeypatch.setattr(accounts.subprocess, "run", timeout)
    assert accounts._local_status("/example/cli", ["login", "status"], {}) == (-1, "")


def test_cli_output_is_not_retained_when_oversize(monkeypatch):
    monkeypatch.setattr(accounts.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="x"*131073, stderr=""))
    assert accounts._local_status("/example/cli", ["login", "status"], {}) == (-1, "")
