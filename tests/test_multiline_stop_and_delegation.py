"""Regression for multiline /btw and /stop followed by a still-steerable delegate."""
from dataclasses import replace
import threading

import pytest

from talos import tools, run_control
from talos.__main__ import delegate_propose
from talos.agent_loop import run_agent
from talos.commands import parse
from talos.provider import ModelRouter, ModelSelection, Provider, ProviderRegistry
from talos.subagent import ReadOnlyCeiling
from talos.worker import Worker
from test_standing_flow import Rig, Scripted, OWNER, CHAT, msg, call
from test_chat_command_compatibility import wait_for


@pytest.mark.parametrize('text,expected', [
    ('/btw\ndu hast jetzt Zugriff', ('btw', 'du hast jetzt Zugriff')),
    ('/btw@Talos_bot\n\nread this', ('btw', 'read this')),
    ('/stop\n\nWarum hat das so lange gedauert?', ('stop', 'Warum hat das so lange gedauert?')),
    ('/q\tsecond task', ('queue', 'second task')),
    ('/', ('','')),
])
def test_multiline_command_whitespace(text, expected):
    assert parse(text) == expected


@pytest.mark.parametrize('command', ['/stop\n\nWarum hat das so lange gedauert?', '/stopall', '/estop'])
def test_stop_closes_steering_immediately_and_disallows_a_late_tool(tmp_path, command):
    entered, release = threading.Event(), threading.Event()
    target = tmp_path / 'late-write'
    class Backend:
        def reason(self, prompt):
            entered.set()
            assert release.wait(3)
            return call('write_file', {'path': str(target), 'content': 'must not run'})
        def cancel(self):
            return False  # reproduce a backend that returns after cancellation
    rig = Rig(tmp_path, Backend(), level=5)
    worker = Worker(rig.conductor.handle)
    object.__setattr__(rig.conductor, 'commands', replace(rig.commands, worker=worker))
    worker.start()
    try:
        assert worker.submit(msg(20001,'Create the file'))
        assert entered.wait(3)
        rig.say(command)
        assert not rig.conductor.redirect.is_open()
        assert not rig.conductor.redirect.offer(str(OWNER),CHAT,'Why so long?')
        assert 'Stopping:' in rig.say('/queue')
        release.set()
        wait_for(lambda:not worker.busy())
        assert not target.exists()
        assert 'Nothing running' in rig.say('/queue')
        assert worker.pending()==0
    finally:
        release.set()
        worker.stop()


def test_an_old_cancelled_generation_cannot_revive_or_close_a_new_task():
    from talos.redirect import Redirect
    inbox=Redirect()
    old=inbox.open(str(OWNER),CHAT)
    inbox.close()
    new=inbox.open(str(OWNER),CHAT)
    assert not inbox.active(old) and inbox.active(new)
    inbox.close(old)
    assert inbox.active(new)


@pytest.mark.parametrize('cancel_parent', [False,True])
def test_delegate_deadline_and_parent_stop_cancel_only_its_own_model(tmp_path, cancel_parent):
    entered, release, stop = threading.Event(),threading.Event(),threading.Event()
    backends=[]
    class Backend:
        cancelled=0
        def reason(self,prompt):
            entered.set()
            assert release.wait(3)
            return call('write_file',{'path':str(tmp_path/'forbidden'),'content':'no'})
        def cancel(self):
            self.cancelled+=1
            release.set()
            return True
    def build(_):
        b=Backend();backends.append(b);return b
    rig=Rig(tmp_path,Scripted('unused'))
    registry=ProviderRegistry([Provider('fixture','Fixture',('one',))])
    router=ModelRouter(registry,ModelSelection('fixture','one'),build,rig.log)
    ceiling=ReadOnlyCeiling()
    object.__setattr__(rig.executor.policy,'delegated',ceiling)
    runner=tools.make_delegate_runner(executor=lambda:rig.executor,ceiling=ceiling,
        propose=delegate_propose(router),run_id=lambda:'child',timeout_s=2 if cancel_parent else .1)
    results=[]
    def invoke():
        with run_control.active(stop.is_set,reasoner=router):
            results.append(runner(__import__('talos.policy',fromlist=['ToolRequest']).ToolRequest(
                'delegate',OWNER,{'question':'Inspect a fixture'})))
    thread=threading.Thread(target=invoke)
    thread.start()
    try:
        assert entered.wait(3)
        if cancel_parent:stop.set()
        thread.join(3)
        assert not thread.is_alive()
        assert 'stopped' in results[0] and 'completion unverified' in results[0]
        assert len(backends)==2
        assert backends[0].cancelled==0 and backends[1].cancelled==1
        assert not (tmp_path/'forbidden').exists()
        assert not any(r['type']=='exec.intent' for r in rig.log.recent(50,()))
    finally:
        release.set();thread.join(3)


def test_recursive_delegation_is_denied_and_read_only_ceiling_stays_active(tmp_path):
    from talos.policy import ToolRequest, Verdict
    rig=Rig(tmp_path,Scripted('unused'),level=5)
    ceiling=ReadOnlyCeiling()
    object.__setattr__(rig.executor.policy,'delegated',ceiling)
    with ceiling.active():
        nested=rig.executor.policy.decide(ToolRequest('delegate',OWNER,{'question':'recurse'}))
        assert nested.verdict is Verdict.DENY and 'recursive' in nested.reason
        assert rig.executor.policy.decide(ToolRequest('write_file',OWNER,{'path':str(tmp_path/'x'),'content':'no'})).verdict is Verdict.DENY


def test_concurrent_delegate_invocations_keep_their_original_identity(tmp_path):
    from talos.policy import ToolRequest
    from test_standing_flow import ZWEITER
    from talos.executor import Outcome, Status
    from talos.eventlog import EventLog
    barrier=threading.Barrier(2)
    seen=[]
    class Executor:
        log=EventLog(tmp_path/'e.db')
        def run(self,req,run_id):
            seen.append(req.identity)
            return Outcome(Status.DONE,'read',result='contents')
    def propose(question):
        calls=0
        def next_(history):
            nonlocal calls
            calls+=1
            if calls==1:
                barrier.wait(3)
                return call('read_file',{'path':'fixture'})
            return 'Done'
        return next_
    runner=tools.make_delegate_runner(executor=lambda:Executor(),ceiling=ReadOnlyCeiling(),
                                     propose=propose,run_id=lambda:'child')
    threads=[threading.Thread(target=lambda who=who:runner(ToolRequest('delegate',who,{'question':'read'})))
             for who in [OWNER,ZWEITER]]
    for t in threads:t.start()
    for t in threads:t.join(3)
    assert sorted(map(str,seen))==sorted(map(str,[OWNER,ZWEITER]))
