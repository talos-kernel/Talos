"""Release tooling cannot count an incomplete or no-op rehearsal as qualified."""
import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.fixture
def gate(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "beta-gate.py"
    spec = importlib.util.spec_from_file_location("release_gate_under_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    for relative in module.LOCKFILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture==1.0\n")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/tools/" + name)
    return module


@pytest.mark.parametrize("arguments", [[], ["--previous", "0.20.0-rc.1", "--current", "0.20.0-rc.1"]])
def test_full_gate_refuses_missing_baseline_or_no_op(gate, monkeypatch, arguments):
    monkeypatch.setattr(sys, "argv", ["beta-gate.py", *arguments])
    monkeypatch.setattr(gate, "run", lambda *args: pytest.fail("preflight must refuse before running checks"))
    with pytest.raises(SystemExit) as error:
        gate.main()
    assert error.value.code == 2


@pytest.mark.parametrize("missing", ["requirements.lock", "requirements-dev.lock",
                                    "requirements-verifier.lock", "deploy/computer-requirements.lock"])
def test_missing_environment_lock_refuses_the_gate(gate, monkeypatch, missing):
    (gate.ROOT / missing).unlink()
    monkeypatch.setattr(sys, "argv", ["beta-gate.py", "--without-upgrade"])
    monkeypatch.setattr(gate, "run", lambda *args: pytest.fail("missing lock must fail before checks"))
    assert gate.main() == 1


def test_source_gate_checks_every_environment_but_never_claims_upgrade(gate, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(sys, "argv", ["beta-gate.py", "--without-upgrade"])
    monkeypatch.setattr(gate, "run", lambda label, argv: calls.append((label, argv)) or True)
    assert gate.main() == 0
    scan = next(argv for label, argv in calls if label == "Locked dependency scan")
    assert {arg for arg in scan if arg.startswith("requirements.txt:")} == {
        "requirements.txt:requirements.lock", "requirements.txt:requirements-dev.lock",
        "requirements.txt:requirements-verifier.lock", "requirements.txt:deploy/computer-requirements.lock",
    }
    assert "--no-resolve" in scan
    assert any("scripts/sync-verifier-lock.py" in argv and "--check" in argv for _, argv in calls)
    assert not any("scripts/beta-clean-install.py" in argv or "scripts/beta-upgrade-rehearsal.py" in argv
                   for _, argv in calls)
    assert "signed upgrade/rollback was NOT run" in capsys.readouterr().out


def test_security_failure_stops_before_installation(gate, monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["beta-gate.py", "--previous", "0.20.0-beta.9",
                                      "--current", "0.20.0-rc.1"])
    def run(label, argv):
        calls.append(label)
        return label != "Locked dependency scan"
    monkeypatch.setattr(gate, "run", run)
    assert gate.main() == 1
    assert calls[-1] == "Locked dependency scan"
    assert "Clean install from published release" not in calls


def test_full_gate_passes_explicit_versions_and_assets_to_real_rehearsal(gate, monkeypatch):
    calls = []
    assets = gate.ROOT / "assets"
    monkeypatch.setattr(sys, "argv", ["beta-gate.py", "--previous", "0.20.0-beta.9",
                                      "--current", "0.20.0-rc.1", "--assets-dir", str(assets),
                                      "--base", "https://example.invalid"])
    monkeypatch.setattr(gate, "run", lambda label, argv: calls.append((label, argv)) or True)
    assert gate.main() == 0
    install = next(argv for label, argv in calls if label == "Clean install from published release")
    upgrade = next(argv for label, argv in calls if label == "Signed upgrade, migration and rollback")
    assert install[-4:] == ["--current", "0.20.0-rc.1", "--base", "https://example.invalid"]
    assert upgrade[-6:] == ["--previous", "0.20.0-beta.9", "--current", "0.20.0-rc.1",
                           "--assets-dir", str(assets.resolve())]
