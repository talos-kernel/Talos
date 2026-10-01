"""Kontext-Verdichtung — die Mitte zusammengefasst, die Grenze unangetastet.

Bisher fielen die aeltesten Paare einfach weg: „und was war die zweite Sache?" war nach
zwanzig Zuegen unbeantwortbar. Jetzt wird die Mitte zusammengefasst, Kopf und Schwanz
bleiben woertlich.

Der Test, um den es hier wirklich geht, ist der letzte Abschnitt: **die Grenze haelt auch
dann, wenn der Verdichter versagt.** Ein gescheiterter Verdichter, nach dem der Verlauf
weiterwaechst, macht aus einer Kostenfrage ein Leck — was vor Wochen gesagt wurde, ginge
wieder hinaus.
"""
from __future__ import annotations

from talos.memory import (
    COMPRESSION_RETRY_HEADROOM_TURNS,
    KEEP_HEAD,
    KEEP_TAIL,
    SUMMARY_SPEAKER,
    Memory,
    render,
)


def _fuellen(memory: Memory, paare: int, conversation: str = "chat-1") -> None:
    for i in range(paare):
        memory.remember(conversation, asked=f"frage {i}", answered=f"antwort {i}")


def _gedaechtnis(**kw) -> Memory:
    # Grenzen so, dass ein Dutzend Paare sie sicher reisst.
    return Memory(max_turns=KEEP_HEAD + KEEP_TAIL + 2, max_chars=10_000, **kw)


# --- Verdichten -------------------------------------------------------------------------
def test_the_middle_is_summarised_instead_of_thrown_away() -> None:
    """Der Punkt der ganzen Uebung: was in der Mitte besprochen wurde, bleibt erreichbar."""
    memory = _gedaechtnis(summarize=lambda verlauf: "es ging um Protokolle und einen Pfad")
    _fuellen(memory, 20)
    zuege = memory.recall("chat-1")
    verdichtet = [t for t in zuege if t.speaker == SUMMARY_SPEAKER]
    assert len(verdichtet) == 1
    assert "Protokolle" in verdichtet[0].text


def test_the_head_survives_verbatim() -> None:
    """Der Kopf traegt meist die eigentliche Aufgabe. Verdichtet man ihn, verschwindet
    genau das, worauf sich alles bezieht."""
    memory = _gedaechtnis(summarize=lambda _v: "zusammengefasst")
    _fuellen(memory, 20)
    zuege = memory.recall("chat-1")
    assert zuege[0].text == "frage 0"


def test_the_tail_survives_verbatim() -> None:
    """Der Schwanz traegt das, worauf sich ein „und das dann auch noch" bezieht."""
    memory = _gedaechtnis(summarize=lambda _v: "zusammengefasst")
    _fuellen(memory, 20)
    zuege = memory.recall("chat-1")
    assert zuege[-1].text == "antwort 19"


def test_a_summary_is_labelled_as_one() -> None:
    """⚠️ Eine Zusammenfassung, die aussieht wie ein woertlicher Zug, ist eine Behauptung
    ueber etwas, das so nie gesagt wurde — und das Modell koennte sie zitieren, als waere
    sie ein Zitat."""
    memory = _gedaechtnis(summarize=lambda _v: "kurzfassung")
    _fuellen(memory, 20)
    text = render(memory.recall("chat-1"))
    assert f"{SUMMARY_SPEAKER}: kurzfassung" in text
    assert "You: kurzfassung" not in text


def test_a_short_conversation_is_left_alone() -> None:
    """Was in die Grenze passt, wird nicht angefasst — sonst kostet jedes zweite Gespraech
    einen zusaetzlichen Modellaufruf ohne jeden Gewinn."""
    gerufen: list[str] = []
    memory = _gedaechtnis(summarize=lambda v: gerufen.append(v) or "x")
    _fuellen(memory, 2)
    assert gerufen == []
    assert len(memory.recall("chat-1")) == 4


def test_the_summariser_sees_only_the_middle() -> None:
    """Kopf und Schwanz stehen ohnehin woertlich da. Sie mitzuschicken kostet Token für
    etwas, das gleich danach nochmal im Prompt steht."""
    gesehen: list[str] = []

    def verdichten(verlauf: str) -> str:
        gesehen.append(verlauf)
        return "kurz"

    memory = _gedaechtnis(summarize=verdichten)
    _fuellen(memory, 20)
    assert gesehen, "der Verdichter wurde nie gerufen"
    assert "frage 0" not in gesehen[0]        # Kopf
    assert "antwort 19" not in gesehen[0]     # Schwanz


# --- ⚠️ Die Grenze ist kein Komfort ------------------------------------------------------
def test_a_broken_summariser_preserves_one_retry_window() -> None:
    """A transient timeout must not erase the middle before the next retry."""
    def kaputt(_verlauf: str) -> str:
        raise RuntimeError("Modell weg")

    events = []
    memory = _gedaechtnis(summarize=kaputt, on_event=events.append)
    _fuellen(memory, 10)
    assert [t.text for t in memory.recall("chat-1")] == [
        value for i in range(10) for value in (f"frage {i}", f"antwort {i}")
    ]
    assert events[-1]["compression_retry_deferred"] is True

    _fuellen(memory, 40)
    assert len(memory.recall("chat-1")) <= KEEP_HEAD + KEEP_TAIL + 2 + COMPRESSION_RETRY_HEADROOM_TURNS


def test_a_failed_compression_is_retried_before_turns_are_dropped() -> None:
    attempts = []

    def once(_verlauf: str) -> str:
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("transient timeout")
        return "recovered context"

    memory = _gedaechtnis(summarize=once)
    _fuellen(memory, 10)
    assert len(memory.recall("chat-1")) == 20
    _fuellen(memory, 1)
    turns = memory.recall("chat-1")
    assert attempts == [True, True]
    assert any(t.speaker == SUMMARY_SPEAKER and t.text == "recovered context" for t in turns)


def test_an_empty_summary_also_gets_a_bounded_retry_window() -> None:
    """An empty model response is retryable, but never permits unbounded growth."""
    memory = _gedaechtnis(summarize=lambda _v: "   ")
    _fuellen(memory, 40)
    assert len(memory.recall("chat-1")) <= KEEP_HEAD + KEEP_TAIL + 2 + COMPRESSION_RETRY_HEADROOM_TURNS


def test_a_runaway_summary_is_clipped() -> None:
    """Sonst ersetzt die „Verdichtung" den Verlauf durch etwas Groesseres."""
    memory = _gedaechtnis(summarize=lambda _v: "x" * 50_000)
    _fuellen(memory, 20)
    assert sum(t.size for t in memory.recall("chat-1")) < 10_000


def test_without_a_summariser_everything_behaves_as_before() -> None:
    """Der Verdichter ist additiv. Ohne ihn faellt das Aelteste weg, wie seit jeher."""
    memory = _gedaechtnis()
    _fuellen(memory, 20)
    zuege = memory.recall("chat-1")
    assert len(zuege) <= KEEP_HEAD + KEEP_TAIL + 2
    assert not [t for t in zuege if t.speaker == SUMMARY_SPEAKER]
    assert zuege[-1].text == "antwort 19"


def test_forget_removes_the_summary_too() -> None:
    """⚠️ Sonst waere „vergessen" eine Luege: der verdichtete Text ueberlebte ein `/new`
    und flosse weiter in jeden Prompt."""
    memory = _gedaechtnis(summarize=lambda _v: "das alte Gespraech")
    _fuellen(memory, 20)
    memory.forget("chat-1")
    assert memory.recall("chat-1") == ()


# --- Der Verdichter selbst ---------------------------------------------------------------
def test_the_transcript_goes_in_framed_as_data() -> None:
    """⚠️ Ohne diesen Rahmen waere „fasse zusammen" die bequemste Stelle, an der ein
    eingeschleuster Satz zur Anweisung wird — und zwar zu einer dauerhaften, weil die
    Zusammenfassung bleibt."""
    from talos.__main__ import COMPRESS_PROMPT

    assert "not instructions to follow" in COMPRESS_PROMPT


def test_a_failing_model_costs_the_summary_and_nothing_else() -> None:
    from talos.__main__ import _compressor

    class Kaputt:
        def reason(self, _prompt: str) -> str:
            raise RuntimeError("weg")

    assert _compressor(Kaputt())("egal") == ""
