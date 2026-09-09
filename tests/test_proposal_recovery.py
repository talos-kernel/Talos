import json

import pytest

from talos import proposal
from talos.agent_loop import AgentStatus, run_agent
from talos.computer.contract import validate
from test_agent_loop import _executor, OWNER
from test_conductor import _build, msg


def call(tool, args):
    return 'TOOL_CALL: ' + json.dumps({'tool': tool, 'args': args})


@pytest.mark.parametrize('args,issue', [
    ({'op': 'exec', 'project': 'bad_project'}, 'project must contain'),
    ({'op': 'exec', 'project': 'proof', 'key': 'bad_key'}, 'operation key must contain'),
    ({'op': 'exec', 'project': 'proof', 'key': 'step', 'title': 'test',
      'command': 'true', 'timeout': 500}, 'timeout must be between'),
    ({'op': 'pause', 'secret-argument': 'PRIVATE_VALUE'}, 'unknown computer argument'),
])
def test_schema_feedback_is_specific_without_argument_values(args, issue):
    explanation = proposal.problem('computer_run', args)
    assert issue in explanation
    assert 'PRIVATE_VALUE' not in explanation and 'bad_project' not in explanation


def test_model_receives_rejected_arguments_and_can_correct_without_replaying_write(tmp_path):
    executor = _executor(tmp_path)
    target = tmp_path / 'once.txt'
    broken = {'op': 'exec', 'project': 'proof', 'key': 'bad_key', 'title': 'test', 'command': 'true'}
    turns = 0
    def propose(history):
        nonlocal turns
        turns += 1
        if turns == 1: return call('write_file', {'path': str(target), 'content': 'once'})
        if turns == 2: return call('computer_run', broken)
        assert 'operation key must contain' in history[-1]
        assert 'bad_key' in history[-1] and 'write_file -> done' in '\n'.join(history)
        corrected = dict(broken, key='valid-key')
        assert validate(corrected) == 'exec'
        return 'File is saved; the extra computer action is unnecessary.'
    result = run_agent(propose, executor, OWNER, 'correction')
    assert result.status is AgentStatus.ANSWERED and target.read_text() == 'once'
    events = executor.log.by_run('correction')
    assert sum(r['type'] == 'exec.result' for r in events) == 1
    repairs = [r for r in events if r['type'] == 'protocol.repair']
    assert 'operation key must contain' in repairs[0]['payload']['schema_issue']
    assert 'bad_key' not in json.dumps(repairs)


def test_failed_proposals_keep_request_and_receipt_for_question_without_restarting(tmp_path):
    target = tmp_path / 'once.txt'
    class Model:
        calls = 0
        def reason(self, prompt):
            self.calls += 1
            if self.calls == 1:
                return call('write_file', {'path': str(target), 'content': 'once'})
            if self.calls <= 4:
                return call('computer_run', {'op': 'exec', 'project': 'bad_project'})
            assert 'Save the file then inspect computer' in prompt
            assert 'project must contain' in prompt
            assert 'write_file -> done' in prompt and 'once.txt' in prompt
            assert 'status-only follow-up' in prompt
            return 'The file is saved. Computer inspection stopped on invalid project syntax.'
    model = Model()
    conductor, sent = _build(tmp_path, model)
    assert conductor.handle(msg(1, OWNER, 'Save the file then inspect computer'))
    # _build's standard policy may park a write; tests grant only this fixture's request.
    pending = conductor.approvals.get(f'telegram:{OWNER.user_id}')
    if pending:
        assert conductor.handle(msg(2, OWNER, 'yes'))
    assert 'Task unfinished:' in sent[-1][1]
    stopped_calls = model.calls
    assert conductor.handle(msg(3, OWNER, '?'))
    assert 'No retry was started' in sent[-1][1] and model.calls == stopped_calls
    assert conductor.handle(msg(4, OWNER, 'Continue from the existing receipt.'))
    assert 'file is saved' in sent[-1][1]
    assert target.read_text() == 'once'
    records = conductor.log.recent(100)
    assert sum(r['type'] == 'exec.result' and r['payload']['tool'] == 'write_file'
               and r['payload']['status'] == 'done' for r in records) == 1
