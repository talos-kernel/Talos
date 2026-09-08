from dataclasses import replace
from pathlib import Path

import pytest
from talos.autonomy import AutonomyGovernor, GovernedKernel
from talos.channel import Principal, Trust
from talos.policy import PolicyKernel, ToolRequest, Verdict
from talos.tools import default_manifest
from talos.routine import remote_readonly
from talos.schedule import UnattendedCeiling
from talos.subagent import ReadOnlyCeiling

OWNER = Principal("telegram", "100000001")
ARGS = {"op":"exec", "project":"demo", "key":"create", "title":"Create", "command":"printf ok > a"}


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setenv("TALOS_COMPUTER_SOCKET", "/run/example/control.sock")
    monkeypatch.setenv("TALOS_REMOTE_HOSTS", "example")
    monkeypatch.setenv("TALOS_COMPUTER_AUTOAPPROVE", "1")
    monkeypatch.setenv("TALOS_REMOTE_READONLY_AUTOAPPROVE", "1")
    return GovernedKernel(PolicyKernel(default_manifest(), frozenset({OWNER})),
                          AutonomyGovernor(5), lambda _:Trust.FULL, attended_autoapprove=True)


def test_attended_computer_opt_in_keeps_raw_kernel_and_audit_reason(gate):
    req = ToolRequest("computer_run", OWNER, ARGS)
    assert gate.kernel.decide(req).verdict is Verdict.NEEDS_HUMAN
    assert gate.decide(req).verdict is Verdict.ALLOW
    assert gate.decide(req).reason.startswith("attended auto-approval")


@pytest.mark.parametrize("change", ["config", "identity", "trust", "level", "malformed", "secret", "system", "persistence", "resume"])
def test_opt_in_cannot_cross_neighboring_boundaries(gate, monkeypatch, change):
    req = ToolRequest("computer_run", OWNER, ARGS)
    if change == "config": monkeypatch.delenv("TALOS_COMPUTER_AUTOAPPROVE")
    if change == "identity": req = replace(req, identity=Principal("telegram", "foreign"))
    if change == "trust": gate = replace(gate, trust_of=lambda _:Trust.ASK)
    if change == "level": gate = replace(gate, governor=AutonomyGovernor(3))
    if change == "malformed": req = replace(req, args={})
    if change == "secret": req = replace(req, targets=(str(Path.home()/'.secrets/key'),))
    if change == "system": req = replace(req, targets=("/etc/passwd",))
    if change == "persistence": req = replace(req, targets=(str(Path.home()/'.bashrc'),))
    if change == "resume": req = replace(req, args={"op":"resume"})
    assert gate.decide(req).verdict is not Verdict.ALLOW


@pytest.mark.parametrize("ceiling,field", [(UnattendedCeiling,"unattended"),(ReadOnlyCeiling,"delegated")])
def test_background_and_delegates_do_not_inherit_computer_opt_in(gate, ceiling, field):
    limit = ceiling()
    gate = replace(gate, **{field:limit})
    with limit.active():
        assert gate.decide(ToolRequest("computer_run",OWNER,ARGS)).verdict is Verdict.DENY


@pytest.mark.parametrize("command", ["df -h /", "free -h", "uptime", "nproc", "uname -r", "systemctl --user is-active demo.service", "systemctl --user show demo.service -p MainPID -p ActiveState,SubState"])
def test_closed_diagnostic_commands_can_run_without_prompt(gate, command):
    assert remote_readonly(command)
    assert gate.decide(ToolRequest("remote_exec",OWNER,{"host":"example","command":command})).verdict is Verdict.ALLOW


@pytest.mark.parametrize("command", ["df -h; id", "df $(id)", "df `id`", "df -h\nid", "free -h > file", "systemctl --user restart demo.service", "systemctl --user show demo.service", "systemctl show demo.service -p Environment", "systemctl show demo.service -p ExecStart", "systemctl --host=other show demo.service -p MainPID", "cat /etc/shadow", "sudo df -h", "df /private", "df --output=source", "env df -h", "systemctl show ../demo.service -p MainPID"])
def test_remote_grammar_rejects_effects_secret_oracles_and_shell_syntax(command):
    assert not remote_readonly(command)


def test_foreign_remote_host_still_denied(gate):
    assert gate.decide(ToolRequest("remote_exec",OWNER,{"host":"foreign","command":"df -h"})).verdict is Verdict.DENY
