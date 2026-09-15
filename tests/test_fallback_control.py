"""Control commands must address the running hop, including background forks."""
import threading

from talos.api_reasoner import ReasonerFailure
from talos.channel import Principal
from talos.eventlog import EventLog
from talos.fallback import FallbackReasoner
from talos.provider import ModelSelection, ModelRouter, Provider, ProviderRegistry
from talos.reasoner import CANCELLED_TEXT

PRIMARY = ModelSelection('openai-api', 'primary')
HOP = ModelSelection('ollama', 'test-model')


class Limited:
    def reason_strict(self, prompt):
        raise ReasonerFailure('Test limit', kind='rate_limited')


class Blocking:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled = False

    def reason(self, prompt):
        self.started.set()
        assert self.release.wait(3), 'test cleanup did not release the call'
        return CANCELLED_TEXT if self.cancelled else 'completed'

    def cancel(self):
        self.cancelled = True
        self.release.set()
        return True


def wrapper(tmp_path, build):
    log = EventLog(tmp_path / 'events.db')
    router = ModelRouter(ProviderRegistry((Provider(PRIMARY.provider, 'Test', (PRIMARY.model,)),)),
                         PRIMARY, lambda _: Limited(), log)
    return FallbackReasoner(router, (HOP,), build, log)


def test_cancel_reaches_active_fallback_and_does_not_report_success(tmp_path):
    hop = Blocking()
    reasoner = wrapper(tmp_path, lambda _: hop)
    result = []
    thread = threading.Thread(target=lambda: result.append(reasoner.reason_strict('test')))
    thread.start()
    try:
        assert hop.started.wait(2)
        assert reasoner.cancel() is True
        thread.join(1)
        assert not thread.is_alive()
        assert result == [CANCELLED_TEXT]
        assert not any(row['payload'].get('outcome') == 'ok'
                       for row in reasoner._log.recent(20, ('model.fallback.runtime',)))
    finally:
        hop.release.set()
        thread.join(3)


def test_model_switch_is_busy_while_fallback_runs(tmp_path):
    hop = Blocking()
    reasoner = wrapper(tmp_path, lambda _: hop)
    thread = threading.Thread(target=lambda: reasoner.reason_strict('test'))
    thread.start()
    try:
        assert hop.started.wait(2)
        assert reasoner.can_select() is False
        switched = reasoner.select(PRIMARY.provider, PRIMARY.model,
                                   principal=Principal('telegram', 100000001))
        assert not switched.ok and 'busy' in switched.error
    finally:
        hop.release.set()
        thread.join(3)
    assert reasoner.can_select() is True


def test_background_fork_retains_configured_fallback(tmp_path):
    class Healthy:
        def reason(self, prompt): return 'background completed'
    reasoner = wrapper(tmp_path, lambda _: Healthy())
    child = reasoner.fork()
    assert child.reason('test').endswith('background completed')
    assert child.current == reasoner.current == PRIMARY
    assert not reasoner._log.recent(20, ('model.selected',))


def test_cancel_while_building_does_not_start_next_model(tmp_path):
    entered, release = threading.Event(), threading.Event()
    hop = Blocking()
    def build(_):
        entered.set()
        assert release.wait(3)
        return hop
    reasoner = wrapper(tmp_path, build)
    result = []
    thread = threading.Thread(target=lambda: result.append(reasoner.reason_strict('test')))
    thread.start()
    try:
        assert entered.wait(2)
        assert reasoner.cancel() is True
    finally:
        release.set()
        thread.join(3)
    assert result == [CANCELLED_TEXT] and not hop.started.is_set()


def test_cancelling_child_leaves_parent_hop_running(tmp_path):
    parent_hop, child_hop = Blocking(), Blocking()
    hops = iter((parent_hop, child_hop))
    parent = wrapper(tmp_path, lambda _: next(hops))
    parent_result, child_result = [], []
    main = threading.Thread(target=lambda: parent_result.append(parent.reason_strict('main')))
    main.start()
    side = None
    try:
        assert parent_hop.started.wait(2)
        child = parent.fork()
        side = threading.Thread(target=lambda: child_result.append(child.reason_strict('side')))
        side.start()
        assert child_hop.started.wait(2)
        assert child.cancel() is True
        side.join(1)
        assert child_result == [CANCELLED_TEXT] and not side.is_alive()
        assert main.is_alive() and not parent_hop.cancelled
        parent_hop.release.set()
        main.join(1)
        assert parent_result[0].endswith('completed')
    finally:
        parent_hop.release.set()
        child_hop.release.set()
        main.join(3)
        if side is not None: side.join(3)
