"""Skill management through the production gate, snapshots and capability runner."""
import hashlib
import json
from dataclasses import replace

import pytest

from talos import tools
from talos.autonomy import AutonomyGovernor, GovernedKernel, attended_routine
from talos.capability import CapabilityMint, GrantedRunner
from talos.channel import Principal, Trust
from talos.eventlog import EventLog
from talos.executor import Executor, Status
from talos.manifest import Effect
from talos.policy import PolicyKernel, ToolRequest, Verdict
from talos.schedule import UnattendedCeiling
from talos.skills import discover_skills
from talos.snapshot import Snapshotter

OWNER = Principal("telegram", "100000001")


@pytest.fixture
def stack(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    monkeypatch.setenv("TALOS_SKILLS_DIRS", str(root))
    kernel = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    mint = CapabilityMint(kernel)
    ex = Executor(policy=kernel, log=EventLog(tmp_path / "events.db"),
                  snapshotter=Snapshotter(tmp_path / "snapshots"), mint=mint,
                  runner=GrantedRunner(mint=mint, runners=tools.RUNNERS))
    return root, kernel, ex


def request(action="create", **kwargs):
    args = dict(action=action, name="verified-workflow", expected_sha256="absent")
    if action == "create":
        args.update(description="A tested workflow.", body="Read input. Verify result.")
    args.update(kwargs)
    return ToolRequest("skill_manage", OWNER, args)


def test_management_is_always_attended_and_view_is_read(stack):
    _, kernel, _ = stack
    spec = tools.default_manifest().get("skill_manage")
    assert spec is not None and spec.effect is Effect.WRITE and not spec.reversible
    assert tools.default_manifest().get("skill_view").effect is Effect.READ
    assert kernel.decide(request()).verdict is Verdict.NEEDS_HUMAN
    assert not attended_routine(request(), spec, kernel)
    ceiling = UnattendedCeiling()
    governed = GovernedKernel(kernel, AutonomyGovernor(5), lambda _: Trust.FULL,
                              unattended=ceiling)
    with ceiling.active():
        assert governed.decide(request()).verdict is Verdict.DENY


def test_approved_lifecycle_survives_fresh_discovery(stack):
    root, kernel, ex = stack
    target = root / "verified-workflow" / "SKILL.md"
    req = request()
    assert kernel.guard_targets(req) == (str(target),)
    assert ex.run(req, "blocked-create").status is Status.NEEDS_HUMAN
    assert not target.exists()
    assert ex.run(req, "approved-create", human_approved=True).status is Status.DONE
    original = target.read_bytes()
    first_hash = hashlib.sha256(original).hexdigest()
    view = ex.run(ToolRequest("skill_view", OWNER, {"name": "verified-workflow"}), "view")
    assert view.status is Status.DONE
    assert json.loads(view.result)["expected_sha256"] == first_hash
    patch = request("patch", expected_sha256=first_hash,
                    old_string="Verify result.", new_string="Verify and report result.")
    assert ex.run(patch, "patch", human_approved=True).status is Status.DONE
    skill = discover_skills(root).get("verified-workflow")
    assert skill and "Verify and report" in skill.load_body()
    changed_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    assert ex.run(patch, "stale-patch", human_approved=True).status is Status.ERROR
    assert hashlib.sha256(target.read_bytes()).hexdigest() == changed_hash
    remove = request("delete", expected_sha256=changed_hash)
    assert ex.run(remove, "delete", human_approved=True).status is Status.DONE
    assert discover_skills(root).get("verified-workflow") is None
    restore = request("restore", revision=first_hash)
    assert ex.run(restore, "restore", human_approved=True).status is Status.DONE
    assert target.read_bytes() == original
    assert discover_skills(root).get("verified-workflow") is not None


@pytest.mark.parametrize("tool", ["skill_view", "skill_manage"])
def test_secret_and_system_roots_keep_their_floor(stack, monkeypatch, tool):
    _, kernel, _ = stack
    req = ToolRequest(tool, OWNER, request().args)
    monkeypatch.setenv("TALOS_SKILLS_DIRS", "/etc/talos-skills")
    assert kernel.decide(req).verdict is Verdict.DENY
    monkeypatch.setenv("TALOS_SKILLS_DIRS", "~/.ssh/skills")
    expected = Verdict.DENY if tool == "skill_view" else Verdict.NEEDS_HUMAN
    assert kernel.decide(req).verdict is expected


def test_declared_targets_cannot_hide_real_destination(stack):
    root, kernel, _ = stack
    req = request()
    forged = ToolRequest(req.tool, req.identity, req.args, ("/tmp/harmless",))
    assert kernel.guard_targets(forged) == (str(root / "verified-workflow" / "SKILL.md"),)
    stranger = ToolRequest(req.tool, Principal("telegram", "999"), req.args)
    assert kernel.decide(stranger).verdict is Verdict.DENY


def test_root_swap_after_grant_cannot_redirect_an_approved_creation(stack, monkeypatch, tmp_path):
    root, _, ex = stack
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    def swapped(req):
        monkeypatch.setenv("TALOS_SKILLS_DIRS", str(elsewhere))
        return tools.RUNNERS["skill_manage"](req)

    ex = replace(ex, runner=GrantedRunner(mint=ex.mint, runners={**tools.RUNNERS, "skill_manage": swapped}))
    outcome = ex.run(request(), "root-swap", human_approved=True)
    assert outcome.status is Status.ERROR
    assert not (elsewhere / "verified-workflow" / "SKILL.md").exists()
    assert not (root / "verified-workflow" / "SKILL.md").exists()


@pytest.mark.parametrize("args", [{}, {"name": "verified-workflow"}])
def test_read_root_swap_after_grant_is_refused(stack, monkeypatch, tmp_path, args):
    root, _, ex = stack
    assert ex.run(request(), "create-before-read", human_approved=True).status is Status.DONE
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def swapped(req):
        monkeypatch.setenv("TALOS_SKILLS_DIRS", str(elsewhere))
        return tools.RUNNERS["skill_view"](req)

    ex = replace(ex, runner=GrantedRunner(mint=ex.mint, runners={**tools.RUNNERS, "skill_view": swapped}))
    outcome = ex.run(ToolRequest("skill_view", OWNER, args), "read-root-swap")
    assert outcome.status is Status.ERROR
    assert "target" in outcome.detail
    assert (root / "verified-workflow" / "SKILL.md").is_file()
