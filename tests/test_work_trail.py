"""Die Zwischenmeldungen bleiben stehen — sie sind der Beleg, nicht das Geruest.

Gemeldet am 12.09.: der Betreiber wolle die Zwischenstaende behalten,
    solange der Agent arbeitet. Bis dahin raeumte der Conductor sie nach dem Ergebnis weg;
ein Zwischenstand, den der Betreiber noch gar nicht gelesen hatte, verschwand vor seinen
Augen. Jetzt ist Behalten die Vorgabe, Aufraeumen die Ausnahme.
"""
from dataclasses import dataclass, field

import pytest

from talos.agent_loop import AgentProgress, ProgressStage
from talos.telegram import TelegramActivity, _keep_work_trail


@dataclass
class _Client:
    now: list[float] = field(default_factory=lambda: [0.0])
    sent: list[tuple[int, str, dict]] = field(default_factory=list)
    edited: list[tuple[int, int, str, dict]] = field(default_factory=list)
    deleted: list[tuple[int, int]] = field(default_factory=list)
    actions: list[tuple[int, str]] = field(default_factory=list)
    _next: int = 100

    def send_message(self, chat_id: int, text: str, **kwargs) -> int:
        self._next += 1
        self.sent.append((chat_id, text, kwargs))
        return self._next

    def edit_message_text(self, chat_id: int, message_id: int, text: str, **kwargs) -> None:
        self.edited.append((chat_id, message_id, text, kwargs))

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.actions.append((chat_id, action))


def _activity(client: _Client) -> TelegramActivity:
    return TelegramActivity(client, 42, clock=lambda: client.now[0], heartbeat_s=0)


def _arbeitet(activity: TelegramActivity) -> None:
    """Ein echter Zwischenstand — sonst gibt es gar keine Anzeige zum Behalten."""
    activity.progress(AgentProgress(
        ProgressStage.TOOL, tool="run_shell", status="running",
        summary="uptime", step=1, max_steps=3,
    ))


# --- Der Schalter -------------------------------------------------------------------
def test_keeping_is_the_default(monkeypatch):
    monkeypatch.delenv("TALOS_TIDY_WORK_TRAIL", raising=False)
    assert _keep_work_trail() is True


def test_the_operator_can_still_ask_for_a_tidy_chat(monkeypatch):
    monkeypatch.setenv("TALOS_TIDY_WORK_TRAIL", "1")
    assert _keep_work_trail() is False
    monkeypatch.setenv("TALOS_TIDY_WORK_TRAIL", "0")
    assert _keep_work_trail() is True


# --- Das Verhalten, um das es geht --------------------------------------------------
def test_the_trail_is_wiped_after_the_answer(monkeypatch):
    """DER gemeldete Fehler: der Fortschritt verschwand, sobald das Ergebnis da war."""
    monkeypatch.delenv("TALOS_TIDY_WORK_TRAIL", raising=False)
    client = _Client()
    activity = _activity(client)
    _arbeitet(activity)
    activity.succeed()
    activity.cleanup()
    assert client.sent, "ohne Anzeige bewiese der Test nichts"
    assert client.deleted == [], "die Zwischenmeldungen wurden weggeraeumt"


def test_a_tidy_operator_still_gets_the_old_behaviour(monkeypatch):
    monkeypatch.setenv("TALOS_TIDY_WORK_TRAIL", "1")
    client = _Client()
    activity = _activity(client)
    _arbeitet(activity)
    activity.succeed()
    activity.cleanup()
    assert client.sent, "ohne Anzeige bewiese der Test nichts"
    assert client.deleted, "mit TALOS_TIDY_WORK_TRAIL=1 muss aufgeraeumt werden"


def test_cleanup_still_ends_the_run_when_the_trail_stays(monkeypatch):
    """Behalten heisst nicht: weiterlaufen. Der Heartbeat muss trotzdem stehen."""
    monkeypatch.delenv("TALOS_TIDY_WORK_TRAIL", raising=False)
    client = _Client()
    activity = _activity(client)
    _arbeitet(activity)
    activity.succeed()
    activity.cleanup()
    vorher = len(client.edited)
    client.now[0] += 600          # viel spaeter
    activity.cleanup()            # doppelter Aufruf darf nichts mehr tun
    assert len(client.edited) == vorher
    assert client.deleted == []


def test_cleanup_twice_never_throws(monkeypatch):
    monkeypatch.delenv("TALOS_TIDY_WORK_TRAIL", raising=False)
    client = _Client()
    activity = _activity(client)
    activity.cleanup()
    activity.cleanup()
    assert client.deleted == []


# --- Der Abschluss-Beleg: append-only --------------------------------------------------
# Gemessen am 15.09.2026: der Endstand eines Laufs ueberschrieb die Zwischenstaende per
# Edit — Telegram zeigt nur den Endzustand einer Nachricht, der gelesene Zwischenstand
# war damit faktisch geloescht. Die Anzeige darf weiter morphen (ihr Design), aber der
# Abschluss geht als eigene Nachricht raus, die nie editiert wird.


def test_the_completion_receipt_is_a_fresh_message_never_edited_again(monkeypatch):
    monkeypatch.delenv("TALOS_TIDY_WORK_TRAIL", raising=False)
    client = _Client()
    activity = _activity(client)
    _arbeitet(activity)
    vorher = len(client.sent)
    activity.succeed("✓ 5s · 1k tok · testmodell")
    assert len(client.sent) == vorher + 1, "kein eigener Abschluss-Beleg gesendet"
    beleg_id, beleg_text = client._next, client.sent[-1][1]
    assert "Turn finished" in beleg_text
    assert "1 tool call" in beleg_text
    assert "testmodell" in beleg_text           # die Quittung gehoert in den Beleg
    activity.cleanup()
    client.now[0] += 600
    activity.tick()
    assert client.deleted == [], "der Beleg wurde aufgeraeumt"
    assert all(mid != beleg_id for _c, mid, _t, _k in client.edited), "der Beleg wurde editiert"


def test_the_receipt_survives_even_when_its_send_is_the_only_evidence(monkeypatch):
    """Scheitert die letzte Editierung der Anzeige, traegt der Beleg den Endstand allein."""
    monkeypatch.delenv("TALOS_TIDY_WORK_TRAIL", raising=False)

    class KaputterEditClient(_Client):
        def edit_message_text(self, chat_id, message_id, text, **kwargs):
            raise OSError("message to edit not found")

    client = KaputterEditClient()
    activity = TelegramActivity(client, 42, clock=lambda: client.now[0], heartbeat_s=0)
    _arbeitet(activity)
    activity.succeed()
    assert any("Turn finished" in text for _c, text, _k in client.sent)


def test_a_tidy_operator_loses_the_receipt_with_the_trail(monkeypatch):
    """Unter TALOS_TIDY_WORK_TRAIL=1 raeumt cleanup wie bisher alles weg — der Beleg
    gehoert zur Spur, nicht zum Ergebnis; das Ergebnis ist die Antwortnachricht."""
    monkeypatch.setenv("TALOS_TIDY_WORK_TRAIL", "1")
    client = _Client()
    activity = _activity(client)
    _arbeitet(activity)
    activity.succeed()
    beleg_id = client._next
    activity.cleanup()
    assert beleg_id in [mid for _c, mid in client.deleted]


def test_a_tool_free_answer_still_gets_no_receipt(monkeypatch):
    """Keine Anzeige, kein Beleg — eine reine Textantwort bleibt allein im Chat."""
    monkeypatch.delenv("TALOS_TIDY_WORK_TRAIL", raising=False)
    client = _Client()
    activity = _activity(client)
    activity.succeed()
    assert client.sent == []
