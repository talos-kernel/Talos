import json
from types import SimpleNamespace
from talos.agent_loop import AgentStatus, run_agent
from talos.channel import Principal
from talos.executor import Outcome, Status
from talos.recovery import advice

OWNER = Principal("telegram", "100000001")


def test_permission_refusal_is_never_a_recovery_opportunity():
    for status in (Status.DENIED, Status.NEEDS_HUMAN):
        outcome = SimpleNamespace(status=status, detail="chromium timeout", result=None)
        assert advice("browse", outcome, 0) == ""


def test_mutations_and_uncertain_writes_are_never_retried():
    outcome = SimpleNamespace(status=Status.ERROR, detail="connection reset", result=None)
    for tool in ("run_shell", "remote_exec", "computer_run", "write_file", "http_request", "delegate_code"):
        assert advice(tool, outcome, 0) == ""


def test_recovery_has_a_fixed_budget():
    outcome = SimpleNamespace(status=Status.ERROR, detail="chromium timed out", result=None)
    assert advice("browse", outcome, 0)
    assert advice("browse", outcome, 1)
    assert not advice("browse", outcome, 2)


def test_announced_plan_can_recover_from_browser_timeout_through_same_executor():
    replies = iter([
        'PLAN: {"goal":"Find a fact","steps":["Read source","Report evidence"]}\n'
        'TOOL_CALL: {"tool":"browse","args":{"url":"https://example.com"}}',
        'TOOL_CALL: {"tool":"web_fetch","args":{"url":"https://example.com"}}',
        "The source confirms the fact."
    ])
    called = []
    class Executor:
        def run(self, request, run_id, **kwargs):
            called.append(request.tool)
            if request.tool == "browse":
                return SimpleNamespace(status=Status.ERROR, detail="Chromium TimeoutExpired", result=None)
            return SimpleNamespace(status=Status.DONE, detail="read", result="verified source fact")
    result = run_agent(lambda history: next(replies), Executor(), OWNER, "recovery")
    assert result.status is AgentStatus.ANSWERED
    assert called == ["browse", "web_fetch"]
    assert any("Every alternative still passes the kernel" in entry for entry in result.history)
