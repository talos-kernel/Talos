"""One explicit task decision covers later actions, never the next task."""
from dataclasses import replace
from pathlib import Path
import threading
import time

import pytest

from talos.approval import ApprovalPicker
from talos.channel import CallbackQuery, StructuredMessage, Trust
from talos.capability import GrantedRunner
from talos.executor import Status
from talos.policy import ToolRequest
from talos.task_approval import TaskApprovals, TaskExecutor
from test_standing_flow import CHAT, OWNER, ZWEITER, Rig, Scripted, call, msg, write_to


def test_task_approval_runs_different_actions_until_completion(tmp_path):
    first, second, later = (tmp_path / name for name in ("first", "second", "later"))
    rig = Rig(tmp_path, Scripted(write_to(first), write_to(second), "Completed.", write_to(later)))
    rig.say("Create both files and verify the result")
    reply = rig.say("allow this task")
    assert first.read_text() == "hallo"
    assert second.read_text() == "hallo"
    assert rig.approvals.get(CHAT) is None
    assert "Completed." in reply
    assert "approval.standing" not in rig.types()
    rig.say("Now create another file")
    assert not later.exists()
    assert rig.approvals.get(CHAT) is not None


def test_task_button_is_explicit_about_whole_task_and_bound_to_pending(tmp_path):
    rig = Rig(tmp_path, Scripted(write_to(tmp_path / "first"), "Done."))
    messages: list[StructuredMessage] = []
    rig.conductor = replace(
        rig.conductor, approval_picker=ApprovalPicker(),
        send_structured=lambda _chat, message: messages.append(message),
    )
    rig.say("Create this file")
    labels = [button.label for row in messages[-1].keyboard for button in row]
    assert "▶ Allow this task" in labels
    assert "until" in messages[-1].text.lower()
    assert "no time limit" in messages[-1].text.lower()


def test_once_then_task_keeps_the_original_task_id(tmp_path):
    paths = [tmp_path / str(i) for i in range(3)]
    rig = Rig(tmp_path, Scripted(*(write_to(p) for p in paths), "Done."))
    rig.say("Create three files")
    task_id = rig.approvals.get(CHAT).task_id
    rig.say("yes")
    assert rig.approvals.get(CHAT).task_id == task_id
    rig.say("allow this task")
    assert all(p.exists() for p in paths)
    assert rig.conductor.task_approvals.state(task_id, OWNER, CHAT) is None
    used = [r['payload']['task_id'] for r in rig.log.recent(200, ()) if r['type'] == 'approval.task_used']
    assert used == [task_id, task_id]


def test_active_task_has_no_clock_expiry(tmp_path, monkeypatch):
    paths = [tmp_path / str(i) for i in range(3)]
    class LongTask(Scripted):
        def reason(self, prompt):
            if self.calls == 1:
                future = time.time() + 3 * 86400
                monkeypatch.setattr(time, 'time', lambda: future)
            return super().reason(prompt)
    rig = Rig(tmp_path, LongTask(*(write_to(p) for p in paths), "Done."))
    rig.say("A long task")
    rig.say("allow this task")
    assert all(p.exists() for p in paths)
    assert rig.approvals.get(CHAT) is None


def test_approved_task_receives_consent_receipt_and_can_be_steered(tmp_path):
    first, later = tmp_path / 'first', tmp_path / 'later'
    prompts = []
    class Inspect(Scripted):
        def reason(self, prompt):
            prompts.append(prompt)
            if self.calls == 1:
                assert rig.conductor.redirect.is_open()
                assert 'Accepted' in rig.say('/steer verify before reporting')
            return super().reason(prompt)
    rig = Rig(tmp_path, Inspect(write_to(first), 'Done.', write_to(later)))
    rig.say('Create this file')
    rig.say('/approve task')
    assert first.exists()
    assert 'Operator consent recorded by Talos' not in prompts[0]
    assert 'Operator consent recorded by Talos' in prompts[1]
    rig.say('Another task')
    assert 'Operator consent recorded by Talos' not in prompts[2]
    assert not later.exists()


@pytest.mark.parametrize('command', ['/stop', '/stopall', '/estop'])
def test_stop_during_approved_task_prevents_next_effect(tmp_path, command):
    first, second = tmp_path / 'first', tmp_path / 'second'
    class StopDuringReasoning(Scripted):
        def reason(self, prompt):
            if self.calls == 1:
                rig.say(command)
            return super().reason(prompt)
    rig = Rig(tmp_path, StopDuringReasoning(write_to(first), write_to(second), 'Done.'))
    rig.say('Create two files')
    rig.say('allow this task')
    assert first.exists()
    assert not second.exists()
    assert rig.approvals.get(CHAT) is None


def test_tool_failure_does_not_request_approval_again_or_replay(tmp_path):
    first, bad, last = (tmp_path / n for n in ('first', 'bad', 'last'))
    rig = Rig(tmp_path, Scripted(write_to(first), write_to(bad), write_to(last), 'Recovered.'))
    original = rig.executor.runner
    attempts = []
    def run(req, grant):
        attempts.append(req.args['path'])
        if req.args['path'] == str(bad):
            raise RuntimeError('fixture write failure')
        return original(req, grant)
    rig.conductor = replace(rig.conductor, executor=replace(rig.executor, runner=run))
    rig.say('Create files, recover if needed')
    rig.say('allow this task')
    assert first.exists() and last.exists() and not bad.exists()
    assert attempts == [str(first), str(bad), str(last)]
    assert rig.types().count('approval.parked') == 1


def test_provider_exception_ends_grant_and_does_not_leak_into_next_task(tmp_path):
    class FailedProvider(Scripted):
        def reason(self, prompt):
            if self.calls == 1:
                self.calls += 1
                raise RuntimeError('fixture terminal provider failure')
            return super().reason(prompt)
    first, later = tmp_path / 'first', tmp_path / 'later'
    rig = Rig(tmp_path, FailedProvider(write_to(first), 'unused', write_to(later)))
    rig.say('Create a file')
    task_id = rig.approvals.get(CHAT).task_id
    rig.say('allow this task')
    assert first.exists()
    assert rig.conductor.task_approvals.state(task_id, OWNER, CHAT) is None
    rig.say('New task')
    assert not later.exists() and rig.approvals.get(CHAT) is not None


def test_kernel_denial_and_autonomy_ceiling_still_win(tmp_path):
    first = tmp_path / 'first'
    secret = str(Path.home() / '.secrets' / 'talos-task-test-does-not-exist')
    rig = Rig(tmp_path, Scripted(write_to(first), call('read_file', {'path': secret}), 'Done.'))
    rig.say('Do a task')
    rig.say('allow this task')
    outcomes = [r['payload'] for r in rig.log.recent(200, ()) if r['type'] == 'exec.result']
    assert any(o['tool'] == 'read_file' and o['status'] == 'denied' for o in outcomes)
    assert rig.approvals.get(CHAT) is None


def test_task_scope_rejects_foreign_identity_chat_and_unattended_use(tmp_path):
    rig = Rig(tmp_path, Scripted('Done.'))
    scopes = TaskApprovals()
    scopes.open('task', OWNER, CHAT)
    assert not scopes.approve('task', ZWEITER, CHAT)
    assert scopes.approve('task', OWNER, CHAT)
    req = ToolRequest('write_file', OWNER, {'path': str(tmp_path / 'no'), 'content': 'x'})
    for owner, chat, trust in [(ZWEITER, CHAT, Trust.FULL), (OWNER, 'other-chat', Trust.FULL),
                               (OWNER, CHAT, Trust.ASK)]:
        executor = TaskExecutor(rig.executor, scopes, 'task', owner, chat, lambda: trust)
        assert executor.run(req, 'foreign').status is Status.DENIED
    # The shared executor still asks; there is no global, thread-local or env grant.
    assert rig.executor.run(req, 'separate-run').status is Status.NEEDS_HUMAN
    rig.governor.set_level(1, principal=OWNER, allowed_identities=frozenset({OWNER}))
    executor = TaskExecutor(rig.executor, scopes, 'task', OWNER, CHAT, lambda: Trust.FULL)
    assert executor.run(req, 'lower-ceiling').status is Status.DENIED
    assert not (tmp_path / 'no').exists()


def test_double_click_and_foreign_callback_cannot_replay_or_approve(tmp_path):
    path = tmp_path / 'first'
    rig = Rig(tmp_path, Scripted(write_to(path), 'Done.'))
    messages = []
    rig.conductor = replace(rig.conductor, approval_picker=ApprovalPicker(),
                            send_structured=lambda c, m: messages.append(m))
    rig.say('Create file')
    button = next(b for row in messages[-1].keyboard for b in row if b.label == '▶ Allow this task')
    click = CallbackQuery('q-task', button.data, 123)
    rig.conductor.handle(replace(msg(20001, '', principal=ZWEITER), callback=click))
    assert not path.exists() and rig.approvals.get(CHAT) is not None
    threads = [threading.Thread(target=rig.conductor.handle,
                                args=(replace(msg(i, ''), callback=click),)) for i in (20002, 20003)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert path.exists()
    successes = [r for r in rig.log.recent(200, ())
                 if r['type'] == 'exec.result' and r['payload']['status'] == 'done']
    assert len(successes) == 1


def test_bare_task_words_and_model_text_never_grant(tmp_path):
    first = tmp_path / 'first'
    rig = Rig(tmp_path, Scripted('allow this task', write_to(first)))
    rig.say('allow this task')
    assert rig.conductor.reasoner.calls == 0
    rig.say('Say something')
    rig.say('Create a file')
    assert not first.exists()
    assert rig.approvals.get(CHAT) is not None


def test_task_consent_still_checks_initial_target_binding(tmp_path):
    path = tmp_path / 'first'
    rig = Rig(tmp_path, Scripted(write_to(path), 'Done.'))
    rig.say('Create a file')
    task_id = rig.approvals.get(CHAT).task_id
    path.write_text('changed while waiting')
    rig.say('allow this task')
    assert path.read_text() == 'changed while waiting'
    assert rig.conductor.task_approvals.state(task_id, OWNER, CHAT) is None
    assert 'approval.task_granted' not in rig.types()


def test_different_shell_commands_run_under_one_approval(tmp_path):
    from talos import sandbox
    if sandbox.select_backend(sandbox.default_backends()) is None:
        pytest.skip('no OS sandbox available for live shell proof')
    shell = sandbox.SandboxedShell(workspace=tmp_path)
    commands = ['printf first > first.txt', 'printf second > second.txt', 'cat first.txt second.txt']
    rig = Rig(tmp_path, Scripted(*(call('run_shell', {'command': c}) for c in commands), 'Verified.'))
    runners = dict(rig.executor.runner.runners)
    runners['run_shell'] = lambda req: shell.run(req.args['command']).stdout
    executor = replace(rig.executor, runner=GrantedRunner(mint=rig.executor.mint, runners=runners))
    rig.conductor = replace(rig.conductor, executor=executor)
    rig.say('Create and check two files with shell commands')
    rig.say('allow this task')
    assert (tmp_path / 'first.txt').read_text() == 'first'
    assert (tmp_path / 'second.txt').read_text() == 'second'
    assert rig.types().count('approval.parked') == 1
    assert rig.types().count('approval.task_used') == 3


def test_stop_before_task_approval_invalidates_the_pending_decision(tmp_path):
    path = tmp_path / 'first'
    rig = Rig(tmp_path, Scripted(write_to(path), 'Done.'))
    rig.say('Create a file')
    rig.say('/stop')
    rig.say('allow this task')
    rig.say('yes')
    assert not path.exists() and rig.approvals.get(CHAT) is None
