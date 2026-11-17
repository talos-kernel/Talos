import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

sys.path.insert(0, str(Path(__file__).parents[1]))
import desktop_runtime
import desktop_updates as updates
from talos import updater


@pytest.fixture
def release(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_runtime, "require_operator_terminal", lambda: None)
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(updater, "RELEASE_PUBLIC_KEY", base64.b64encode(
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode())
    content = io.BytesIO()
    files = {"talos/__init__.py": '__version__ = "0.20.3"', "tests/test_smoke.py": "", "redteam.py": "",
             "requirements.txt": "", "requirements.lock": "", "SOUL.md": "public"}
    with tarfile.open(fileobj=content, mode="w:gz") as archive:
        for name, value in files.items():
            data = value.encode()
            info = tarfile.TarInfo("talos/" + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    payload = content.getvalue()
    responses = {"latest.txt": b"0.20.3", "CHANGELOG-0.20.3.md": b"Release notes",
                 "talos-0.20.3.tar.gz": payload,
                 "talos-0.20.3.tar.gz.sha256": hashlib.sha256(payload).hexdigest().encode(),
                 "talos-0.20.3.tar.gz.sig": base64.b64encode(key.sign(payload))}
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    for name in ("data", "workspace", "skills"):
        (profile / name).mkdir()
    for name in ("SOUL.md", "AGENTS.md", "USER.md", "talos.env"):
        (profile / name).write_text("operator-owned")
    calls = []
    def runner(argv, cwd):
        calls.append(argv)
        if "venv" in argv:
            (cwd / ".venv/bin").mkdir(parents=True)
            (cwd / ".venv/bin/python").write_text("fixture")
        return 0
    return profile, responses, lambda url: responses[url.rsplit("/", 1)[1]], runner, calls


def test_signed_update_proves_then_switches_and_preserves_profile(release):
    profile, _, fetch, runner, calls = release
    assert updates.install(profile, "0.20.0-beta.8", fetch=fetch, runner=runner, stdout=io.StringIO()) == 0
    record = json.loads((profile / "active-runtime.json").read_text())
    source = profile / "updates" / record["sha256"]
    assert record["version"] == "0.20.3"
    assert any("pytest" in call for call in calls)
    assert any("redteam.py" in call for call in calls)
    assert (source / "SOUL.md").resolve() == profile / "SOUL.md"
    assert (profile / "SOUL.md").read_text() == "operator-owned"
    assert (profile / "talos.env").read_text() == "operator-owned"
    assert (profile / "active-runtime.json").stat().st_mode & 0o777 == 0o600
    bundled = profile / "bundled"
    (bundled / "talos").mkdir(parents=True)
    (bundled / "talos/__init__.py").write_text('__version__ = "0.20.0-beta.8"')
    assert updates.selected_runtime(profile, bundled) == source
    (bundled / "talos/__init__.py").write_text('__version__ = "0.20.4"')
    assert updates.selected_runtime(profile, bundled) == bundled


@pytest.mark.parametrize("failure", ["signature", "checksum", "pytest", "redteam.py"])
def test_failed_proof_leaves_active_pointer_and_state_untouched(release, failure):
    profile, responses, fetch, runner, _ = release
    pointer = profile / "active-runtime.json"
    pointer.write_text("old pointer")
    if failure == "signature":
        responses["talos-0.20.3.tar.gz.sig"] = base64.b64encode(b"0" * 64)
    if failure == "checksum":
        responses["talos-0.20.3.tar.gz.sha256"] = b"0" * 64
    def fail(argv, cwd):
        return 1 if failure in argv else runner(argv, cwd)
    with pytest.raises(RuntimeError):
        updates.install(profile, "0.20.0-beta.8", fetch=fetch, runner=fail, stdout=io.StringIO())
    assert pointer.read_text() == "old pointer"
    assert (profile / "SOUL.md").read_text() == "operator-owned"
    assert not list((profile / "updates").iterdir())


def test_current_version_does_not_install_or_downgrade(release):
    profile, _, fetch, runner, calls = release
    for version in ("0.20.3", "0.20.4"):
        assert not updates.check(version, fetch=fetch)["available"]
        assert updates.install(profile, version, fetch=fetch, runner=runner, stdout=io.StringIO()) == 0
    assert calls == []
    assert not (profile / "active-runtime.json").exists()


@pytest.mark.parametrize("digest", ["../outside", "0" * 63, "Z" * 64])
def test_invalid_update_pointer_is_not_followed(tmp_path, digest):
    (tmp_path / "active-runtime.json").write_text(json.dumps({"sha256": digest}))
    with pytest.raises(RuntimeError):
        updates.selected_runtime(tmp_path, tmp_path / "bundled")


def test_proofs_do_not_inherit_operator_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHONPATH", "old-runtime")
    monkeypatch.setenv("TALOS_SECRETS_ENV", "private-config")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    def run(argv, **kwargs):
        assert argv[:2] == ["python", "-B"]
        assert not {"PYTHONPATH", "TALOS_SECRETS_ENV", "ANTHROPIC_API_KEY"} & kwargs["env"].keys()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(updates.subprocess, "run", run)
    assert updates.proof_runner(["python", "redteam.py"], tmp_path) == 0


def test_update_venv_uses_private_copy_not_signed_interpreter(tmp_path, monkeypatch):
    base = tmp_path / "Talos.app/Contents/Resources/python"
    (base / "bin").mkdir(parents=True)
    (base / "bin/python3").write_text("interpreter")
    destination = tmp_path / "candidate"
    destination.mkdir()
    monkeypatch.setattr(sys, "base_prefix", str(base))
    monkeypatch.setattr(sys, "_base_executable", str(base / "bin/python3"))
    calls = []
    run = updates.prepare_runner(destination, lambda argv, cwd: calls.append(argv) or 0)
    run([str(base / "bin/python3"), "-m", "venv", ".venv"], destination)
    copied = Path(calls[0][0])
    assert copied.is_relative_to(destination / ".desktop-python")
    copied.write_text("child cache or changes")
    assert (base / "bin/python3").read_text() == "interpreter"
