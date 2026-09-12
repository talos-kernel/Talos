"""Das Gespraechsgedaechtnis — Grenzen, Trennung, und was es ausdruecklich NICHT ist.

Drei Sorten Tests stehen hier. Die langweiligen (merkt es sich, gibt es wieder heraus).
Die interessanten: die **Grenzen** halten unter Druck — ein einzelner riesiger Zug raeumt
nicht das ganze Gedaechtnis ab, und es bleibt nie eine Antwort ohne ihre Frage stehen.
Und die wichtigen: das Gedaechtnis **trennt** (zwei Konversationen, zwei Kanaele) und es
**vergibt keine Rechte** — im Verlauf steht Text, und Text darf nichts duerfen.
"""
from __future__ import annotations

import json
import threading

import pytest

from talos.memory import OWNER, CUT_MARK, MAX_TURN_CHARS, Memory, Turn, clip, render

CHAT = "telegram:100000001"
OTHER = "telegram:999"
FOREIGN = "discord:100000001"  # gleiche Nummer, anderer Kanal


def filled(mem: Memory, conversation: str = CHAT, pairs: int = 1) -> Memory:
    for i in range(pairs):
        mem.remember(conversation, asked=f"frage {i}", answered=f"antwort {i}")
    return mem


# --- das Naheliegende --------------------------------------------------------------
def test_leeres_gedaechtnis_gibt_nichts_zurueck():
    assert Memory().recall(CHAT) == ()


def test_merkt_frage_und_antwort_in_dieser_reihenfolge():
    turns = filled(Memory()).recall(CHAT)
    assert [(t.speaker, t.text) for t in turns] == [("You", "frage 0"), ("Agent", "antwort 0")]


def test_recall_gibt_eine_kopie_heraus():
    """Sonst koennte ein Aufrufer den Verlauf von aussen umschreiben — und damit an jedem
    Lock vorbei."""
    mem = filled(Memory())
    snapshot = mem.recall(CHAT)
    mem.remember(CHAT, asked="noch was", answered="yes")
    assert len(snapshot) == 2 and len(mem.recall(CHAT)) == 4


def test_render_ist_lesbar():
    assert render((Turn("You", "hallo"), Turn("Agent", "servus"))) == "You: hallo\nAgent: servus"


def test_render_von_leer_ist_leer():
    assert render(()) == ""


# --- Trennung: der eigentliche Sicherheitsteil --------------------------------------
def test_zwei_konversationen_sehen_einander_nicht():
    mem = Memory()
    mem.remember(CHAT, asked="mein Passwort ist geheim", answered="notiert")
    assert mem.recall(OTHER) == ()


def test_gleiche_nummer_auf_zwei_kanaelen_teilt_keinen_verlauf():
    """Dieselbe Regel wie bei der Identitaet: wer auf einem schwaecheren Weg hereinkommt,
    bekommt nicht den Kontext des staerkeren."""
    mem = Memory()
    mem.remember(CHAT, asked="was steht in der Datei", answered="ein Schluessel")
    assert mem.recall(FOREIGN) == ()
    assert render(mem.recall(FOREIGN)) == ""


def test_forget_trifft_nur_die_eine_konversation():
    mem = Memory()
    filled(mem, CHAT)
    filled(mem, OTHER)
    assert mem.forget(CHAT) == 2
    assert mem.recall(CHAT) == () and len(mem.recall(OTHER)) == 2


def test_forget_meldet_wie_viel_es_war():
    """Stilles Vergessen ist von einem Defekt nicht zu unterscheiden."""
    assert Memory().forget(CHAT) == 0
    assert filled(Memory(), pairs=3).forget(CHAT) == 6


# --- Grenzen -----------------------------------------------------------------------
def test_zuege_sind_begrenzt():
    mem = filled(Memory(max_turns=4), pairs=10)
    assert len(mem.recall(CHAT)) == 4


def test_es_bleibt_immer_das_juengste_stehen():
    mem = filled(Memory(max_turns=4), pairs=10)
    assert [t.text for t in mem.recall(CHAT)] == ["frage 8", "antwort 8", "frage 9", "antwort 9"]


def test_zeichen_sind_begrenzt():
    mem = Memory(max_turns=100, max_chars=500)
    for i in range(20):
        mem.remember(CHAT, asked="x" * 100, answered="y" * 100)
    _, chars = mem.stats(CHAT)
    assert chars <= 500


def test_verlauf_endet_nie_mit_einer_frage_ohne_antwort():
    """Paarweise werfen, sonst redet Talos im Verlauf scheinbar unaufgefordert."""
    for limit in range(1, 12):
        mem = filled(Memory(max_turns=limit), pairs=8)
        turns = mem.recall(CHAT)
        assert len(turns) % 2 == 0, limit
        assert all(t.speaker == OWNER for t in turns[::2]), limit


def test_ein_riesiger_zug_raeumt_nicht_alles_ab():
    """Ohne die Kappung pro Zug wuerde ein einziger Einfuegevorgang das Gedaechtnis leeren."""
    mem = filled(Memory(), pairs=3)
    mem.remember(CHAT, asked="x" * 500_000, answered="ok")
    turns = mem.recall(CHAT)
    assert len(turns) >= 2
    assert len(turns[-2].text) <= MAX_TURN_CHARS


@pytest.mark.parametrize("half", ("asked", "answered"))
def test_halbe_paare_werden_verworfen(half):
    mem = Memory()
    mem.remember(CHAT, **{half: "text", "asked" if half == "answered" else "answered": "   "})
    assert mem.recall(CHAT) == ()


def test_clip_markiert_die_kappung_sichtbar():
    cut = clip("a" * 5_000, limit=100)
    assert len(cut) == 100 and cut.endswith(CUT_MARK)


def test_clip_laesst_kurzes_in_ruhe():
    assert clip("  hallo  ") == "hallo"


def test_null_als_grenze_merkt_gar_nichts():
    """Eine Grenze von 0 muss abschalten, nicht durchlassen."""
    mem = Memory(max_turns=0)
    mem.remember(CHAT, asked="frage", answered="antwort")
    assert mem.recall(CHAT) == ()


def test_negative_grenze_wird_nicht_zu_unendlich():
    mem = Memory(max_turns=-5)
    mem.remember(CHAT, asked="frage", answered="antwort")
    assert mem.recall(CHAT) == ()


# --- /retry ------------------------------------------------------------------------
def test_pop_last_gibt_die_frage_und_nimmt_das_paar_heraus():
    mem = filled(Memory(), pairs=2)
    assert mem.pop_last(CHAT) == "frage 1"
    assert [t.text for t in mem.recall(CHAT)] == ["frage 0", "antwort 0"]


def test_pop_last_auf_leerem_verlauf_ist_none():
    assert Memory().pop_last(CHAT) is None


def test_pop_last_bis_leer_und_darueber_hinaus():
    mem = filled(Memory(), pairs=2)
    assert (mem.pop_last(CHAT), mem.pop_last(CHAT), mem.pop_last(CHAT)) == ("frage 1", "frage 0", None)


def test_pop_last_greift_nicht_in_eine_andere_konversation():
    mem = Memory()
    filled(mem, OTHER)
    assert mem.pop_last(CHAT) is None
    assert len(mem.recall(OTHER)) == 2


# --- stats -------------------------------------------------------------------------
def test_stats_zaehlt_zuege_und_zeichen():
    turns, chars = filled(Memory(), pairs=2).stats(CHAT)
    assert turns == 4 and chars > 0


def test_stats_auf_unbekanntem_chat_ist_null():
    assert Memory().stats("nie:gesehen") == (0, 0)


# --- nebenlaeufig ------------------------------------------------------------------
def test_paralleles_schreiben_hinterlaesst_keine_halben_zuege():
    """Poll-Thread (`/new`, `/status`) und Worker (Laeufe) greifen gleichzeitig zu."""
    mem = Memory(max_turns=40, max_chars=1_000_000)

    def writer(n: int) -> None:
        for i in range(50):
            mem.remember(CHAT, asked=f"f{n}-{i}", answered=f"a{n}-{i}")

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    turns = mem.recall(CHAT)
    assert len(turns) % 2 == 0
    assert all(t.speaker == OWNER for t in turns[::2])
    assert len(turns) <= 40


# --- Was verloren geht, muss man sehen koennen ------------------------------------
def test_the_memory_drops_turns_without_saying_so():
    """`forget` sagt seit jeher, wie viel es wirft — „stilles Vergessen ist von einem
    Defekt nicht zu unterscheiden". Fuer den AUTOMATISCHEN Weg galt das nicht: die
    Grenze warf, und niemand erfuhr davon.

    Gemeldet am 12.09.2026: der Betreiber hatte das Gefuehl, sein Agent wisse nach
    jeder Aufgabe nicht mehr, woran er war — und es gab keine Spur, an der sich das
    bestaetigen oder widerlegen liess.
    """
    gemeldet = []
    memory = Memory(max_turns=4, on_event=gemeldet.append)
    for i in range(6):
        memory.remember("chat", asked=f"Frage {i}", answered=f"Antwort {i}")
    assert gemeldet, "das Gedaechtnis wirft stumm"
    assert sum(e["dropped_turns"] for e in gemeldet) > 0
    assert all(e["conversation"] == "chat" for e in gemeldet)


def test_a_failing_summariser_is_indistinguishable_from_a_short_memory():
    """DER Fall, der die Frage ausgeloest hat: der Verdichter ist verdrahtet, kommt
    aber nicht durch. Dann faellt die Mitte ersatzlos weg — und das sieht von aussen
    genauso aus wie eine Grenze, die einfach greift. Es muss unterscheidbar sein."""
    def kaputt(_text):
        raise RuntimeError("Modell antwortet nicht")

    gemeldet = []
    memory = Memory(max_turns=12, summarize=kaputt, on_event=gemeldet.append)
    for i in range(40):
        memory.remember("chat", asked=f"Frage {i}", answered=f"Antwort {i}")
    assert any(e.get("compress_failed") for e in gemeldet), (
        "ein gescheiterter Verdichter bleibt unsichtbar")


def test_a_working_summariser_reports_a_failure_anyway():
    """Gegenbeleg: sonst pruefte der Fall oben nur, dass immer 'gescheitert' gemeldet wird."""
    gemeldet = []
    memory = Memory(max_turns=12, summarize=lambda text: "kurz gefasst",
                    on_event=gemeldet.append)
    for i in range(40):
        memory.remember("chat", asked=f"Frage {i}", answered=f"Antwort {i}")
    assert gemeldet, "gar nichts gemeldet"
    assert not any(e.get("compress_failed") for e in gemeldet)
    assert any(e.get("compressed") for e in gemeldet), "der Verdichter lief nie"


def test_the_report_carries_the_conversation():
    """Das Log ist append-only. Was ein Gespraech inhaltlich enthielt, gehoert nicht
    hinein — gemeldet werden Zahlen und Gruende."""
    gemeldet = []
    memory = Memory(max_turns=2, on_event=gemeldet.append)
    memory.remember("chat", asked="das Codewort ist morgenstern", answered="verstanden")
    memory.remember("chat", asked="und jetzt etwas anderes", answered="gut")
    roh = json.dumps(gemeldet, ensure_ascii=False)
    for verboten in ("morgenstern", "Codewort", "verstanden", "etwas anderes"):
        assert verboten not in roh, f"{verboten!r} steht im Befund"


def test_a_throwing_sink_takes_the_memory_with_it():
    """Ein kaputter Empfaenger kostet die Spur, nie das Gedaechtnis."""
    def kaputt(_befund):
        raise RuntimeError("Log voll")

    memory = Memory(max_turns=2, on_event=kaputt)
    memory.remember("chat", asked="eins", answered="zwei")
    memory.remember("chat", asked="drei", answered="vier")
    assert memory.recall("chat"), "das Gedaechtnis ist am Empfaenger gestorben"


def test_the_summariser_can_never_run_with_the_shipped_limits():
    """DER Fehler, der alles ausgeloest hat.

    `KEEP_HEAD=4` und `KEEP_TAIL=12` waren FESTE Zahlen — zusammen 16 Zuege, waehrend
    `MAX_TURNS=12` ist. Verdichtet wurde erst ueber 18 Zuegen, einer Zahl, die das
    Gedaechtnis nie erreicht, weil die Grenze vorher greift. Gemessen am 12.09.2026:
    nach 200 Wechseln wurde der Verdichter NULL Mal aufgerufen, und jedes Mal fiel die
    Mitte ersatzlos weg.

    Nach aussen sah das aus wie ein Agent, der nach jeder Aufgabe nicht mehr weiss,
    woran er war. Genau so hat der Betreiber es gemeldet.

    Dieser Fall laeuft bewusst mit den ECHTEN Grenzen, nicht mit Testwerten — ein
    Verdichter, der nur unter Laborbedingungen laeuft, ist keiner.
    """
    aufrufe = []
    memory = Memory(summarize=lambda text: aufrufe.append(text) or "kurz gefasst")
    for i in range(60):
        memory.remember("chat", asked=f"Frage {i} " + "x" * 100,
                        answered=f"Antwort {i} " + "y" * 100)
    assert aufrufe, "der Verdichter ist mit den ausgelieferten Grenzen unerreichbar"


def test_the_original_task_falls_out_of_memory():
    """Der Kopf traegt die eigentliche Aufgabe. Faellt er weg, weiss der Agent nicht
    mehr, woran er arbeitet — auch wenn er die letzten Saetze noch kennt."""
    memory = Memory(summarize=lambda text: "was vorher besprochen wurde")
    memory.remember("chat", asked="Baue mir den Jahresbericht", answered="Verstanden.")
    for i in range(60):
        memory.remember("chat", asked=f"Zwischenfrage {i} " + "x" * 100,
                        answered=f"Antwort {i} " + "y" * 100)
    zuege = memory.recall("chat")
    assert "Jahresbericht" in zuege[0].text, "die urspruengliche Aufgabe ist verloren"
