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
