"""Die Laufzeit-Fallback-Kette: Reihenfolge, Belege und der alte Text ohne Kette.

Alles laeuft ueber die injizierte `http`-Abhaengigkeit — kein Test fasst das Netz an.
Der Primaer-Reasoner ist kein Double, sondern die echte Kette Registry → ModelRouter →
ApiReasoner, damit genau der Vertrag geprueft wird, den die Produktion faehrt: der
Router reicht `reason_strict` durch, und die Ausnahme traegt ihre Art nach oben.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from talos import configcli, schema
from talos.api_reasoner import ApiReasoner, ReasonerFailure
from talos.credentials import CredentialStore, Route
from talos.eventlog import EventLog
from talos.fallback import FALLBACK_EVENT, FallbackReasoner, parse_chain
from talos.provider import ModelSelection, ModelRouter, Provider, ProviderRegistry
from talos.usage import UsageMeter

OPENAI_KEY = "sk-proj-FALLBACK-TEST-111"


class FakeResponse:
    def __init__(self, lines: list[str], status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self._lines = lines

    def iter_lines(self, decode_unicode: bool = False):
        yield from self._lines

    def close(self) -> None:
        pass


class QueueHttp:
    """Antwortet der Reihe nach aus einer Warteschlange.

    ⚠️ Seit d4d7cda baut der Router beim Boot OHNE Probe (lazy validation):
    die READY-Marker-Antwort gehoert nicht mehr in die Queue des Primaer-
    Reasoners. Wer einen Lauf-Fehlschlag simuliert, legt die Fehler-Antwort
    an ERSTER Stelle — was beim Boot gefressen wuerde, frueher als Probe
    ausgegeben wurde, gibt es seit dem lazy Router nicht mehr."""

    def __init__(self, *responses: FakeResponse) -> None:
        self._queue = list(responses)
        self.calls = 0

    def post(self, url, *, headers, json, timeout, stream):  # noqa: A002 — Vertragsname
        self.calls += 1
        assert self._queue, "mehr Aufrufe als Antworten"
        return self._queue.pop(0)


def ok_lines(text: str) -> list[str]:
    import json as _json

    return [
        "data: " + _json.dumps({"choices": [{"delta": {"content": text}}]}),
        "data: [DONE]",
    ]


READY = FakeResponse(ok_lines("TALOS_READY"))
OPENAI_ROUTE = CredentialStore({"openai-api": Route("openai-api", OPENAI_KEY)})
OLLAMA_ROUTE = CredentialStore({"ollama": Route("ollama", "", "http://localhost:11434/v1")})

PRIMARY = ModelSelection("openai-api", "gpt-5.2")
OLLAMA_HOP = ModelSelection("ollama", "qwen3:27b")
NVIDIA_HOP = ModelSelection("nvidia-nim", "nvidia/llama-3.3-nemotron-super-49b-v1.5")


def registry() -> ProviderRegistry:
    return ProviderRegistry((Provider("openai-api", "OpenAI", ("gpt-5.2",)),))


def failing_primary(tmp_path: Path, status: int, meter: UsageMeter | None = None):
    """Router + ApiReasoner, dessen Lauf mit `status` endet.

    Der lazy Router (d4d7cda) baut beim Boot ohne Probe: der einzige HTTP-
    Aufruf ist der Lauf selbst, also traegt die Queue nur die Fehler-Antwort."""
    http = QueueHttp(FakeResponse([], status_code=status, text="fehler"))

    def build(selection: ModelSelection) -> ApiReasoner:
        return ApiReasoner(
            selection.provider, selection.model, OPENAI_ROUTE, timeout_s=5,
            meter=meter, http=http,
        )

    router = ModelRouter(registry(), PRIMARY, build, EventLog(tmp_path / "events.db"))
    return router, http


def ollama_build(answer: str, *, meter: UsageMeter | None = None, status: int = 200):
    """Ein Hop-Bauer wie `build_reasoner` in `__main__`: echter ApiReasoner, Fake-Http."""
    def build(selection: ModelSelection) -> ApiReasoner:
        if selection.provider == "ollama":
            return ApiReasoner(
                "ollama", selection.model, OLLAMA_ROUTE, timeout_s=5, meter=meter,
                http=QueueHttp(FakeResponse(ok_lines(answer), status_code=status,
                                            text="fehler")),
            )
        # Kein NVIDIA-Schluessel hinterlegt: der Bau MUSS hier scheitern (fail-closed).
        return ApiReasoner(
            selection.provider, selection.model,
            CredentialStore(), timeout_s=5, meter=meter, http=QueueHttp(),
        )

    return build


def events(log: EventLog, kind: str) -> list[dict]:
    return [row["payload"] for row in log.recent(50, (kind,))]


# --- Die Kette selbst ---------------------------------------------------------------


def test_parse_chain_reads_provider_slash_model_in_order() -> None:
    kette = parse_chain(
        "ollama/qwen3:27b, nvidia-nim/nvidia/llama-3.3-nemotron-super-49b-v1.5 , kaputt"
    )
    assert kette == (OLLAMA_HOP, NVIDIA_HOP)
    assert parse_chain("") == ()


def test_a_classified_failure_falls_back_and_says_so(tmp_path: Path) -> None:
    router, _http = failing_primary(tmp_path, 529)
    log = EventLog(tmp_path / "events.db")
    kette = FallbackReasoner(router, (OLLAMA_HOP,), ollama_build("Ollama antwortet."), log)

    antwort = kette.reason("Status?")

    assert antwort.startswith("(Fallback: ollama/qwen3:27b — Grund: Primär-Provider überlastet)\n")
    assert antwort.endswith("Ollama antwortet.")
    belege = events(log, FALLBACK_EVENT)
    assert len(belege) == 1
    assert belege[0]["to"] == "ollama/qwen3:27b"
    assert belege[0]["from"] == "openai-api/gpt-5.2"
    assert belege[0]["kind"] == "overloaded"
    assert belege[0]["outcome"] == "ok"


def test_the_order_of_the_chain_is_the_order_of_the_attempts(tmp_path: Path) -> None:
    router, _http = failing_primary(tmp_path, 429)
    log = EventLog(tmp_path / "events.db")
    besucht: list[str] = []

    def build(selection: ModelSelection) -> ApiReasoner:
        besucht.append(f"{selection.provider}/{selection.model}")
        return ollama_build("vom zweiten Hop")(selection)

    kette = FallbackReasoner(router, (NVIDIA_HOP, OLLAMA_HOP), build, log)
    antwort = kette.reason("x")

    # nvidia-nim hat keinen Schluessel im Bestand: uebersprungen, belegt, kein Absturz.
    assert besucht == ["nvidia-nim/nvidia/llama-3.3-nemotron-super-49b-v1.5", "ollama/qwen3:27b"]
    assert "vom zweiten Hop" in antwort
    belege = events(log, FALLBACK_EVENT)
    assert [b["outcome"] for b in belege] == ["skipped", "ok"]
    assert belege[0]["to"].startswith("nvidia-nim/")
    assert "NVIDIA_API_KEY" in belege[0]["detail"]


def test_a_factual_4xx_never_triggers_the_chain(tmp_path: Path) -> None:
    """HTTP_FAILED heisst: das Modell hat verstanden und abgelehnt — niemand sonst fragen."""
    router, _http = failing_primary(tmp_path, 400)
    log = EventLog(tmp_path / "events.db")
    aufgerufen: list[str] = []

    def build(selection: ModelSelection) -> ApiReasoner:
        aufgerufen.append(selection.provider)
        return ollama_build("unerreichbar")(selection)

    kette = FallbackReasoner(router, (OLLAMA_HOP,), build, log)
    antwort = kette.reason("x")

    assert aufgerufen == []
    assert antwort.startswith("(Reasoner error: HTTP 400")
    assert events(log, FALLBACK_EVENT) == []


def test_without_a_chain_the_error_text_is_exactly_the_old_one(tmp_path: Path) -> None:
    """Der Kompatibilitaets-Vertrag: kein TALOS_MODEL_FALLBACKS, kein neues Verhalten."""
    for status, stueck in ((401, "API key rejected (HTTP 401)"),
                           (429, "rate limit or quota reached (HTTP 429)"),
                           (529, "provider overloaded (HTTP 529)"),
                           (400, "HTTP 400")):
        router, _http = failing_primary(tmp_path, status)
        kette = FallbackReasoner(router, (), ollama_build("x"),
                                 EventLog(tmp_path / f"e{status}.db"))
        antwort = kette.reason("x")
        assert stueck in antwort, status
        assert not antwort.startswith("(Fallback")


def test_a_total_chain_failure_returns_the_last_errors_text(tmp_path: Path) -> None:
    router, _http = failing_primary(tmp_path, 429)
    log = EventLog(tmp_path / "events.db")
    kette = FallbackReasoner(router, (OLLAMA_HOP,), ollama_build("x", status=503), log)

    antwort = kette.reason("x")

    assert antwort == "(Reasoner error: provider overloaded (HTTP 503). Retry shortly.)"
    assert [b["outcome"] for b in events(log, FALLBACK_EVENT)] == ["failed"]


def test_the_persisted_selection_is_never_touched(tmp_path: Path) -> None:
    """Die Kette gilt pro Lauf: `model.selected` bleibt unangetastet, der Router steht
    danach auf demselben Modell wie davor."""
    router, _http = failing_primary(tmp_path, 529)
    log = EventLog(tmp_path / "events.db")
    kette = FallbackReasoner(router, (OLLAMA_HOP,), ollama_build("ok."), log)
    vorher = router.current

    kette.reason("x")

    assert router.current == vorher == PRIMARY
    assert events(log, "model.selected") == []


def test_each_attempt_is_its_own_measured_run(tmp_path: Path) -> None:
    """Fehlschlag des Primaeren UND Erfolg des Hops zaehlen als eigene Laeufe.
    Ein Zaehler, der den Fehlversuch verschlueckt, laesst genau das verschwinden,
    wonach man sucht, wenn es klemmt. (Zwei Laeufe, nicht drei: der lazy Router
    probt beim Bauen nicht mehr — d4d7cda.)"""
    meter = UsageMeter()
    router, _http = failing_primary(tmp_path, 429, meter=meter)
    log = EventLog(tmp_path / "events.db")
    kette = FallbackReasoner(router, (OLLAMA_HOP,), ollama_build("ok.", meter=meter), log)

    kette.reason("x")

    stand = meter.snapshot()
    assert stand.runs == 2   # Primaer (fehlgeschlagen) + Hop (ok)
    assert stand.failed == 1
    assert stand.last is not None and stand.last.ok


def test_delegation_to_the_router_still_works(tmp_path: Path) -> None:
    router, _http = failing_primary(tmp_path, 529)
    kette = FallbackReasoner(router, (), ollama_build("x"),
                             EventLog(tmp_path / "events.db"))
    assert kette.current == PRIMARY
    assert kette.can_select() is True


# --- Schema und Config ---------------------------------------------------------------


def test_model_fallbacks_is_a_writable_setting() -> None:
    eintrag = schema.get("TALOS_MODEL_FALLBACKS")
    assert eintrag is not None
    assert eintrag.kind == schema.SETTING and eintrag.writable
    with pytest.raises(ValueError):
        eintrag.validate("ollama/qwen3:27b\nTALOS_ALLOWED_PRINCIPALS=telegram:6")


def test_the_new_provider_keys_are_secrets_and_redacted() -> None:
    for name in ("KIMI_API_KEY", "NVIDIA_API_KEY"):
        eintrag = schema.get(name)
        assert eintrag is not None and eintrag.kind == schema.SECRET, name
        ausgabe = io.StringIO()
        configcli.cmd_get(name, {name: "geheim-123"}, ausgabe.write)
        assert ausgabe.getvalue() == schema.REDACTED + "\n"


def test_the_new_base_url_variables_are_policy() -> None:
    """Wer die Adresse biegt, schickt Prompts an eine Maschine, die er nicht gewaehlt hat."""
    for name in ("TALOS_BASE_URL_OLLAMA", "TALOS_BASE_URL_KIMI", "TALOS_BASE_URL_NVIDIA_NIM"):
        eintrag = schema.get(name)
        assert eintrag is not None and eintrag.kind == schema.POLICY, name


def test_load_config_reads_the_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from talos import config as config_modul

    monkeypatch.setenv(
        "TALOS_MODEL_FALLBACKS",
        "ollama/qwen3:27b, nvidia-nim/nvidia/llama-3.3-nemotron-super-49b-v1.5",
    )
    cfg = config_modul.load_config(require_channel=False)
    assert parse_chain(cfg.model_fallbacks) == (OLLAMA_HOP, NVIDIA_HOP)


# --- Leere Antwort: Fehlfunktion, nicht Ablehnung ---------------------------------
def test_an_empty_response_is_fallbackable_because_it_is_a_malfunction() -> None:
    """Gemessen auf einer laufenden Installation am 12.09.2026: kimi-cli lieferte leere
    Completions mit Exit 75. Der Router wiederholte intern, danach war Schluss — und
    Distill-Laeufe endeten als `distill.failed`, obwohl zwei funktionierende Anbieter
    in der Kette standen.

    Eine leere Antwort ist eine FEHLFUNKTION: der Anbieter hat nicht verstanden und
    abgelehnt, er hat gar nicht geantwortet. Eine Ablehnung kaeme als Text zurueck und
    loeste hier gar nichts aus. Damit erfuellt sie das Kriterium dieser Liste — ein
    anderer Anbieter hilft plausibel.
    """
    from talos.api_reasoner import FALLBACKABLE_KINDS, KIND_EMPTY_RESPONSE

    assert KIND_EMPTY_RESPONSE in FALLBACKABLE_KINDS


def test_the_loosening_does_not_take_http_failed_with_it() -> None:
    """DIE Grenze daneben: die Lockerung darf HTTP_FAILED nicht mitnehmen.

    Bei einem 4xx hat das Modell die Anfrage verstanden und abgelehnt — der naechste
    Anbieter bekaeme dieselbe Anfrage und antwortete gleich, nur teurer. Genau dieser
    Fall unterscheidet eine begruendete Lockerung von einer bequemen.
    """
    from talos.api_reasoner import (
        FALLBACKABLE_KINDS, KIND_HTTP_FAILED,
    )

    assert KIND_HTTP_FAILED not in FALLBACKABLE_KINDS
    assert "unknown" not in FALLBACKABLE_KINDS


def test_both_gates_are_required_to_switch_providers() -> None:
    """Zwei Tore, nicht eines — und das hat den ersten Anlauf gekostet.

    `FallbackReasoner.reason` verlangt BEIDES: `err.fallback_allowed` UND
    `err.kind in FALLBACKABLE_KINDS`. Wer nur eines patcht, aendert nichts und sucht
    den Fehler danach an der falschen Stelle.
    """
    from talos.api_reasoner import FALLBACKABLE_KINDS
    import inspect

    quelle = inspect.getsource(FallbackReasoner.reason)
    assert "fallback_allowed" in quelle, "das erste Tor ist verschwunden"
    assert "FALLBACKABLE_KINDS" in quelle, "das zweite Tor ist verschwunden"

    # Und beide Tore muessen fuer diese Art offen sein, sonst wirkt der Fix nicht.
    from talos.provider_errors import _MESSAGES  # noqa: F401 — nur Existenzbeweis
    fehler = ReasonerFailure("x", kind="empty_response", fallback_allowed=True)
    assert fehler.fallback_allowed and fehler.kind in FALLBACKABLE_KINDS


def test_every_fallbackable_kind_has_a_reason_text() -> None:
    """Ein stiller Wechsel des Denkers ist derselbe Vertrauensbruch wie ein stiller
    Katalog-Rueckfall. Fuer jede fallbackbare Art muss ein Grund-Text existieren."""
    from talos.api_reasoner import FALLBACKABLE_KINDS
    from talos.fallback import _GRUND

    ohne_text = sorted(k for k in FALLBACKABLE_KINDS if k not in _GRUND)
    assert not ohne_text, f"ohne Grund-Text im Prefix: {ohne_text}"


def test_an_empty_response_really_hops_to_the_next_provider(tmp_path: Path) -> None:
    """Der Verhaltensbeweis. Die Faelle darueber pruefen die beiden Tore; dieser hier
    laesst den Primaer-Anbieter wirklich leer antworten und verlangt, dass die Kette
    greift und den Wechsel belegt."""
    http = QueueHttp(FakeResponse([], status_code=200, text=""))

    def primaer(selection: ModelSelection) -> ApiReasoner:
        reasoner = ApiReasoner(selection.provider, selection.model, OPENAI_ROUTE,
                               timeout_s=5, http=http)

        def leer(*_a, **_k):
            raise ReasonerFailure("primär: leer", kind="empty_response",
                                  provider=selection.provider, model=selection.model,
                                  fallback_allowed=True)

        reasoner.reason_strict = leer  # type: ignore[method-assign]
        return reasoner

    log = EventLog(tmp_path / "events.db")
    router = ModelRouter(registry(), PRIMARY, primaer, log)
    kette = FallbackReasoner(router, (OLLAMA_HOP,), ollama_build("Ollama antwortet."), log)

    antwort = kette.reason("x")

    assert "Ollama antwortet." in antwort, "die Kette ist nicht gesprungen"
    assert "leere Antwort" in antwort, "der Betreiber erfährt den Grund nicht"
    belege = events(log, FALLBACK_EVENT)
    assert belege, "der Wechsel ist nicht belegt"


def test_the_classifier_itself_marks_an_empty_response_as_switchable() -> None:
    """Das ERSTE Tor, an seiner Quelle geprüft.

    ⚠️ Diese Pruefung fehlte zunaechst, und die Gegenprobe zeigte es sofort: mit
    `fallback_allowed=False` in `provider_errors.py` blieb die ganze Datei gruen, weil
    die Faelle darueber das Flag selbst setzen und die klassifizierende Stelle damit
    uebersprangen. Ein Test, der den gepatchten Ort gar nicht beruehrt, beweist nichts
    ueber ihn.
    """
    from talos.provider_errors import cli_failure

    leer = cli_failure(
        "The API returned an empty response.", "", 75,
        provider="kimi-cli", model="k3",
    )
    assert leer.kind == "empty_response"
    assert leer.fallback_allowed is True, "der Klassifizierer sperrt den Wechsel"


def test_the_classifier_opens_the_gate_for_everything() -> None:
    """Die Grenze an derselben Quelle: was lokal oder fachlich scheitert, wechselt nicht."""
    from talos.provider_errors import cli_failure

    unbekannt = cli_failure("irgendein Absturz", "", 1, provider="kimi-cli", model="k3")
    assert unbekannt.kind == "unknown"
    assert unbekannt.fallback_allowed is False, "ein unklarer Fehler oeffnet die Kette"
