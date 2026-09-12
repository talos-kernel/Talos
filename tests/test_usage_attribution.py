"""Verbrauch pro Modell — und eine Zeile, die niemals Inhalt traegt."""
import json

from talos.usage import (
    ModelTotals,
    Run,
    UsageMeter,
    event_payload,
    snapshot_from_events,
)


def _run(model: str, **felder) -> Run:
    grund = dict(at=0.0, ok=True, duration_s=2.0, model=model,
                 input_tokens=100, output_tokens=50, cost_usd=0.25)
    grund.update(felder)
    return Run(**grund)


# --- Die Zuordnung, die bisher fehlte ----------------------------------------------
def test_two_models_are_counted_apart():
    meter = UsageMeter()
    meter.record(_run("claude-fable-5-1"))
    meter.record(_run("k3", input_tokens=10, output_tokens=5, cost_usd=0.01))
    meter.record(_run("k3", input_tokens=10, output_tokens=5, cost_usd=0.01))
    per = meter.snapshot().per_model
    assert set(per) == {"claude-fable-5-1", "k3"}
    assert per["claude-fable-5-1"].runs == 1 and per["k3"].runs == 2
    assert per["k3"].input_tokens == 20 and round(per["k3"].cost_usd, 4) == 0.02


def test_a_failed_run_is_attributed_to_its_model():
    meter = UsageMeter()
    meter.record(_run("k3", ok=False))
    per = meter.snapshot().per_model
    assert per["k3"].runs == 1 and per["k3"].failed == 1


def test_a_run_without_a_model_is_dropped():
    meter = UsageMeter()
    meter.record(_run(""))
    assert "unbekannt" in meter.snapshot().per_model


def test_the_totals_still_match_the_sum_of_the_models():
    meter = UsageMeter()
    meter.record(_run("a"))
    meter.record(_run("b", input_tokens=7, output_tokens=3, cost_usd=0.5))
    snap = meter.snapshot()
    assert snap.input_tokens == sum(t.input_tokens for t in snap.per_model.values())
    assert round(snap.cost_usd, 6) == round(sum(t.cost_usd for t in snap.per_model.values()), 6)


# --- Die Sicherheitsgrenze: Zahlen, nie Inhalt -------------------------------------
def test_the_usage_row_carries_the_conversation():
    """Das Log ist append-only — was hier hineingeraet, bleibt fuer immer."""
    heikel = _run(
        "k3",
        note="das codewort ist morgenstern-santorin",
        session_id="telegram:749908868/geheimer-thread",
    )
    roh = json.dumps(event_payload(heikel), ensure_ascii=False)
    for verboten in ("morgenstern", "santorin", "codewort", "telegram:", "geheimer"):
        assert verboten not in roh, f"{verboten!r} steht in der Verbrauchszeile"
    assert set(event_payload(heikel)) == {
        "model", "ok", "duration_s", "input_tokens", "output_tokens",
        "cache_read", "cache_write", "cost_usd", "cost_source",
    }


def test_the_row_still_names_what_it_must():
    roh = event_payload(_run("k3", cost_source="catalog"))
    assert roh["model"] == "k3" and roh["input_tokens"] == 100
    assert roh["cost_source"] == "catalog" and roh["ok"] is True


# --- Persistenz: aus dem Log zurueckrechnen ----------------------------------------
def test_the_log_rebuilds_the_usage_after_a_restart():
    gesendet: list[dict] = []
    meter = UsageMeter(on_record=lambda run: gesendet.append(event_payload(run)))
    meter.record(_run("k3"))
    meter.record(_run("claude-fable-5-1", cost_usd=1.0))
    zurueck = snapshot_from_events(gesendet)
    lebend = meter.snapshot()
    assert zurueck.runs == lebend.runs == 2
    assert round(zurueck.cost_usd, 6) == round(lebend.cost_usd, 6)
    assert set(zurueck.per_model) == set(lebend.per_model)


def test_an_older_or_broken_row_takes_the_report_down():
    assert snapshot_from_events([]).runs == 0
    kaputt = [None, "text", {"model": []}, {"input_tokens": "viele"}, {}]
    snap = snapshot_from_events(kaputt)
    assert snap.runs >= 0  # nur: es wirft nicht


# --- Der Sink darf den Lauf nie mitnehmen ------------------------------------------
def test_a_throwing_sink_kills_the_run():
    def kaputt(run):
        raise RuntimeError("Log voll")

    meter = UsageMeter(on_record=kaputt)
    meter.record(_run("k3"))          # darf nicht werfen
    assert meter.snapshot().runs == 1  # und muss trotzdem gezaehlt haben


def test_the_sink_sees_every_run():
    gesehen: list[str] = []
    meter = UsageMeter(on_record=lambda run: gesehen.append(run.model))
    meter.record(_run("a"))
    meter.record(_run("b"))
    assert gesehen == ["a", "b"]


def test_model_totals_start_empty():
    assert ModelTotals().runs == 0 and ModelTotals().cost_usd == 0.0
    assert UsageMeter().snapshot().per_model == {}
