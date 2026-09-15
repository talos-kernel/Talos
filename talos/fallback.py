"""Laufzeit-Fallback-Kette: der naechste Anbieter, wenn dieser Lauf klassifiziert scheitert.

Der Anlass: ein Reasoner, der heute mit „HTTP 529 — provider overloaded" endet, laesst
den Betreiber im Regen stehen, obwohl ein zweiter, lange konfigurierter Anbieter (etwa
das lokale Ollama) die Frage beantwortet haette. Der bisherige Rueckfall —
`resolve_fallback` in `provider.py` — wirkt einmal beim START, wenn der Katalog die
gewaehlte Modell-ID nicht kennt. Er hilft nichts gegen einen Anbieter, der mitten im
Betrieb ausfaellt. Diese Kette schliesst genau diese Luecke.

Drei Entscheidungen tragen das:

⚠️ **Ausgeloest wird nur durch eine klassifizierte Ausnahme.** `ApiReasoner` wirft
`ReasonerFailure` mit einer Fehlerart (`kind`); die Kette schaltet nur weiter, wenn die
Art in `FALLBACKABLE_KINDS` liegt. Ein HTTP-4xx-Fachfehler (HTTP_FAILED) tut das nie:
das Modell hat die Anfrage verstanden und abgelehnt — der naechste Anbieter bekaeme
dieselbe Anfrage und loeste denselben Fehler aus, nur teurer. *Grenze, bewusst:*
`ClaudeCliReasoner` meldet Fehler als AntwortTEXT und `HermesCliReasoner` als nackte
`RuntimeError` ohne Klassifikation — beides ist von einer echten Antwort bzw. einer
Betriebsstoerung („Reasoner laeuft bereits") nicht sauber zu unterscheiden, ohne an
Texten zu raten. CLI-Reasoner loesen die Kette deshalb nicht aus; sie kann nur als
Sprungziel dienen. Wer den Fehler eines CLI-Laufs faengt, faengt am Text — und das
waere genau das Raten, fuer das `ReasonerFailure.kind` existiert.

⚠️ **Die Kette gilt pro Lauf und schreibt nichts.** Die persistierte Wahl
(`model.selected` im Event-Log) bleibt unangetastet — der naechste Lauf startet wieder
beim Primaer-Anbieter. Ein Fallback ist eine Antwort auf eine Stoerung, keine
Entscheidung ueber die Zukunft; wer dauerhaft wechseln will, sagt `/model`.

⚠️ **Jeder Hop ist belegt.** Jeder Versuch landet als `model.fallback.runtime`-Event im
Event-Log: wohin, mit welcher Fehlerart, mit welchem Ausgang. Ein stiller Wechsel des
Denkers hinter dem Ruecken des Betreibers ist derselbe Vertrauensbruch wie der stille
Katalog-Rueckfall, den `restore_selection` einmal beheben musste.

Fail-closed beim Bauen: ein Hop ohne hinterlegten Schluessel wird uebersprungen (mit
Event), nie laeuft der Lauf deshalb in einen Traceback. Ein Hop, der gar nicht gebaut
werden kann, kostet die Kette einen Eintrag, nicht den Zug.
"""
from __future__ import annotations

import threading
from typing import Callable

from .api_reasoner import FALLBACKABLE_KINDS, ReasonerFailure
from .eventlog import Event, EventLog, new_run_id
from .provider import ModelSelection, SwitchResult, _takes_sink
from .stream import OnText

__all__ = ["FALLBACK_EVENT", "FallbackReasoner", "parse_chain"]

FALLBACK_EVENT = "model.fallback.runtime"

# Der Hinweis im Antworttext ist Teil des GESPRAECHS mit dem Betreiber, nicht der
# Maschinen-Konsole — er folgt dessen Sprache. Die Texte nennen die Art, nie Details:
# die stehen gesaeubert in `ReasonerFailure.message`, und die Kette gibt sie nur im
# Totalversagen weiter.
_GRUND = {
    "key_rejected": "Schlüssel abgelehnt",
    "rate_limited": "Kontingent erschöpft",
    "overloaded": "überlastet",
    "network_failed": "Netzfehler",
    "timed_out": "Zeitüberschreitung",
    # Der Betreiber soll am Prefix sehen, WARUM gewechselt wurde. „leere Antwort"
    # sagt genau das, was passiert ist — und unterscheidet sich hoerbar von einer
    # Ablehnung, die als Text zurueckkaeme und gar keinen Hop ausloest.
    "empty_response": "leere Antwort",
}


def parse_chain(raw: str) -> tuple[ModelSelection, ...]:
    """Kommagetrennte `provider/model`-Specs in Prioritaetsreihenfolge.

    Unlesbare Stuecke fallen weg statt umzufallen: ein Tippfehler in der Umgebung darf
    den Start nicht kosten — der betroffene Hop fehlt dann einfach in der Kette.
    Leer heisst: keine Kette, alles wie bisher. Modell-IDs duerfen selbst `/` tragen
    (etwa `nvidia-nim/nvidia/llama-…`); getrennt wird am ERSTEN Schraegstrich.
    """
    hops: list[ModelSelection] = []
    for part in str(raw).split(","):
        provider, trenner, model = part.partition("/")
        provider, model = provider.strip(), model.strip()
        if trenner and provider and model:
            hops.append(ModelSelection(provider, model))
    return tuple(hops)


class FallbackReasoner:
    """Ein Reasoner um den aktiven Router herum: scheitert der Lauf klassifiziert,
    denkt der naechste Anbieter der Kette.

    Die gespeicherte Auswahl bleibt beim primaeren Router. Abbruch und Beschaeftigung
    gelten fuer den tatsaechlich laufenden Hop; ein Hintergrundlauf bekommt eine eigene
    Kette und eigene Abbruchfelder. Ein Fallback aendert keine gespeicherte Auswahl.
    """

    def __init__(
        self,
        primary: object,
        chain: tuple[ModelSelection, ...],
        build: Callable[[ModelSelection], object],
        log: EventLog,
    ) -> None:
        self._primary = primary
        self._chain = tuple(chain)
        self._build = build
        self._log = log
        self._control_lock = threading.RLock()
        self._running = False
        self._selecting = False
        self._active_hop = None
        self._cancelled = threading.Event()

    def __getattr__(self, name: str):
        return getattr(self._primary, name)

    def fork(self) -> "FallbackReasoner":
        """Background work keeps the configured routes, with its own cancellation."""
        return FallbackReasoner(self._primary.fork(), self._chain, self._build, self._log)

    def can_select(self) -> bool:
        with self._control_lock:
            return not (self._running or self._selecting) and self._primary.can_select()

    def select(self, provider: str, model: str, *, principal):
        with self._control_lock:
            if self._running or self._selecting:
                return SwitchResult(False, self.current, "reasoner busy")
            self._selecting = True
        try:
            return self._primary.select(provider, model, principal=principal)
        finally:
            with self._control_lock:
                self._selecting = False

    def cancel(self) -> bool:
        with self._control_lock:
            running = self._running
            if running:
                self._cancelled.set()
            active = self._active_hop or self._primary
        cancel = getattr(active, "cancel", None)
        stopped = bool(cancel()) if callable(cancel) else False
        return stopped or running

    def _invoke(self, reasoner, prompt, on_text):
        from .reasoner import CANCELLED_TEXT
        with self._control_lock:
            if self._cancelled.is_set():
                return CANCELLED_TEXT
            self._active_hop = reasoner
        try:
            return _call(reasoner, prompt, on_text)
        finally:
            with self._control_lock:
                self._active_hop = None

    def reason(self, prompt: str, on_text: OnText | None = None) -> str:
        """Legacy text interface; the conductor uses the strict interface below."""
        try:
            return self.reason_strict(prompt, on_text)
        except ReasonerFailure as error:
            if not error.fallback_allowed:
                raise
            return error.message

    def reason_strict(self, prompt: str, on_text: OnText | None = None) -> str:
        """Do not turn an exhausted provider chain into a completed chat turn."""
        from .reasoner import CANCELLED_TEXT
        with self._control_lock:
            if self._running or self._selecting:
                raise RuntimeError("Reasoner laeuft bereits")
            self._running = True
            self._cancelled.clear()
        try:
            result = self._reason_chain(prompt, on_text)
            return CANCELLED_TEXT if self._cancelled.is_set() else result
        except Exception:
            if self._cancelled.is_set():
                return CANCELLED_TEXT
            raise
        finally:
            with self._control_lock:
                self._running = False

    def _reason_chain(self, prompt: str, on_text: OnText | None) -> str:
        from .reasoner import CANCELLED_TEXT
        try:
            return self._invoke(self._primary, prompt, on_text)
        except ReasonerFailure as err:
            if not err.fallback_allowed:
                raise  # Classifying CLI failures must not enable provider switching.
            if not self._chain or err.kind not in FALLBACKABLE_KINDS:
                # Genau der bisherige Text — dieselbe Zeile, die der Reasoner ohne Kette
                # selbst ausgeliefert haette. e2e/redteam haengen an diesem Wortlaut.
                raise
            # `err` wird am Ende des except-Blocks geloescht (Python-Semantik) — die
            # Referenz braucht einen eigenen Namen, sonst ist die Kette unten leer.
            failure = err
        run_id = new_run_id()
        ausloeser = self._spec(self._primary)
        quelle = "Primär-Provider"
        fehler: BaseException = failure
        for hop in self._chain:
            if self._cancelled.is_set():
                return CANCELLED_TEXT
            ziel = f"{hop.provider}/{hop.model}"
            try:
                hop_reasoner = self._build(hop)
            except Exception as error:
                # Fail-closed pro Hop: fehlender Schluessel, fehlende CLI — der Hop wird
                # uebersprungen und belegt, der Zug stuerzt deshalb nie ab.
                self._record(run_id, ausloeser, ziel, _kind_of(fehler), "skipped",
                             str(error)[:200])
                continue
            try:
                antwort = self._invoke(hop_reasoner, prompt, on_text)
            except ReasonerFailure as hop_fehler:
                self._record(run_id, ausloeser, ziel, hop_fehler.kind, "failed",
                             hop_fehler.note)
                if not hop_fehler.fallback_allowed or hop_fehler.kind not in FALLBACKABLE_KINDS:
                    # Ein Fachfehler mitten in der Kette beendet sie: weiterschalten
                    # hiesse, dieselbe abgelehnte Anfrage an den naechsten zu stellen.
                    raise
                ausloeser, quelle, fehler = ziel, ziel, hop_fehler
                continue
            except Exception as error:
                # Ein Hop, der wirft statt klassifiziert (etwa eine CLI): zaehlt als
                # gescheiterter Hop. Bleibt er der letzte Fehler, fliegt er wie bisher —
                # ein erfundener Meldungstext waere schlimmer als die Ausnahme.
                self._record(run_id, ausloeser, ziel, _kind_of(fehler), "failed",
                             str(error)[:200])
                ausloeser, quelle, fehler = ziel, ziel, error
                continue
            if self._cancelled.is_set() or antwort == CANCELLED_TEXT:
                self._record(run_id, ausloeser, ziel, _kind_of(fehler), "cancelled", "")
                return CANCELLED_TEXT
            self._record(run_id, ausloeser, ziel, _kind_of(fehler), "ok", "")
            grund = _GRUND.get(_kind_of(fehler), _kind_of(fehler))
            return f"(Fallback: {ziel} — Grund: {quelle} {grund})\n{antwort}"
        # Totales Kettenversagen: das bisherige Textverhalten des LETZTEN Fehlers.
        raise fehler

    @staticmethod
    def _spec(reasoner: object) -> str:
        """`provider/model` des aktiven Denkers — leer, wenn das Objekt es nicht weiss."""
        current = getattr(reasoner, "current", None)
        if isinstance(current, ModelSelection):
            return f"{current.provider}/{current.model}"
        return ""

    def _record(
        self,
        run_id: str,
        von: str,
        nach: str,
        kind: str,
        ausgang: str,
        detail: str,
    ) -> None:
        """Der Beleg darf selbst nie der Grund sein, warum der Zug scheitert."""
        try:
            self._log.append(Event(run_id, "provider", FALLBACK_EVENT, {
                "from": von,
                "to": nach,
                "kind": kind,
                "outcome": ausgang,
                "detail": detail,
            }))
        except Exception:
            pass


def _call(reasoner: object, prompt: str, on_text: OnText | None) -> str:
    """Ein Hop-Lauf. `reason_strict` wo vorhanden — nur so traegt der Fehler seine Art."""
    method = getattr(reasoner, "reason_strict", None) or getattr(reasoner, "reason")  # type: ignore[attr-defined]
    if on_text is not None and _takes_sink(method):
        answer = str(method(prompt, on_text=on_text))
    else:
        answer = str(method(prompt))
    # Compatibility with a legacy text-only adapter. Validate before decorating.
    if answer.strip() in {"", "(Empty answer.)", "(leere Antwort)"}:
        raise ReasonerFailure("The provider returned no usable answer.", kind="empty_response")
    return answer


def _kind_of(fehler: BaseException) -> str:
    return fehler.kind if isinstance(fehler, ReasonerFailure) else "error"
