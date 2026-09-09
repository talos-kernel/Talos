"""Production router concurrency, cancellation, selection and cooldown boundaries."""
import threading

import pytest

from talos.eventlog import EventLog
from talos.provider import ModelRouter, ModelSelection, Provider, ProviderRegistry
from talos.provider_errors import ReasonerFailure
from test_standing_flow import OWNER


def test_fork_has_independent_process_cancellation_and_fixed_selection(tmp_path):
    entered, released = threading.Event(), threading.Event()
    instances = []
    class Backend:
        def __init__(self, selection):
            self.selection, self.cancelled, self.probes = selection, 0, 0
        def validate(self):
            self.probes += 1
        def reason(self, prompt):
            if prompt == 'main':
                entered.set()
                assert released.wait(3)
            return self.selection.model + ':' + prompt
        def cancel(self):
            self.cancelled += 1
            released.set()
            return True
    def build(selection):
        instance = Backend(selection)
        instances.append(instance)
        return instance
    log = EventLog(tmp_path / 'events.db')
    registry = ProviderRegistry([Provider('fixture', 'Fixture', ('one', 'two'))])
    main = ModelRouter(registry, ModelSelection('fixture', 'one'), build, log)
    results = []
    thread = threading.Thread(target=lambda: results.append(main.reason('main')))
    thread.start()
    try:
        assert entered.wait(3)
        side = main.fork()
        assert side.current == main.current
        assert len(instances) == 2 and sum(x.probes for x in instances) == 0
        assert side.reason('side') == 'one:side'
        assert not released.is_set()
        assert main.cancel()
        thread.join(3)
        assert instances[0].cancelled == 1 and instances[1].cancelled == 0
        assert main.select('fixture', 'two', principal=OWNER).ok
        assert side.current.model == 'one' and side.reason('still side') == 'one:still side'
        assert len([r for r in log.recent(50, ()) if r['type'] == 'model.selected']) == 1
    finally:
        released.set()
        thread.join(3)


def test_fork_preserves_provider_cooldown_and_does_not_call_it(tmp_path):
    class Broken:
        calls = 0
        def reason(self, prompt):
            Broken.calls += 1
            raise ReasonerFailure('Limit', kind='rate_limited')
    registry = ProviderRegistry([Provider('fixture', 'Fixture', ('one',))])
    main = ModelRouter(registry, ModelSelection('fixture', 'one'), lambda _: Broken(), EventLog(tmp_path/'e.db'))
    with pytest.raises(ReasonerFailure):
        main.reason('main')
    side = main.fork()
    with pytest.raises(ReasonerFailure):
        side.reason('side')
    assert Broken.calls == 1
    assert side.readiness()['retry_in_s'] > 0


def test_fork_during_model_switch_is_refused_without_persisting(tmp_path):
    registry = ProviderRegistry([Provider('fixture', 'Fixture', ('one',))])
    main = ModelRouter(registry, ModelSelection('fixture','one'), lambda _: object(), EventLog(tmp_path/'e.db'))
    main._switching = True
    with pytest.raises(RuntimeError, match='selection is changing'):
        main.fork()
