import json
import pytest
from talos import proposal
from talos.agent_loop import run_agent, AgentStatus
from test_agent_loop import _executor, OWNER


@pytest.mark.parametrize("tool", ["run_shell","remote_exec","vault_search","read_file","delegate_code","computer_run"])
def test_empty_argument_proposals_are_repaired_before_execution(tmp_path,tool):
    executor = _executor(tmp_path)
    path=tmp_path/'proof.txt';path.write_text('proof')
    replies=iter(['TOOL_CALL: '+json.dumps({'tool':tool,'args':{}}),
                  'TOOL_CALL: '+json.dumps({'tool':'read_file','args':{'path':str(path)}}), 'Read proof.'])
    result=run_agent(lambda history:next(replies),executor,OWNER,'repair')
    assert result.status is AgentStatus.ANSWERED
    records=executor.log.recent(20)
    assert [r['payload']['tool'] for r in records if r['type']=='exec.intent']==['read_file']
    assert sum(r['type']=='protocol.repair' for r in records)==1
    assert 'proof' in ' '.join(result.history)


def test_invalid_proposals_stop_after_two_repairs_without_effects(tmp_path):
    executor=_executor(tmp_path)
    result=run_agent(lambda _: 'TOOL_CALL: {"tool":"remote_exec","args":{}}',executor,OWNER,'invalid')
    assert result.status is AgentStatus.STEP_LIMIT and result.steps==3
    assert not any(r['type']=='exec.intent' for r in executor.log.recent(20))


def test_repair_does_not_invent_values_or_reclassify_a_denial(tmp_path):
    executor=_executor(tmp_path)
    replies=iter(['TOOL_CALL: {"tool":"remote_exec","args":{}}',
                  'TOOL_CALL: {"tool":"read_file","args":{"path":"/etc/shadow"}}','Blocked.'])
    result=run_agent(lambda _:next(replies),executor,OWNER,'deny')
    assert any(r['type']=='exec.result' and r['payload']['status']=='denied' for r in executor.log.recent(20))
    assert result.status is AgentStatus.ANSWERED


def test_legitimate_empty_argument_tools_and_empty_files_are_unchanged():
    assert not proposal.problem('undo_last',{})
    assert not proposal.problem('computer_status',{})
    assert not proposal.problem('write_file',{'path':'/tmp/x','content':''})


@pytest.mark.parametrize('empty',['','(leere Antwort)','(Empty answer.)'])
def test_empty_model_answer_recovers_without_replaying_an_effect(tmp_path,empty):
    executor=_executor(tmp_path)
    path=tmp_path/'once.txt'
    replies=iter(['TOOL_CALL: '+json.dumps({'tool':'write_file','args':{'path':str(path),'content':'once'}}),empty,'Saved once.'])
    result=run_agent(lambda _:next(replies),executor,OWNER,'empty')
    assert result.status is AgentStatus.ANSWERED and path.read_text()=='once'
    assert sum(r['type']=='exec.intent' for r in executor.log.recent(20))==1


def test_empty_model_answer_retry_is_bounded(tmp_path):
    result=run_agent(lambda _:'',_executor(tmp_path),OWNER,'empty')
    assert result.status is AgentStatus.STEP_LIMIT and result.steps==2


@pytest.mark.parametrize('broken', [
    'TOOL_CALL: {"tool":"read_file","args":',
    'TOOL_CALL: {"tool":"read_file","args":[]} ',
    'TOOL_CALL: {"tool":"read_file","args":{"path":"secret-value"}}\n'
    'TOOL_CALL: {"tool":"read_file","args":{"path":"second-value"}}',
])
def test_malformed_reply_never_executes_a_partial_batch_or_leaks_arguments(tmp_path, broken):
    executor = _executor(tmp_path)
    replies = iter([broken, 'No request was sent.'])
    result = run_agent(lambda _: next(replies), executor, OWNER, 'malformed')
    records = executor.log.recent(20)
    assert result.status is AgentStatus.ANSWERED
    assert not any(r['type'] == 'exec.intent' for r in records)
    assert sum(r['type'] == 'protocol.repair' for r in records) == 1
    assert 'secret-value' not in json.dumps(records)


def test_malformed_repair_retains_receipts_without_replaying_writes(tmp_path):
    executor = _executor(tmp_path)
    target = tmp_path / 'once.txt'
    replies = iter([
        'TOOL_CALL: ' + json.dumps({'tool': 'write_file', 'args': {'path': str(target), 'content': 'once'}}),
        'TOOL_CALL: {broken',
        'Saved once.',
    ])
    histories = []
    def propose(history):
        histories.append(tuple(history))
        return next(replies)
    result = run_agent(propose, executor, OWNER, 'write-repair')
    assert result.status is AgentStatus.ANSWERED and target.read_text() == 'once'
    assert 'write_file' in '\n'.join(histories[-1])
    assert sum(r['type'] == 'exec.intent' for r in executor.log.recent(20)) == 1


def test_malformed_repair_is_bounded_and_respects_stop(tmp_path):
    executor = _executor(tmp_path)
    result = run_agent(lambda _: 'TOOL_CALL: {broken', executor, OWNER, 'bounded')
    assert result.status is AgentStatus.STEP_LIMIT and result.steps == 3
    stopped = [False]
    def propose(_):
        stopped[0] = True
        return 'TOOL_CALL: {broken'
    result = run_agent(propose, executor, OWNER, 'stop', should_stop=lambda: stopped[0])
    assert result.steps == 1
    assert not any(r['type'] == 'exec.intent' for r in executor.log.recent(30))


def test_malformed_repair_does_not_reclassify_kernel_denial(tmp_path):
    executor = _executor(tmp_path)
    replies = iter(['TOOL_CALL: {broken',
                    'TOOL_CALL: {"tool":"read_file","args":{"path":"/etc/shadow"}}', 'Blocked.'])
    run_agent(lambda _: next(replies), executor, OWNER, 'deny-repaired')
    records = executor.log.recent(20)
    assert sum(r['type'] == 'protocol.repair' for r in records) == 1
    assert any(r['type'] == 'exec.result' and r['payload']['status'] == 'denied' for r in records)


def test_tool_examples_in_prose_are_not_repair_requests(tmp_path):
    explanation = 'The protocol looks like this:\n```\nTOOL_CALL: {example}\n```'
    result = run_agent(lambda _: explanation, _executor(tmp_path), OWNER, 'explain')
    assert result.text == explanation and result.steps == 1


def test_inference_repair_does_not_replay_an_uncertain_committed_write(tmp_path):
    from dataclasses import replace
    from talos.capability import CapabilityMint, GrantedRunner
    executor = _executor(tmp_path)
    target = tmp_path / 'ledger.txt'
    attempts = []
    def uncertain_write(req):
        attempts.append(req)
        target.write_text('committed')
        raise TimeoutError('response lost after commit')
    # Shell/remote effects cannot be rolled back by a write_file snapshot. Simulate
    # their lost response with a local ledger and the normal sandboxed-shell policy.
    policy = replace(executor.policy, shell_needs_human=False)
    mint = CapabilityMint(policy)
    runner = GrantedRunner(mint=mint, runners={
        **executor.runner.runners, 'run_shell': uncertain_write,
    })
    executor = replace(executor, policy=policy, mint=mint, runner=runner)
    replies = iter([
        'TOOL_CALL: {"tool":"run_shell","args":{"command":"python3 submit.py"}}',
        'TOOL_CALL: {broken',
        'TOOL_CALL: ' + json.dumps({'tool': 'read_file', 'args': {'path': str(target)}}),
        'The read-back confirms the commit.',
    ])
    result = run_agent(lambda _: next(replies), executor, OWNER, 'uncertain')
    assert result.status is AgentStatus.ANSWERED
    assert target.read_text() == 'committed' and len(attempts) == 1
    records = executor.log.recent(30)
    assert [r['payload']['tool'] for r in records if r['type'] == 'exec.intent'] == ['run_shell', 'read_file']
