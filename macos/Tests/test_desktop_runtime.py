"""Desktop boundary tests: fresh identity, private state and the existing kernel."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("desktop_runtime", Path(__file__).parents[1] / "desktop_runtime.py")
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("TALOS_SANDBOX", raising=False)
    bundle = tmp_path / "Resources"
    backend = bundle / "backend"
    backend.mkdir(parents=True)
    (backend / "SOUL.md").write_text("# Public identity\n")
    (backend / "code.py").write_text("VERSION = 1\n")
    (bundle / "backend.sha256").write_text(desktop.backend_digest(backend))
    return bundle, tmp_path / "profile"


def test_first_open_does_not_enable_identity_or_copy_credentials(runtime, monkeypatch):
    bundle, profile = runtime
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "another-profile-secret")
    release = desktop.provision(bundle, profile)
    assert (release / "workspace").resolve() == profile / "workspace"
    assert not (profile / "talos.env").exists()
    assert not desktop.status(profile)["configured"]
    assert profile.stat().st_mode & 0o777 == 0o700
    assert (profile / "SOUL.md").stat().st_mode & 0o777 == 0o600


def test_upgrade_preserves_operator_files_and_workspace(runtime):
    bundle, profile = runtime
    old = desktop.provision(bundle, profile)
    (profile / "SOUL.md").write_text("# My identity")
    (profile / "workspace/project.txt").write_text("real work")
    (profile / "talos.env").write_text("TALOS_MODEL=custom\n")
    (bundle / "backend/code.py").write_text("VERSION = 2\n")
    (bundle / "backend.sha256").write_text(desktop.backend_digest(bundle / "backend"))
    new = desktop.provision(bundle, profile)
    assert old != new and old.is_dir()
    assert (new / "SOUL.md").read_text() == "# My identity"
    assert (new / "workspace/project.txt").read_text() == "real work"
    assert (profile / "talos.env").read_text() == "TALOS_MODEL=custom\n"


def test_relative_work_stays_in_workspace_across_runtime_updates(runtime, monkeypatch):
    bundle, profile = runtime
    monkeypatch.setattr(desktop, "PROFILE", profile)
    monkeypatch.setattr(desktop, "__file__", str(bundle / "desktop_runtime.py"))
    monkeypatch.setattr(desktop.sys, "argv", ["desktop_runtime.py", "status"])
    monkeypatch.syspath_prepend(str(bundle))
    monkeypatch.chdir(bundle)
    monkeypatch.setattr(desktop, "clean_environment", lambda *args: dict(os.environ))
    def create_work(action):
        assert Path.cwd() == profile / "workspace"
        Path("project.txt").write_text("durable work")
        return 0
    monkeypatch.setattr(desktop, "run_action", create_work)
    assert desktop.main() == 0
    (bundle / "backend/code.py").write_text("VERSION = 2\n")
    (bundle / "backend.sha256").write_text(desktop.backend_digest(bundle / "backend"))
    def read_work(action):
        assert Path("project.txt").read_text() == "durable work"
        return 0
    monkeypatch.setattr(desktop, "run_action", read_work)
    assert desktop.main() == 0
    assert not list((profile / "releases").glob("*/project.txt"))


@pytest.mark.parametrize("name", ["workspace", "talos.env", "releases", "SOUL.md", ".install.lock"])
def test_unexpected_profile_symlinks_are_not_followed(runtime, name):
    bundle, profile = runtime
    profile.mkdir()
    outside = profile.parent / "outside"
    outside.mkdir()
    (profile / name).symlink_to(outside)
    with pytest.raises(RuntimeError, match="unexpected path"):
        desktop.provision(bundle, profile)
    assert not list(outside.iterdir())


def test_corrupt_bundle_never_activates_release(runtime):
    bundle, profile = runtime
    (bundle / "backend/code.py").write_text("changed without new manifest")
    with pytest.raises(RuntimeError, match="incomplete"):
        desktop.provision(bundle, profile)
    assert not list((profile / "releases").iterdir())


def test_environment_separates_existing_agent_and_keeps_sandbox(runtime, monkeypatch):
    bundle, profile = runtime
    for name in ("TELEGRAM_BOT_TOKEN", "TALOS_ALLOWED_PRINCIPALS", "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "PYTHONSTARTUP"):
        monkeypatch.setenv(name, "must-not-import")
    monkeypatch.setenv("TALOS_SANDBOX", "1")
    env = desktop.clean_environment(profile, bundle / "backend", bundle / "packages")
    assert "must-not-import" not in env.values()
    assert env["TALOS_SANDBOX"] == "1"
    assert env["TALOS_SECRETS_ENV"] == str(profile / "talos.env")
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    with pytest.raises(RuntimeError, match="sandbox"):
        desktop.provision(bundle, profile)
    assert not profile.exists()


@pytest.mark.parametrize("input_tty,output_tty,sandbox", [(False, True, False), (True, False, False), (True, True, True)])
def test_mutating_actions_require_attended_terminal(monkeypatch, input_tty, output_tty, sandbox):
    monkeypatch.setattr(desktop.sys, "stdin", SimpleNamespace(isatty=lambda: input_tty))
    monkeypatch.setattr(desktop.sys, "stdout", SimpleNamespace(isatty=lambda: output_tty))
    if sandbox:
        monkeypatch.setenv("TALOS_SANDBOX", "1")
    else:
        monkeypatch.delenv("TALOS_SANDBOX", raising=False)
    with pytest.raises(RuntimeError, match="operator terminal"):
        desktop.require_operator_terminal()


def test_quick_connection_only_enables_account_after_success(runtime, monkeypatch):
    import talos.reasoner
    bundle, profile = runtime
    desktop.provision(bundle, profile)
    monkeypatch.setattr(desktop, "require_operator_terminal", lambda: None)
    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/example/claude")
    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout='{"loggedIn":true}', returncode=0))
    answer = {"text": "Reasoner error: unavailable"}
    monkeypatch.setattr(talos.reasoner, "ClaudeCliReasoner", lambda *a, **k: SimpleNamespace(reason=lambda p: answer["text"]))
    with pytest.raises(RuntimeError, match="connection check"):
        desktop.quick_claude(profile)
    assert not (profile / "talos.env").exists()
    answer["text"] = "TALOS_DESKTOP_READY"
    desktop.quick_claude(profile)
    assert desktop.status(profile)["configured"]
    assert (profile / "talos.env").stat().st_mode & 0o777 == 0o600


def test_telegram_setup_preserves_only_previously_enabled_mac_identity(runtime, monkeypatch):
    from talos.configcli import write_keys, read_file
    import talos.setup_wizard
    bundle, profile = runtime
    desktop.provision(bundle, profile)
    identity = f"cli:{os.getuid()}"
    write_keys(profile / "talos.env", {"TALOS_MODEL_PROVIDER": "claude-cli", "TALOS_ALLOWED_PRINCIPALS": f"{identity},telegram:111"})
    monkeypatch.setattr(desktop, "require_operator_terminal", lambda: None)
    def setup(args):
        write_keys(profile / "talos.env", {"TALOS_ALLOWED_PRINCIPALS": "telegram:222", "TELEGRAM_BOT_TOKEN": "telegram-token-never-in-status"})
        return 0
    monkeypatch.setattr(talos.setup_wizard, "run_setup", setup)
    assert desktop.run_action("telegram", profile) == 0
    assert set(read_file(profile / "talos.env")["TALOS_ALLOWED_PRINCIPALS"].split(",")) == {identity, "telegram:222"}
    assert "telegram-token-never-in-status" not in json.dumps(desktop.status(profile))


def test_second_session_is_refused_before_dispatch(runtime, monkeypatch):
    bundle, profile = runtime
    desktop.provision(bundle, profile)
    monkeypatch.setattr(desktop, "require_operator_terminal", lambda: None)
    with (profile / ".session.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already open"):
            desktop.run_action("chat", profile)
