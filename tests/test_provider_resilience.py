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
    # WICHTIG: Hier stand `assert not e.fallback_allowed`. Seit dem 12.09. darf eine
    # leere Antwort die Kette ausloesen — sie ist eine Fehlfunktion, keine Ablehnung,
    # und ein anderer Anbieter hilft plausibel (siehe tests/test_fallback.py). Die
    # Zusicherung gehoerte hier ohnehin nicht hin: dieser Fall prueft REDAKTION.
    assert e.fallback_allowed is True
    assert 'private prompt' not in str(e) + json.dumps(e.event_data())
    assert 'secret-fixture' not in str(e) + json.dumps(e.event_data())


def test_failed_cli_does_not_expose_unknown_output():
    e = cli_failure('private entire prompt', 'Authorization: Bearer secret-fixture',
                    1, provider='test', model='test-model')
    assert e.kind == 'unknown'
    assert 'secret-fixture' not in str(e)
    assert 'private entire prompt' not in str(e)


@pytest.mark.parametrize('backend', ['claude', 'hermes'])
def test_probe_timeout_never_exposes_the_subprocess_prompt(tmp_path, monkeypatch, backend):
    import subprocess
    from talos.reasoner import ClaudeCliReasoner, HermesCliReasoner
    binary = tmp_path / 'fixture-provider'
    binary.write_text('#!/bin/sh\n[ "$1" = tools ] && echo "✗ disabled web"\n')
    binary.chmod(0o700)
    reasoner = (ClaudeCliReasoner(str(binary), 2) if backend == 'claude'
                else HermesCliReasoner(str(binary), 2, provider='fixture', model='one'))
    class TimedOut:
        calls = 0
        def communicate(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(['provider', '-z', 'secret-fixture-prompt'], 2)
            return '', ''
    monkeypatch.setattr('talos.reasoner.subprocess.Popen', lambda *a, **kw: TimedOut())
    monkeypatch.setattr('talos.reasoner._kill_group', lambda _: None)
    with pytest.raises(ReasonerFailure) as caught:
        reasoner.validate()
    assert caught.value.kind == 'timed_out'
    assert 'secret-fixture-prompt' not in str(caught.value)


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


def test_an_unclassifiable_cli_failure_still_never_hops(tmp_path):
    """Die Grenze, die bleibt: was sich NICHT sauber klassifizieren laesst, loest
    die Kette weiterhin nicht aus.

    Bis zum 12.09. hielt dieser Fall fest, dass eine CLI-Klassifikation *nie*
    weiterschaltet — mit `empty_response` als Beispiel. Das war zu breit: eine leere
    Antwort ist klassifiziert UND eine Fehlfunktion, dort hilft der naechste Anbieter.
    Geprueft wird jetzt der Fall, um den es wirklich ging: ein unklarer Fehler
    (`unknown`) bleibt am Platz, denn niemand weiss, ob ein Wechsel etwas heilt.
    """
    class Fake:
        def reason(self, prompt, on_text=None):
            raise cli_failure('irgendein Absturz', '', 1, provider='kimi-cli', model='k3')
    hops = []
    chain = FallbackReasoner(Fake(), (ModelSelection('other', 'model'),),
                           lambda x: hops.append(x), EventLog(tmp_path/'fallback.db'))
    with pytest.raises(ReasonerFailure): chain.reason('one')
    assert hops == []


def test_a_classified_empty_response_does_hop(tmp_path):
    """Der Gegenbeleg — sonst pruefte der Fall oben nur, dass die Kette nie greift."""
    class Fake:
        def reason(self, prompt, on_text=None):
            raise cli_failure('The API returned an empty response.', '', 75,
                              provider='kimi-cli', model='k3')
    hops = []

    def bauen(auswahl):
        hops.append(auswahl)
        class Sprung:
            def reason(self, prompt, on_text=None): return 'Ersatz antwortet.'
        return Sprung()

    chain = FallbackReasoner(Fake(), (ModelSelection('other', 'model'),),
                             bauen, EventLog(tmp_path/'fallback.db'))
    assert 'Ersatz antwortet.' in chain.reason('one')
    assert len(hops) == 1, 'die Kette ist nicht gesprungen'
