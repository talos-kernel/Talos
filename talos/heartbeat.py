"""Heartbeat — ein Takt, an dem Talos von selbst nachsieht, ohne sich Arbeit auszudenken.

Ein Zeitplan (`schedule.py`) ist ein Auftrag mit Termin: er feuert, laeuft, meldet.
Ein Heartbeat ist ein Wachzustand: dieselbe Frage in festem Takt, und in den meisten
Faellen lautet die ehrliche Antwort „nichts Neues". Genau dann soll er schweigen.

Drei Unterschiede zum gewoehnlichen Zeitplan — sie sind der ganze Grund fuer dieses Modul:

  1. **Nur im Leerlauf.** Wird ein Takt faellig, waehrend ein Auftrag laeuft, faellt er
     AUS statt sich anzustellen. Ein Herzschlag, der sich in eine Warteschlange legt, ist
     keiner mehr: er kaeme an, wenn der Zustand, den er messen sollte, laengst ein anderer
     ist. Der Termin rueckt dabei NICHT weiter: der Takt bleibt faellig und schlaegt beim
     naechsten freien Tick. Einen Rueckstau kann das nicht geben — offen ist immer nur
     der eine Takt, denn `next_run` rueckt erst mit der Ausfuehrung weiter.
  2. **Stille ist eine Antwort.** Der Lauf darf mit einer einzigen Zeile enden —
     `[[NO_REPLY]]` — und dann geht keine Nachricht raus. Der Lauf selbst bleibt im
     Event-Log belegbar; nur der Chat bleibt ruhig. Ohne das waere ein Fuenf-Minuten-Takt
     ein Fuenf-Minuten-Piepen, und das erste, was ein Mensch damit tut, ist es abzuschalten.
  3. **Pausierbar.** `/heartbeat pause` haelt den Takt an, ohne den Auftrag zu verlieren.
     Die Alternative waere loeschen und spaeter neu tippen — der sicherste Weg, einen
     Waechter nach der Pause nie wieder anzuschalten.

Was hier NICHT entsteht: ein zweiter Ausfuehrungsweg. Ein Heartbeat ist ein Eintrag im
ScheduleStore wie jeder andere, laeuft durch denselben Conductor, denselben Kernel und
unter derselben unbeaufsichtigten Decke — `NEEDS_HUMAN` wird auch hier `DENY`. Und das
Kommando gehoert dem Betreiber: es gibt bewusst kein Werkzeug dafuer, mit dem sich ein
Modell selbst einen Takt anlegen koennte (dieselbe Begruendung wie bei `/every`).

⚠️ Der Stille-Marker wird NUR bei Heartbeat-Eintraegen beachtet (`Task.heartbeat`) und
nur, wenn er eine eigene, alleinstehende Zeile der Modellantwort ist. Beides ist Absicht:
ein gewoehnlicher Waechter kann auf diesem Weg nicht stumm geschaltet werden, und ein
Satz aus einer Werkzeugausgabe oder einer Webseite auch nicht — er muesste das Modell
dazu bringen, eine Zeile zu schreiben, die aus NICHTS SONST besteht.
"""
from __future__ import annotations

import time

__all__ = [
    "FRAME",
    "NO_REPLY",
    "USAGE",
    "command",
    "frame",
    "strip_marker",
    "wants_silence",
]

# Bewusst keine Vokabel, die in Prosa vorkommt: eckige Doppelklammern schreibt niemand
# versehentlich, und ein Modell, das sie tippt, hat sie aus diesem Rahmen.
NO_REPLY = "[[NO_REPLY]]"

# Der Rahmen liegt zur LAUFZEIT um den Auftrag, nicht im gespeicherten Prompt: sonst
# frisst er dessen Zeichenlimit und `/schedules` zeigt Rahmen statt Auftrag.
FRAME = (
    "[Heartbeat — a recurring look at something that already exists, not a new assignment]\n"
    "Do exactly what the standing instruction below asks, with the tools you already have. "
    "Do not invent adjacent work, do not start anything, and do not repeat what the previous "
    "run already reported.\n"
    "If nothing changed and nothing needs attention, answer with the single line {marker} "
    "and nothing else — silence is the expected outcome here, not a failure.\n\n"
    "Standing instruction: {prompt}"
)

USAGE = (
    "Usage: /heartbeat <minutes> <what to look at>\n"
    "   or: /heartbeat status | pause | resume | clear"
)


def frame(prompt: str) -> str:
    """Der Auftrag im Heartbeat-Rahmen — inklusive der Erlaubnis zu schweigen."""
    return FRAME.format(marker=NO_REPLY, prompt=str(prompt))


def wants_silence(reply: str) -> bool:
    """True = dieser Lauf hat nichts zu sagen.

    Nur eine ALLEINSTEHENDE Markerzeile zaehlt. Ein Marker mitten in einem Satz ist kein
    Schweigewunsch, sondern ein Zitat — und ein Zitat darf keine Meldung verschlucken.
    """
    return any(zeile.strip() == NO_REPLY for zeile in str(reply).splitlines())


def strip_marker(text: str) -> str:
    """Der Marker gehoert nicht ins Gedaechtnis des naechsten Laufs.

    Sonst stuende er im naechsten Prompt als „so hat es der Vorlauf gemacht" — und aus
    einer einmaligen Stille wuerde eine Gewohnheit.
    """
    return "\n".join(
        zeile for zeile in str(text).splitlines() if zeile.strip() != NO_REPLY
    ).strip()


def _dauer(sekunden: int) -> str:
    minuten = max(0, int(sekunden)) // 60
    if minuten < 60:
        return f"{minuten} min"
    return f"{minuten // 60} h {minuten % 60} min"


def _status(task, *, now: float | None = None) -> str:
    if task is None:
        return "No heartbeat in this chat.\n" + USAGE
    moment = time.time() if now is None else float(now)
    if task.paused:
        wann = "paused"
    else:
        rest = int(task.next_run - moment)
        wann = "due now" if rest <= 0 else f"next in {_dauer(rest)}"
    zeilen = [
        f"Heartbeat {task.id}: every {task.interval_s // 60} min — {wann}",
        f"Looking at: {task.prompt}",
    ]
    if task.last_run:
        zeilen.append(
            "Last beat: " + time.strftime("%a %d.%m %H:%M", time.localtime(task.last_run))
        )
    zeilen.append("/heartbeat pause · /heartbeat resume · /heartbeat clear")
    return "\n".join(zeilen)


def command(rest: str, *, schedules, principal, conversation: str,
            now: float | None = None) -> str:
    """`/heartbeat …` — eine duenne Schicht auf dem ScheduleStore, kein eigener Speicher.

    EIN Heartbeat pro Konversation. Ein zweiter Takt neben dem ersten waere kein
    zweiter Wachzustand, sondern zwei Wecker, die sich gegenseitig den Leerlauf nehmen —
    wer wirklich zwei unabhaengige Auftraege will, nimmt `/every`.
    """
    if schedules is None:
        return "No schedule store wired."
    wort, _, argument = str(rest).strip().partition(" ")
    schluessel = wort.lower()
    bestehend = schedules.heartbeat_for(conversation)

    if not schluessel or schluessel == "status":
        return _status(bestehend, now=now)

    if schluessel in ("pause", "off"):
        if bestehend is None:
            return "No heartbeat in this chat.\n" + USAGE
        if bestehend.paused:
            return f"Heartbeat {bestehend.id} is already paused. /heartbeat resume starts it again."
        if not schedules.set_paused(bestehend.id, conversation=conversation, paused=True):
            return "Could not pause it — nothing changed."
        return f"Heartbeat {bestehend.id} paused. The task is kept; /heartbeat resume starts it again."

    if schluessel in ("resume", "on", "start"):
        if bestehend is None:
            return "No heartbeat in this chat.\n" + USAGE
        if not bestehend.paused:
            return _status(bestehend, now=now)
        if not schedules.set_paused(bestehend.id, conversation=conversation, paused=False):
            return "Could not resume it — nothing changed."
        # Bewusst ohne neuen Termin: der faellige Takt schlaegt beim naechsten Leerlauf
        # sofort einmal — „jetzt wieder nachsehen" ist genau, was ein Resume meint.
        return f"Heartbeat {bestehend.id} resumed — it beats at the next idle moment."

    if schluessel in ("clear", "stop", "remove", "delete"):
        if bestehend is None:
            return "No heartbeat in this chat — nothing to clear."
        if not schedules.remove(bestehend.id, conversation=conversation):
            return "Could not clear it — nothing changed."
        return f"Heartbeat {bestehend.id} cleared."

    auftrag = argument.strip()
    if not schluessel.isdigit() or not auftrag:
        return USAGE

    try:
        task = schedules.add(
            conversation=conversation,
            principal=str(principal),
            prompt=auftrag,
            interval_s=int(schluessel) * 60,
            # Gedaechtnis per Vorgabe: ein Waechter ohne Vorlauf-Wissen meldet jedes Mal
            # dasselbe „91 %" als waere es neu. Genau dafuer gibt es `continuity.py`.
            continuity=True,
            heartbeat=True,
            now=now,
        )
    except ValueError as fehler:
        return f"No heartbeat: {fehler}"
    if task is None:
        return "No heartbeat — the schedule store refused it."
    # Erst anlegen, dann den alten entfernen: scheitert das Anlegen an einer Grenze,
    # steht der bisherige Takt noch. Andersherum waere ein Limit-Fehler ein Datenverlust.
    if bestehend is not None:
        schedules.remove(bestehend.id, conversation=conversation)
    naechster = time.strftime("%a %d.%m %H:%M", time.localtime(task.next_run))
    ersetzt = f" (replaces {bestehend.id})" if bestehend is not None else ""
    return (
        f"Heartbeat {task.id}{ersetzt}: every {task.interval_s // 60} min — {task.prompt}\n"
        f"First beat: {naechster}.\n"
        "It skips any beat that falls while something is running, and stays silent when "
        "there is nothing to report. Unattended runs do less than you do: anything that "
        "would need your approval is reported, not performed.\n"
        "/heartbeat status · pause · resume · clear"
    )
