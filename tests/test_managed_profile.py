"""A package-manager code swap must never swap its operator's state or policy."""
import os
from pathlib import Path
import subprocess
import sys
import shutil
import pytest


ROOT = Path(__file__).resolve().parents[1]


def child(home, script, *, managed=True, profile=None, code_root=ROOT):
    env = {key: value for key, value in os.environ.items() if not key.startswith("TALOS_")}
    env.update(HOME=str(home), PYTHONPATH=str(code_root), PYTHONDONTWRITEBYTECODE="1")
    if managed:
        env["TALOS_INSTALL_MANAGER"] = "homebrew"
    if profile is not None:
        env["TALOS_PROFILE_DIR"] = str(profile)
    result = subprocess.run([sys.executable, "-c", script], env=env, cwd=code_root,
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_all_mutable_paths_share_profile_and_assets_stay_with_code(tmp_path):
    child(tmp_path, '''
from talos import paths, config, identity, instructions, policy, autonomy, clianything, vault
p = paths.PROFILE_DIR
assert p == paths.Path.home() / ".talos" / "homebrew"
assert config.DATA_DIR == p / "data"
assert config.PROJECTS_FILE == p / "data/projects.json"
assert config.LOCAL_ENV == config.SECRETS_ENV == p / "talos.env"
assert identity.SOUL_PATH == policy.SOUL_FILE == p / "SOUL.md"
assert instructions.AGENTS_PATH == policy.AGENTS_FILE == p / "AGENTS.md"
assert instructions.USER_PATH == policy.USER_FILE == p / "USER.md"
assert policy.WORKSPACE_DIR == autonomy.WORKSPACE == p / "workspace"
assert clianything.DEFAULT_REGISTRY == p / "data/cli-anything.json"
assert config.VAULT_DIR == vault.DEFAULT_VAULT_DIR == p / "vault"
assert config.MISSIONS_DIR == paths.INSTALL_DIR / "missions"
assert config.BLUEPRINTS_DIR == paths.INSTALL_DIR / "blueprints"
assert not p.exists(), "imports must not initialize profiles"
''')


def test_unmanaged_defaults_are_unchanged(tmp_path):
    child(tmp_path, '''
from talos import paths, config, policy, identity
assert paths.PROFILE_DIR == paths.INSTALL_DIR
assert config.DATA_DIR == paths.INSTALL_DIR / "data"
assert identity.SOUL_PATH == paths.INSTALL_DIR / "SOUL.md"
assert policy.WORKSPACE_DIR == paths.INSTALL_DIR / "workspace"
''', managed=False)


def test_profile_init_upgrade_rollback_preserve_state_and_permissions(tmp_path):
    child(tmp_path, '''
from pathlib import Path
from talos import paths
from talos.channel import Principal
from talos.projects import ProjectStore
from talos.eventlog import EventLog, Event
paths.initialize_profile()
p = paths.PROFILE_DIR
assert p.stat().st_mode & 0o777 == 0o700
for name in ("SOUL.md", "AGENTS.md", "USER.md"):
    assert (p / name).is_file()
    assert (p / name).stat().st_mode & 0o777 == 0o600
(p / "SOUL.md").write_text("# My operator agent\\n")
(p / "talos.env").write_text("TALOS_MODEL=operator-model\\n")
(p / "talos.env").chmod(0o600)
(p / "workspace/keep.txt").write_text("operator work")
store = ProjectStore(p / "data/projects.json")
principal = Principal("cli", "123")
store.add("work", p / "workspace", principal=principal, conversation="cli:123")
store.select("work", principal=principal, conversation="cli:123")
log = EventLog(p / "data/eventlog.db")
log.append(Event("managed-test", "test", "model.selected", {"model": "operator-model"}))
log.close()
before = {str(f.relative_to(p)): f.read_bytes() for f in p.rglob("*") if f.is_file()}
for version in ("new", "old"):
    code = Path.home() / version
    code.mkdir()
    for name in ("SOUL.md", "AGENTS.md", "USER.md"):
        (code / name).write_text("replacement " + version)
    paths.initialize_profile(source=code)
    assert {str(f.relative_to(p)): f.read_bytes() for f in p.rglob("*") if f.is_file()} == before
assert ProjectStore(p / "data/projects.json").selected(principal=principal, conversation="cli:123").name == "work"
''')


def test_managed_policy_protects_config_identity_and_code(tmp_path):
    child(tmp_path, '''
from talos import paths, policy
from talos.channel import Principal
from talos.tools import default_manifest
p = Principal("cli", "123")
k = policy.PolicyKernel(default_manifest(), frozenset({p}))
def verdict(tool, path):
    return k.decide(policy.ToolRequest(tool, p, {"path": str(path), "content": "x"})).verdict
assert verdict("read_file", paths.PROFILE_DIR / "talos.env") is policy.Verdict.DENY
assert verdict("write_file", paths.PROFILE_DIR / "talos.env") is policy.Verdict.NEEDS_HUMAN
for name in ("SOUL.md", "AGENTS.md", "USER.md"):
    assert verdict("write_file", paths.PROFILE_DIR / name) is policy.Verdict.NEEDS_HUMAN
assert verdict("write_file", paths.INSTALL_DIR / "talos/policy.py") is policy.Verdict.NEEDS_HUMAN
for name in ("missions/evil.json", "blueprints/evil.json", ".talos-install-manager"):
    assert verdict("write_file", paths.INSTALL_DIR / name) is policy.Verdict.NEEDS_HUMAN
assert verdict("write_file", paths.PROFILE_DIR / "workspace/note.txt") is policy.Verdict.ALLOW
''')


def test_managed_update_refuses_before_network_or_writes_even_with_prefix(tmp_path):
    child(tmp_path, '''
import io
from talos.updater import run_update
def forbidden(*args):
    raise AssertionError("managed updater reached an effect")
for args in ([], ["--check"], ["--prefix", "/tmp/other-install"]):
    out = io.StringIO()
    assert run_update(args, stdout=out, http=forbidden, runner=forbidden) != 0
    assert "brew upgrade" in out.getvalue()
''')


def test_existing_unsafe_profile_or_symlink_is_refused(tmp_path):
    child(tmp_path, '''
from talos import paths
p = paths.PROFILE_DIR
p.mkdir(parents=True, mode=0o755)
p.chmod(0o755)
try:
    paths.initialize_profile()
except ValueError:
    pass
else:
    raise AssertionError("shared profile accepted")
p.chmod(0o700)
outside = p.parent / "outside"
outside.mkdir()
(p / "data").symlink_to(outside, target_is_directory=True)
try:
    paths.initialize_profile()
except ValueError:
    pass
else:
    raise AssertionError("symlink profile child accepted")
assert list(outside.iterdir()) == []
''')


def test_profiles_are_isolated_between_users(tmp_path):
    for name in ("alice", "bob"):
        child(tmp_path / name, '''
from talos import paths
paths.initialize_profile()
(paths.PROFILE_DIR / "workspace/owner").write_text(str(paths.Path.home()))
''')
    alice = tmp_path / "alice/.talos/homebrew/workspace/owner"
    bob = tmp_path / "bob/.talos/homebrew/workspace/owner"
    assert alice.read_text() != bob.read_text()


def test_profile_location_cannot_be_changed_through_config_file(tmp_path):
    child(tmp_path, '''
import io
from talos import paths, config, configcli, schema
paths.initialize_profile()
p = paths.PROFILE_DIR
for key in ("TALOS_PROFILE_DIR", "TALOS_INSTALL_MANAGER"):
    assert schema.get(key).kind is schema.POLICY
    assert configcli.cmd_set(key, "/tmp/elsewhere", p / "talos.env", io.StringIO().write) != 0
(p / "talos.env").write_text("TALOS_PROFILE_DIR=/tmp/elsewhere\\nTALOS_INSTALL_MANAGER=\\n")
(p / "talos.env").chmod(0o600)
assert config.LOCAL_ENV == p / "talos.env"
assert paths.install_manager() == "homebrew"
''')


def test_managed_diagnostics_do_not_create_profile(tmp_path):
    child(tmp_path, '''
import io, os
from talos import paths, config
from talos.cli import dispatch
assert dispatch(["version"]) == 0
assert dispatch(["status"]) == 0
config.load_config(require_channel=False)
os.environ["TELEGRAM_BOT_TOKEN"] = "123:synthetic"
os.environ["TALOS_ALLOWED_PRINCIPALS"] = "telegram:100000001"
from talos.doctor import run_doctor
run_doctor([], out=io.StringIO())
assert not paths.PROFILE_DIR.exists()
''')


def test_cancelled_setup_does_not_create_profile(tmp_path):
    child(tmp_path, '''
import io
from talos import paths
from talos.setup_wizard import run_setup
class Terminal(io.StringIO):
    def isatty(self):
        return True
out = Terminal()
assert run_setup(["terminal"], stdin=Terminal("n\\n"), stdout=out) == 0
assert "Cancelled" in out.getvalue()
assert not paths.PROFILE_DIR.exists()
''')


def test_real_code_prefix_switch_and_uninstall_retain_profile(tmp_path):
    """Import and execute two separate installed packages, including rollback."""
    home = tmp_path / "operator"
    prefixes = [tmp_path / "cellar/old", tmp_path / "cellar/new"]
    for code in prefixes:
        shutil.copytree(ROOT / "talos", code / "talos", ignore=shutil.ignore_patterns("__pycache__"))
        (code / ".talos-install-manager").write_text("homebrew\n")
        (code / "SOUL.md").write_text("# Shipped " + code.name)
    child(home, '''
from talos import paths, config
paths.initialize_profile()
assert paths.install_manager() == "homebrew"
(paths.PROFILE_DIR / "SOUL.md").write_text("# Kept identity")
(paths.PROFILE_DIR / "workspace/keep.txt").write_text("kept work")
from talos.eventlog import EventLog, Event
log = EventLog(config.EVENTLOG_DB)
log.append(Event("run", "test", "model.selected", {"model": "kept-model"}))
log.close()
''', managed=False, code_root=prefixes[0])
    for code in (prefixes[1], prefixes[0]):
        child(home, '''
import io
from talos import paths, config, identity
from talos.cli import cmd_status
from talos.updater import run_update
paths.initialize_profile()
assert identity.agent_name() == "Kept identity"
assert (paths.PROFILE_DIR / "workspace/keep.txt").read_text() == "kept work"
out = io.StringIO()
cmd_status(out)
assert "kept-model" in out.getvalue()
assert run_update([], stdout=io.StringIO(), http=lambda _: (_ for _ in ()).throw(AssertionError("network"))) == 1
assert not (paths.INSTALL_DIR / "data").exists()
''', managed=False, code_root=code)
    for code in prefixes:
        shutil.rmtree(code)
    assert (home / ".talos/homebrew/workspace/keep.txt").read_text() == "kept work"


def test_live_managed_sandbox_masks_profile_secret_and_bounds_writes(tmp_path):
    output = child(tmp_path, '''
import shlex
from talos import paths, sandbox
backend = sandbox.select_backend(sandbox.default_backends())
if backend is None:
    print("SKIP:no working OS sandbox")
    raise SystemExit(0)
paths.initialize_profile()
p = paths.PROFILE_DIR
secret = p / "talos.env"
secret.write_text("PRIVATE_PROFILE_SENTINEL")
secret.chmod(0o600)
shell = sandbox.SandboxedShell(backend=backend)
read = shell.run("cat " + shlex.quote(str(secret)))
assert read.returncode != 0
assert "PRIVATE_PROFILE_SENTINEL" not in read.stdout
write = shell.run("printf changed > " + shlex.quote(str(p / "SOUL.md")))
assert write.returncode != 0
workspace = shell.run("printf allowed > note.txt")
assert workspace.returncode == 0, workspace.stderr
assert (p / "workspace/note.txt").read_text() == "allowed"
''')
    if output.startswith("SKIP:"):
        pytest.skip(output.strip())
