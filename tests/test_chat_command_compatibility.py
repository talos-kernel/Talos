"""Actual conductor command paths, simultaneous jobs, and isolation boundaries."""
from dataclasses import replace
import threading
import time

import pytest

from talos.channel import Trust
from talos.fast_eval import FastEvalEngine
from talos.schedule import UnattendedCeiling
from test_standing_flow import Rig, Scripted, OWNER, ZWEITER, CHAT, msg, call


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


def background_rig(tmp_path, reasoner):
    rig = Rig(tmp_path, reasoner)
    ceiling = UnattendedCeiling()
    object.__setattr__(rig.executor.policy, 'unattended', ceiling)
    object.__setattr__(rig.conductor, 'unattended', ceiling)
    object.__setattr__(rig.commands, 'background', rig.conductor.background)
    return rig


@pytest.mark.parametrize('alias', ['background', 'bg', 'btw'])
def test_background_alias_preserves_foreground_memory_and_steering(tmp_path, alias):
    entered, release = threading.Event(), threading.Event()
    class Reasoner:
        def reason(self, prompt):
            assert 'private foreground context' not in prompt
            entered.set()
            assert release.wait(5)
            return 'The independent result.'
    rig = background_rig(tmp_path, Reasoner())
    rig.conductor.memory.remember(CHAT, asked='private foreground context', answered='kept')
    before = rig.conductor.memory.recall(CHAT)
    rig.conductor.redirect.open(str(OWNER), CHAT)
    try:
        assert rig.conductor.is_inline(msg(1, f'/{alias} independent task'))
        rig.say(f'/{alias} independent task')
        assert entered.wait(5)
        release.set()
        wait_for(lambda: rig.conductor.background.busy() == 0)
        assert rig.conductor.redirect.is_open(), 'side job closed the foreground mailbox'
        assert rig.conductor.memory.recall(CHAT) == before, 'side job polluted main history'
        assert any('The independent result.' in text for _, text in rig.sent)
    finally:
        release.set()


def test_explicit_steer_reaches_only_its_owner_and_chat(tmp_path):
    rig = Rig(tmp_path, Scripted('unused'))
    rig.conductor.redirect.open(str(OWNER), CHAT)
    assert 'Accepted' in rig.say('/steer use the second file')
    assert [c.text for c in rig.conductor.redirect.take()] == ['use the second file']
    assert 'not accepted' in rig.say('/steer foreign', principal=ZWEITER).lower()
    assert 'not accepted' in rig.say('/steer other chat', conversation='telegram:other').lower()
    assert rig.conductor.redirect.take() == ()
    assert rig.conductor.reasoner.calls == 0


def test_background_status_stop_and_steer_are_owner_scoped(tmp_path):
    rig = background_rig(tmp_path, Scripted('unused'))
    desk = rig.conductor.background
    own = desk.accept('own task', run_id='own', principal=str(OWNER), conversation=CHAT)
    other = desk.accept('private foreign task', run_id='other', principal=str(ZWEITER), conversation=CHAT)
    assert own.task_id in rig.say('/tasks')
    assert 'private foreign task' not in rig.say('/tasks')
    assert 'Accepted' in rig.say(f'/steer {own.task_id} inspect the receipt')
    assert [s.text for s in desk.take_steering(own.task_id)] == ['inspect the receipt']
    assert 'not accepted' in rig.say(f'/steer {other.task_id} inject').lower()
    assert 'not found' in rig.say(f'/cancel {other.task_id}').lower()
    assert not desk.was_cancelled(other.task_id)
    rig.conductor.redirect.open(str(OWNER), CHAT)
    rig.conductor.task_approvals.open('foreground', OWNER, CHAT)
    rig.conductor.task_approvals.approve('foreground', OWNER, CHAT)
    assert 'Stopping' in rig.say(f'/stop {own.task_id}')
    assert desk.was_cancelled(own.task_id)
    assert rig.conductor.redirect.is_open()
    assert rig.conductor.task_approvals.state('foreground', OWNER, CHAT) is True


def test_failed_background_provider_sends_a_terminal_report(tmp_path):
    class Broken:
        def reason(self, prompt):
            raise RuntimeError('fixture provider unavailable')
    rig = background_rig(tmp_path, Broken())
    rig.say('/btw inspect this')
    wait_for(lambda: rig.conductor.background.busy() == 0)
    assert any('Background' in text and 'failed' in text.lower() for _, text in rig.sent)
    assert not rig.conductor.memory.recall(CHAT)
    assert rig.approvals.get(CHAT) is None


def test_background_never_inherits_foreground_task_consent(tmp_path):
    target = tmp_path / 'must-not-exist'
    rig = background_rig(tmp_path, Scripted(call('write_file', {'path': str(target), 'content': 'no'})))
    rig.conductor.task_approvals.open('foreground', OWNER, CHAT)
    rig.conductor.task_approvals.approve('foreground', OWNER, CHAT)
    rig.say('/btw write a file')
    wait_for(lambda: rig.conductor.background.busy() == 0)
    assert not target.exists()
    assert rig.approvals.get(CHAT) is None
    assert rig.conductor.task_approvals.state('foreground', OWNER, CHAT) is True


def test_whoami_help_approval_aliases_do_not_call_a_provider(tmp_path):
    rig = Rig(tmp_path, Scripted('unused'))
    who = rig.say('/whoami')
    assert str(OWNER) in who and CHAT in who and 'Allowed: yes' in who
    assert '/btw' in rig.say('/help background')
    assert '/whoami' not in rig.say('/help background')
    assert rig.say('/approvals') == rig.say('/pending')
    assert rig.conductor.reasoner.calls == 0


@pytest.mark.parametrize('command', ['/steer change direction', '/cancel bg_one', '/stopall', '/estop'])
def test_control_commands_fail_closed_on_ask_channels(tmp_path, command):
    rig = Rig(tmp_path, Scripted('unused'), trust_of=lambda _: Trust.ASK)
    rig.conductor.redirect.open(str(OWNER), CHAT)
    rig.say(command)
    assert 'control.rejected' in rig.types()
    assert rig.conductor.redirect.take() == ()
    assert rig.conductor.reasoner.calls == 0


def test_eval_is_local_and_does_not_need_full_channel_trust(tmp_path):
    rig = Rig(tmp_path, Scripted('unused'), trust_of=lambda _: Trust.ASK)
    object.__setattr__(rig.commands, 'evaluator', FastEvalEngine())

    assert rig.conductor.is_inline(msg(900, '/eval public statement')) is True
    rig.say('/eval public statement')

    assert 'Fast evaluation (local)' in rig.sent[-1][1]
    assert 'no external call' in rig.sent[-1][1]
    assert rig.conductor.reasoner.calls == 0


def test_shared_fast_preflight_runs_before_any_reasoner_provider(tmp_path):
    rig = Rig(tmp_path, Scripted('The provider answer.'))
    object.__setattr__(rig.conductor, 'fast_eval', FastEvalEngine())

    assert rig.say('Please check the service') == 'The provider answer.'

    events = rig.conductor.log.recent(50)
    preflight = [event for event in events if event['type'] == 'eval.preflight']
    assert len(preflight) == 1
    assert preflight[0]['payload'] == {
        'category': 'instruction',
        'confidence': 0.91,
        'duration_ms': preflight[0]['payload']['duration_ms'],
        'external_call': False,
        'authority': 'advisory',
    }
    assert 'Please check the service' not in str(preflight[0]['payload'])
    assert rig.conductor.reasoner.calls == 1


def test_shared_fast_preflight_failure_does_not_block_provider(tmp_path):
    class BrokenEval:
        def classify(self, _text):
            raise RuntimeError('fixture failure')

    rig = Rig(tmp_path, Scripted('The provider answer.'))
    object.__setattr__(rig.conductor, 'fast_eval', BrokenEval())

    assert rig.say('Continue normally') == 'The provider answer.'
    assert rig.conductor.reasoner.calls == 1
    failed = [event for event in rig.conductor.log.recent(50)
              if event['type'] == 'eval.preflight_failed']
    assert failed[0]['payload'] == {'error': 'RuntimeError'}


def test_shared_fast_preflight_never_authorizes_a_tool(tmp_path):
    target = tmp_path / 'must-still-need-approval'
    rig = Rig(tmp_path, Scripted(call('write_file', {
        'path': str(target),
        'content': 'no',
    }, [str(target)])))
    object.__setattr__(rig.conductor, 'fast_eval', FastEvalEngine())

    rig.say('Please write the file')

    assert not target.exists()
    assert rig.approvals.get(CHAT) is not None
    assert rig.conductor.reasoner.calls == 1
    assert any(event['type'] == 'eval.preflight'
               for event in rig.conductor.log.recent(50))
