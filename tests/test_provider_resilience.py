import json
import threading
import time

import pytest

from talos.channel import Principal
from talos.eventlog import EventLog
from talos.provider import ModelRouter, ModelSelection, Provider, ProviderRegistry
from talos.provider_errors import ReasonerFailure, cli_failure
from talos.fallback import FallbackReasoner


def router(tmp_path, build):
    reg = ProviderRegistry([Provider('test', 'Test', ('limited', 'healthy'))])
    return ModelRouter(reg, ModelSelection('test', 'limited'), build,
                       EventLog(tmp_path / 'events.db'))


def empty():
    return cli_failure('The API returned an empty response.', '', 75,
                       provider='kimi-cli', model='test-model')


def test_boot_does_not_probe_and_failed_model_can_be_replaced(tmp_path):
    probes = []
    class Fake:
        def __init__(self, selection): self.selection = selection
        def validate(self):
            probes.append(self.selection.model)
            if self.selection.model == 'limited':
                raise ReasonerFailure('limit', kind='rate_limited', fallback_allowed=False)
        def reason(self, prompt):
            if self.selection.model == 'limited':
                raise ReasonerFailure('limit', kind='rate_limited', fallback_allowed=False)
            return 'healthy answer'
    r = router(tmp_path, Fake)
    assert probes == []
    with pytest.raises(ReasonerFailure): r.reason('hello')
    assert r.can_select() and r.readiness()['state'] == 'unavailable'
    assert r.select('test', 'healthy', principal=Principal('telegram', '7')).ok
    assert probes == ['healthy']
    assert r.reason('hello') == 'healthy answer'


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_structured_error_is_safe_and_identical_on_both_streams(stream):
    raw = 'TALOS_PROVIDER_ERROR ' + json.dumps({'type':'provider_error',
          'kind':'empty_response', 'prompt':'private prompt', 'token':'secret-fixture'})
    e = cli_failure(raw if stream == 'stdout' else '', raw if stream == 'stderr' else '',
                    75, provider='kimi-cli', model='test-model')
    assert e.kind == 'empty_response' and e.exit_code == 75
    assert not e.fallback_allowed
    assert 'private prompt' not in str(e) + json.dumps(e.event_data())
    assert 'secret-fixture' not in str(e) + json.dumps(e.event_data())


def test_failed_cli_does_not_expose_unknown_output():
    e = cli_failure('private entire prompt', 'Authorization: Bearer secret-fixture',
                    1, provider='test', model='test-model')
    assert e.kind == 'unknown'
    assert 'secret-fixture' not in str(e)
    assert 'private entire prompt' not in str(e)


def test_model_retry_never_replays_completed_tool(tmp_path):
    calls, tools, budgets = [], [], []
    class Fake:
        timeout_s = 10
        def reason_strict(self, prompt, *, timeout_s):
            calls.append(prompt); budgets.append(timeout_s)
            if len(calls) == 1: return 'proposal'
            if len(calls) == 2: raise empty()
            return 'confirmed'
    r = router(tmp_path, lambda _: Fake())
    assert r.reason('start') == 'proposal'
    tools.append('persisted-effect')
    assert r.reason('tool receipt: persisted-effect') == 'confirmed'
    assert tools == ['persisted-effect']
    assert calls == ['start', 'tool receipt: persisted-effect', 'tool receipt: persisted-effect']
    assert budgets[2] < budgets[1]


def test_two_empty_responses_stop_and_cooldown_does_not_call_again(tmp_path):
    calls = []
    class Fake:
        timeout_s = 10
        def reason_strict(self, prompt, *, timeout_s):
            calls.append(prompt); raise empty()
    r = router(tmp_path, lambda _: Fake())
    for _ in range(2):
        with pytest.raises(ReasonerFailure): r.reason('one request')
    assert calls == ['one request', 'one request']
    assert r.readiness()['retry_in_s'] > 0


def test_cancel_during_retry_wait_does_not_start_second_attempt(tmp_path):
    failed = threading.Event(); calls = []
    class Fake:
        timeout_s = 10
        def reason_strict(self, prompt, *, timeout_s):
            calls.append(prompt); failed.set(); raise empty()
        def cancel(self): return False
    r = router(tmp_path, lambda _: Fake()); results = []
    t = threading.Thread(target=lambda: results.append(r.reason('one'))); t.start()
    assert failed.wait(2)
    assert r.cancel()
    t.join(2)
    from talos.reasoner import CANCELLED_TEXT
    assert results == [CANCELLED_TEXT] and calls == ['one']
    assert r.can_select()


def test_no_retry_after_visible_partial_output_or_expired_budget(tmp_path):
    calls = []
    class Fake:
        timeout_s = 0.5
        def reason_strict(self, prompt, on_text=None, *, timeout_s):
            calls.append(prompt)
            if on_text: on_text('partial')
            raise empty()
    r = router(tmp_path, lambda _: Fake())
    with pytest.raises(ReasonerFailure): r.reason('one', on_text=lambda _: None)
    assert calls == ['one']


def test_cli_classification_does_not_enable_existing_provider_fallback(tmp_path):
    class Fake:
        def reason(self, prompt, on_text=None): raise empty()
    hops = []
    chain = FallbackReasoner(Fake(), (ModelSelection('other', 'model'),),
                           lambda x: hops.append(x), EventLog(tmp_path/'fallback.db'))
    with pytest.raises(ReasonerFailure): chain.reason('one')
    assert hops == []
