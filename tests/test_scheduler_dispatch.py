import json
import threading
from types import SimpleNamespace

import pytest

from talos.channel import ChannelRegistry, Trust
from talos.continuity import Continuity
from talos.schedule import ScheduleStore, UnattendedCeiling
from talos.scheduler import dispatch_due, start_scheduler
from test_conductor import _build, OWNER, CHAT_OWNER, ScriptedReasoner


def rig(tmp_path, *, once=False, cron="", registry=None):
    path = tmp_path / "proof.txt"
    path.write_text("scheduler proof")
    replies = ScriptedReasoner('TOOL_CALL: ' + json.dumps(
        {'tool': 'read_file', 'args': {'path': str(path)}}), 'Read scheduler proof.')
    registry = registry if registry is not None else ChannelRegistry((
        SimpleNamespace(name='telegram', trust=Trust.FULL),))
    conductor, sent = _build(tmp_path, replies, trust_of=registry.trust_of)
    store = ScheduleStore(tmp_path / 'schedules.db')
    task = store.add(conversation=CHAT_OWNER, principal=str(OWNER), prompt='Check proof',
                     interval_s=0 if cron else 60, once=once, cron=cron, now=0)
    deps = dict(schedules=store, registry=registry, allowed_principals=frozenset({OWNER}),
                unattended=UnattendedCeiling(), conductor=conductor, log=conductor.log,
                continuity=Continuity(schedules=store, log=conductor.log,
                                      execute=conductor.executor.run))
    return deps, task, sent, replies


@pytest.mark.parametrize('once,cron', [(False, ''), (True, ''), (False, '* * * * *')])
def test_cli_cannot_consume_slot_service_executes_once_and_restart_does_not_replay(tmp_path, once, cron):
    deps, task, sent, model = rig(tmp_path, once=once, cron=cron)
    second = ScheduleStore(tmp_path / 'schedules.db')
    cli = dict(deps, schedules=second, registry=ChannelRegistry())
    assert start_scheduler(service_mode=False, **cli) is None
    dispatch_due(**cli, now=task.next_run)
    assert len(second.due(now=task.next_run)) == 1 and model.calls == 0
    dispatch_due(**deps, now=task.next_run)
    assert len(sent) == 1 and sent[0][0] == CHAT_OWNER
    assert sent[0][1].startswith('Read scheduler proof.')
    assert model.calls == 2
    restarted = ScheduleStore(tmp_path / 'schedules.db')
    dispatch_due(**dict(deps, schedules=restarted), now=task.next_run)
    assert model.calls == 2 and len(sent) == 1
    records = deps['log'].recent(50)
    assert sum(r['type'] == 'exec.result' for r in records) == 1
    assert not any(r['type'] == 'task.rejected' for r in records)
    assert restarted.count() == (0 if once else 1)


@pytest.mark.parametrize('trust', [None, Trust.NOTIFY])
def test_unavailable_channel_leaves_one_shot_due(tmp_path, trust):
    registry = ChannelRegistry(() if trust is None else (
        SimpleNamespace(name='telegram', trust=trust),))
    deps, task, sent, model = rig(tmp_path, once=True, registry=registry)
    dispatch_due(**deps, now=task.next_run)
    assert len(deps['schedules'].due(now=task.next_run)) == 1
    assert model.calls == 0 and not sent


@pytest.mark.parametrize('once', [False, True])
def test_two_database_connections_claim_same_due_slot_only_once(tmp_path, once):
    deps, task, sent, model = rig(tmp_path, once=once)
    other = ScheduleStore(tmp_path / 'schedules.db')
    barrier = threading.Barrier(2)
    # Both dispatchers obtained the same due snapshot before either claims it.
    def due(**_):
        barrier.wait(timeout=5)
        return (task,)
    deps['schedules'].due = due
    other.due = due
    errors = []
    def dispatch(store):
        try:
            dispatch_due(**dict(deps, schedules=store), now=task.next_run)
        except Exception as error:
            errors.append(error)
    threads = [threading.Thread(target=dispatch, args=(s,)) for s in (deps['schedules'], other)]
    for t in threads: t.start()
    for t in threads: t.join(5)
    assert not errors and all(not t.is_alive() for t in threads)
    assert model.calls == 2 and len(sent) == 1
    assert sum(r['type'] == 'exec.result' for r in deps['log'].recent(50)) == 1


def test_revoked_owner_does_not_reach_model(tmp_path):
    deps, task, sent, model = rig(tmp_path)
    dispatch_due(**dict(deps, allowed_principals=frozenset()), now=task.next_run)
    assert model.calls == 0 and not sent
    assert any(r['type'] == 'schedule.refused' for r in deps['log'].recent(50))
