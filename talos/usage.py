"""Verbrauchszaehler fuer die Denk-Laeufe — gemessen, nicht geschaetzt.

`/usage` zeigt bei vergleichbaren Agenten Token und Kosten. Dieselbe Anzeige waere hier eine Erfindung
gewesen: Talos rief die claude-CLI im Klartext-Modus auf und bekam nur die Antwort
zurueck — kein Modellname, keine Token, keine Dauer. Ein `/usage`, das daraus Zahlen
macht, zeigt Zahlen ueber sich selbst, nicht ueber den Lauf.

Deshalb zuerst die Quelle: der Reasoner laeuft jetzt mit `--output-format json` und
liest ab, was die CLI ohnehin meldet (`usage`, `modelUsage`, `duration_ms`,
`num_turns`). Der Zaehler hier summiert nur noch.

Drei Entscheidungen:

**1. Nur Summen und der letzte Lauf.** Kein Verlauf. Ein Zaehler, der jeden Lauf
aufhebt, waechst unbegrenzt und speichert nebenbei, wann the operator was gefragt hat — das
Gedaechtnis hat aus demselben Grund eine Obergrenze.

**2. Kosten sind rechnerisch, nicht abgerechnet.** Die CLI laeuft ueber Abo/OAuth.
`total_cost_usd` ist der Listenpreis derselben Anfrage ueber die API. Wer das als
Rechnung liest, taeuscht sich um Groessenordnungen — `/usage` sagt es deshalb dazu.
Meldet der Reasoner KEINEN Preis (der API-Weg kennt nur Token), rechnet der Zaehler
mit den Preisen, die der Betreiber in `TALOS_MODEL_OVERRIDES` hinterlegt hat — und
merkt sich am Lauf, dass die Zahl daher stammt (`cost_source`). Ohne Preis bleibt es
bei 0: eine Null, die „unbekannt" heisst, ist ehrlicher als ein geratener Tarif.

**3. Fehlversuche zaehlen mit.** Timeout, Abbruch und Fehlstart sind Laeufe. Ein
Zaehler, der nur die geglueckten zeigt, laesst genau das verschwinden, wonach man
sucht, wenn etwas klemmt.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace, field
from typing import Callable

from . import modelinfo
from .catalog import ModelInfo

__all__ = ["Run", "Snapshot", "UsageMeter"]


@dataclass(frozen=True)
class Run:
    """Ein einzelner Denk-Lauf. `note` ist leer, wenn nichts auffiel.

    `cost_source` sagt, woher `cost_usd` stammt: leer = vom Reasoner gemeldet (oder
    kein Preis), `override` = nach Betreiber-Preisen gerechnet, `catalog` = nach
    Katalog-Preisen. Eine gerechnete Zahl darf nie wie eine gemeldete aussehen.
    """

    at: float
    ok: bool
    duration_s: float
    model: str = ""
    models: tuple[str, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    session_id: str = ""
    note: str = ""
    cost_source: str = ""


@dataclass(frozen=True)
class ModelTotals:
    """Was EIN Modell gekostet hat. Die Zuordnung, die `/usage` bisher nicht zeigte.

    Ohne sie sagt der Zaehler nur, dass viel verbraucht wurde — nicht von wem. Genau das
    fehlte, als das Abo eines Anbieters mitten in der Arbeit auslief: die Summe stieg,
    aber welcher Anbieter sie trieb, stand nirgends.
    """

    runs: int = 0
    failed: int = 0
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class Snapshot:
    runs: int = 0
    failed: int = 0
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    # Der Teil von `cost_usd`, der nach Betreiber-Preisen gerechnet wurde — `/usage`
    # nennt ihn getrennt, sonst laese jemand einen selbst eingetragenen Tarif als Messung.
    cost_override_usd: float = 0.0
    last: Run | None = None
    # Modellname -> Summen. Leer heisst: noch kein Lauf, nicht „kein Modell".
    per_model: dict[str, ModelTotals] = field(default_factory=dict)

    @property
    def cache_total(self) -> int:
        return self.cache_read + self.cache_write


class UsageMeter:
    """Thread-sicher: der Worker misst, der Poll-Thread liest (`/usage`, `/debug`).

    `infos` liefert die Eckdaten eines Modells (Preise). Vorgabe ist die beim Laden
    installierte Tabelle (`modelinfo.lookup`) — so ist der Zaehler in `__main__`
    verdrahtet; Tests uebergeben ihre eigene Funktion.
    """

    def __init__(self, *, infos: Callable[[str], ModelInfo] | None = None,
                 on_record: Callable[[Run], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._total = Snapshot()
        self._infos = infos or modelinfo.lookup
        # Der Sink schreibt den Lauf dorthin, wo er einen Neustart ueberlebt (Event-Log).
        # Er darf NIE werfen: ein Zaehler, der den Zug mitnimmt, ist schlimmer als eine
        # fehlende Zahl.
        self._on_record = on_record

    def record(self, run: Run) -> None:
        run = self._priced(run)
        with self._lock:
            total = self._total
            self._total = replace(
                total,
                runs=total.runs + 1,
                failed=total.failed + (0 if run.ok else 1),
                seconds=total.seconds + max(0.0, run.duration_s),
                input_tokens=total.input_tokens + run.input_tokens,
                output_tokens=total.output_tokens + run.output_tokens,
                cache_read=total.cache_read + run.cache_read,
                cache_write=total.cache_write + run.cache_write,
                cost_usd=total.cost_usd + run.cost_usd,
                cost_override_usd=total.cost_override_usd
                + (run.cost_usd if run.cost_source == "override" else 0.0),
                last=run,
                per_model=_mit_modell(total.per_model, run),
            )
        if self._on_record is not None:
            try:
                self._on_record(run)
            except Exception:
                pass

    def _priced(self, run: Run) -> Run:
        """Ein gemeldeter Preis bleibt; nur ein fehlender wird gerechnet — und nur, wenn
        es Token UND einen belegten Preis gibt. Sonst bleibt es bei 0 = unbekannt."""
        if run.cost_usd > 0 or not (run.input_tokens or run.output_tokens) or not run.model:
            return run
        info = self._infos(run.model)
        if not info.has_prices:
            return run
        quelle = "override" if {"input_price", "output_price"} & info.overridden else "catalog"
        return replace(
            run,
            cost_usd=modelinfo.cost_usd(info, run.input_tokens, run.output_tokens),
            cost_source=quelle,
        )

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._total


def now() -> float:
    """Eigene Funktion, damit Tests die Uhr ersetzen koennen."""
    return time.time()


def _mit_modell(bisher: dict[str, ModelTotals], run: Run) -> dict[str, ModelTotals]:
    """Neue Zuordnungstabelle — nie die alte veraendern (der Snapshot ist unveraenderlich)."""
    name = run.model or "unbekannt"
    alt = bisher.get(name, ModelTotals())
    neu = dict(bisher)
    neu[name] = ModelTotals(
        runs=alt.runs + 1,
        failed=alt.failed + (0 if run.ok else 1),
        seconds=alt.seconds + max(0.0, run.duration_s),
        input_tokens=alt.input_tokens + run.input_tokens,
        output_tokens=alt.output_tokens + run.output_tokens,
        cost_usd=alt.cost_usd + run.cost_usd,
    )
    return neu


def event_payload(run: Run) -> dict[str, object]:
    """Was vom Lauf ins Event-Log darf: ZAHLEN und der Modellname. Sonst nichts.

    Ausdruecklich NICHT dabei: `note` und `session_id`. Beide tragen freien Text, und ein
    Verbrauchszaehler ist kein Ort fuer Gespraechsinhalte — das Log ist append-only, ein
    Inhalt darin bleibt fuer immer. Dieselbe Grenze wie bei `crashreport`: Struktur, nie Inhalt.
    """
    return {
        "model": run.model or "",
        "ok": bool(run.ok),
        "duration_s": round(max(0.0, float(run.duration_s)), 3),
        "input_tokens": int(run.input_tokens),
        "output_tokens": int(run.output_tokens),
        "cache_read": int(run.cache_read),
        "cache_write": int(run.cache_write),
        "cost_usd": round(float(run.cost_usd), 6),
        "cost_source": run.cost_source or "",
    }


def snapshot_from_events(payloads) -> Snapshot:
    """Den Verbrauch aus dem Event-Log zurueckrechnen — so ueberlebt er einen Neustart.

    Unbrauchbare Zeilen fallen weg statt umzufallen: das Log ist aelter als dieses Format.
    """
    gesamt = Snapshot()
    for roh in payloads:
        if not isinstance(roh, dict):
            continue
        try:
            run = Run(
                at=0.0,
                ok=bool(roh.get("ok", True)),
                duration_s=float(roh.get("duration_s") or 0.0),
                model=str(roh.get("model") or ""),
                input_tokens=int(roh.get("input_tokens") or 0),
                output_tokens=int(roh.get("output_tokens") or 0),
                cache_read=int(roh.get("cache_read") or 0),
                cache_write=int(roh.get("cache_write") or 0),
                cost_usd=float(roh.get("cost_usd") or 0.0),
                cost_source=str(roh.get("cost_source") or ""),
            )
        except (TypeError, ValueError):
            continue
        gesamt = replace(
            gesamt,
            runs=gesamt.runs + 1,
            failed=gesamt.failed + (0 if run.ok else 1),
            seconds=gesamt.seconds + max(0.0, run.duration_s),
            input_tokens=gesamt.input_tokens + run.input_tokens,
            output_tokens=gesamt.output_tokens + run.output_tokens,
            cache_read=gesamt.cache_read + run.cache_read,
            cache_write=gesamt.cache_write + run.cache_write,
            cost_usd=gesamt.cost_usd + run.cost_usd,
            cost_override_usd=gesamt.cost_override_usd
            + (run.cost_usd if run.cost_source == "override" else 0.0),
            last=run,
            per_model=_mit_modell(gesamt.per_model, run),
        )
    return gesamt
