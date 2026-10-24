"""Skill guidance cannot acquire permanent or task-wide write authorization."""
import json
import hashlib

import pytest

from talos.approval import ApprovalPicker
from talos.channel import Trust
from talos.executor import Status
from talos.policy import ToolRequest
from talos.standing import action_key
from talos.task_approval import TaskApprovals, TaskExecutor
from test_conductor import _build, FakeReasoner, OWNER, CHAT_OWNER, msg


def req(tool="skill_manage", **extra):
    args = dict(name="approval-probe", description="A probe", body="Read and verify.")
    if tool == "skill_manage":
        args.update(action="create", expected_sha256="absent")
    args.update(extra)
    return ToolRequest(tool, OWNER, args)


@pytest.mark.parametrize("tool", ["skill_write", "skill_manage"])
def test_skill_changes_cannot_get_standing_or_task_authorization(tmp_path, monkeypatch, tool):
    monkeypatch.setenv("TALOS_SKILLS_DIRS", str(tmp_path / "skills"))
    conductor, _ = _build(tmp_path, FakeReasoner())
    request = req(tool)
    assert action_key(request) is None
    assert conductor.standing.grant(CHAT_OWNER, request, principal=OWNER, run_id="standing") is None
    assert conductor.standing.find(CHAT_OWNER, request, principal=OWNER) is None
    scopes = TaskApprovals()
    scopes.open("task", OWNER, CHAT_OWNER)
    assert scopes.approve("task", OWNER, CHAT_OWNER)
    executor = TaskExecutor(conductor.executor, scopes, "task", OWNER, CHAT_OWNER, lambda: Trust.FULL)
    assert executor.run(request, "task-skill").status is Status.NEEDS_HUMAN
    assert not (tmp_path / "skills" / "approval-probe" / "SKILL.md").exists()


def test_skill_approval_names_the_operation_and_exact_patch(tmp_path, monkeypatch):
    monkeypatch.setenv("TALOS_SKILLS_DIRS", str(tmp_path / "skills"))
    conductor, _ = _build(tmp_path, FakeReasoner())
    request = req(action="patch", expected_sha256="a" * 64,
                  old_string="original\nline", new_string="reviewed\nline", replace_all=False)
    prompt = conductor._approval_prompt(request)
    assert '"action": "patch"' in prompt
    assert json.dumps("original\nline") in prompt
    assert json.dumps("reviewed\nline") in prompt
    assert '"replace_all": false' in prompt
    assert "a" * 64 in prompt
    assert "always (" not in prompt
    assert "untrusted" in prompt.lower()


def test_skill_approval_marks_large_content_with_full_hash_without_secret_leak(tmp_path, monkeypatch):
    monkeypatch.setenv("TALOS_SKILLS_DIRS", str(tmp_path / "skills"))
    conductor, _ = _build(tmp_path, FakeReasoner())
    content = "read then verify " * 400 + "END-OF-REVIEW"
    prompt = conductor._approval_prompt(req(action="edit", content=content))
    assert '"characters_not_shown"' in prompt
    assert hashlib.sha256(content.encode()).hexdigest() in prompt
    assert len(prompt) < 4096
    synthetic = "ghp_" + "abcdefghijklmnopqrstuvwxyz"
    assert synthetic not in conductor._approval_prompt(req(body=synthetic))


def test_skill_buttons_only_offer_once_or_deny(tmp_path, monkeypatch):
    monkeypatch.setenv("TALOS_SKILLS_DIRS", str(tmp_path / "skills"))
    conductor, _ = _build(tmp_path, FakeReasoner())
    pending = conductor.approvals.park(CHAT_OWNER, req(), "r", task_id="task", resume_agent=True)
    message = ApprovalPicker().open("Review skill", pending, principal=OWNER, conversation=CHAT_OWNER)
    labels = [button.label for row in message.keyboard for button in row]
    assert labels == ["✓ Allow once", "✕ Deny"]


def test_typed_task_approval_does_not_expand_a_skill_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("TALOS_SKILLS_DIRS", str(tmp_path / "skills"))
    conductor, sent = _build(tmp_path, FakeReasoner())
    conductor.task_approvals.open("task", OWNER, CHAT_OWNER)
    pending = conductor.approvals.park(CHAT_OWNER, req(), "r", task_id="task", resume_agent=True)
    conductor.handle(msg(1, OWNER, "allow this task"))
    assert conductor.task_approvals.state("task", OWNER, CHAT_OWNER) is False
    assert conductor.approvals.get(CHAT_OWNER) is pending
    assert "yes or no" in sent[-1][1]
    assert not (tmp_path / "skills").exists()
