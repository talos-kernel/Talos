"""Der Takt — und die drei Dinge, die ihn von einem gewoehnlichen Zeitplan trennen.

Nicht der Termin ist hier der Pruefgegenstand, sondern was ein Wachzustand DARF und was
nicht: im Leerlauf schlagen statt sich anzustellen, schweigen duerfen, pausiert werden
ohne verloren zu gehen. Jeder dieser drei Punkte hat eine Gegenprobe, weil er sonst zu
viel koennte — die Stille zum Beispiel darf ein Waechter ohne `heartbeat` nicht haben.
"""
from __future__ import annotations

import sqlite3
import threading
from types import SimpleNamespace

import pytest

from talos import heartbeat
from talos.channel import ChannelRegistry, Trust
from talos.continuity import Continuity
from talos.schedule import ScheduleStore, UnattendedCeiling
from talos.scheduler import dispatch_due
from test_conductor import _build, OWNER, CHAT_OWNER, ScriptedReasoner

OTHER_CHAT = "telegram:999"


def rig(tmp_path, *, reply=heartbeat.NO_REPLY, beat=True, continuity=True):
    """Ein Takt, ein Conductor, ein Modell, das genau `reply` sagt — ohne Werkzeug."""
    model = ScriptedReasoner(reply, reply)
    registry = ChannelRegistry((SimpleNamespace(name="telegram", trust=Trust.FULL),))
    conductor, sent = _build(tmp_path, model, trust_of=registry.trust_of)
    store = ScheduleStore(tmp_path / "schedules.db")
    task = store.add(conversation=CHAT_OWNER, principal=str(OWNER),
                     prompt="Look at the disk", interval_s=300,
                     continuity=continuity, heartbeat=beat, now=0)
    deps = dict(schedules=store, registry=registry, allowed_principals=frozenset({OWNER}),
                unattended=UnattendedCeiling(), conductor=conductor, log=conductor.log,
                continuity=Continuity(schedules=store, log=conductor.log,
                                      execute=conductor.executor.run))
    return deps, task, sent, model


def types_in(log):
    return [row["type"] for row in log.recent(100)]


# --- Stille ist eine Antwort -------------------------------------------------------
def test_a_silent_beat_stays_in_the_log_and_out_of_the_chat(tmp_path) -> None:
    """Der Lauf hat stattgefunden und ist belegbar — nur die Nachricht bleibt aus.
    Ohne das waere ein Fuenf-Minuten-Takt ein Fuenf-Minuten-Piepen."""
    deps, task, sent, model = rig(tmp_path)

    dispatch_due(**deps, now=task.next_run)

    assert model.calls == 1 and sent == []
    ereignisse = types_in(deps["log"])
    assert "schedule.fired" in ereignisse and "heartbeat.silent" in ereignisse


def test_the_marker_does_not_reach_the_next_prompt(tmp_path) -> None:
    """Sonst stuende er im naechsten Prompt als Vorlauf und wuerde zur Gewohnheit."""
    deps, task, _sent, _model = rig(tmp_path)

    dispatch_due(**deps, now=task.next_run)

    danach = deps["schedules"].heartbeat_for(CHAT_OWNER)
    assert heartbeat.NO_REPLY not in danach.last_result


def test_only_a_heartbeat_may_go_silent(tmp_path) -> None:
    """Ein gewoehnlicher Zeitplan darf mit demselben Text nicht stumm schalten."""
    deps, task, sent, model = rig(tmp_path, beat=False)

    dispatch_due(**deps, now=task.next_run)

    assert model.calls == 1 and len(sent) == 1
    assert heartbeat.NO_REPLY in sent[0][1]
    assert "heartbeat.silent" not in types_in(deps["log"])


def test_a_quoted_marker_never_swallows_a_report(tmp_path) -> None:
    """Nur eine alleinstehende Markerzeile darf eine Meldung unterdruecken."""
    deps, task, sent, _model = rig(
        tmp_path, reply=f"Disk at 91 %. The runbook says to answer {heartbeat.NO_REPLY} when idle.")

    dispatch_due(**deps, now=task.next_run)

    assert len(sent) == 1 and "91 %" in sent[0][1]


def test_the_frame_carries_the_task_and_the_permission_to_stay_quiet(tmp_path) -> None:
    deps, task, _sent, _model = rig(tmp_path)

    bereit = deps["continuity"].prepare(task, OWNER, run_id="r1")

    assert "Look at the disk" in bereit.text
    assert heartbeat.NO_REPLY in bereit.text
    assert "Do not invent adjacent work" in bereit.text


def test_an_ordinary_schedule_keeps_its_bare_prompt(tmp_path) -> None:
    deps, task, _sent, _model = rig(tmp_path, beat=False, continuity=False)

    bereit = deps["continuity"].prepare(task, OWNER, run_id="r1")

    assert bereit.text == "Look at the disk" and bereit.before_reply is None


# --- Nur im Leerlauf ---------------------------------------------------------------
def test_a_beat_falling_into_a_busy_moment_is_dropped_and_stays_due(tmp_path) -> None:
    """Ein Takt stellt sich nicht an; sein Termin bleibt beim belegten Schloss faellig."""
    deps, task, sent, model = rig(tmp_path)
    lock = deps["conductor"].execution_lock
    gehalten, freigeben = threading.Event(), threading.Event()

    def besetzt() -> None:
        with lock:
            gehalten.set()
            freigeben.wait(timeout=5)

    halter = threading.Thread(target=besetzt, daemon=True)
    halter.start()
    assert gehalten.wait(timeout=5)
    try:
        dispatch_due(**deps, now=task.next_run)
    finally:
        freigeben.set()
        halter.join(timeout=5)

    assert model.calls == 0 and sent == []
    assert "heartbeat.busy" in types_in(deps["log"])
    assert len(deps["schedules"].due(now=task.next_run)) == 1

    dispatch_due(**deps, now=task.next_run)
    assert model.calls == 1


# --- Pausierbar --------------------------------------------------------------------
def test_pause_keeps_the_task_and_resume_makes_it_due_again(tmp_path) -> None:
    deps, task, _sent, _model = rig(tmp_path)
    store = deps["schedules"]

    assert store.set_paused(task.id, conversation=CHAT_OWNER, paused=True)
    assert store.due(now=task.next_run) == ()
    assert len(store.list_for(CHAT_OWNER)) == 1
    assert store.heartbeat_for(CHAT_OWNER).paused is True

    assert store.set_paused(task.id, conversation=CHAT_OWNER, paused=False)
    assert len(store.due(now=task.next_run)) == 1


def test_a_second_chat_cannot_silence_this_heartbeat(tmp_path) -> None:
    deps, task, _sent, _model = rig(tmp_path)
    store = deps["schedules"]

    assert store.set_paused(task.id, conversation=OTHER_CHAT, paused=True) is False
    assert len(store.due(now=task.next_run)) == 1


def test_a_paused_beat_is_skipped_by_the_dispatcher(tmp_path) -> None:
    deps, task, sent, model = rig(tmp_path, reply="Disk at 91 %.")
    deps["schedules"].set_paused(task.id, conversation=CHAT_OWNER, paused=True)

    dispatch_due(**deps, now=task.next_run)

    assert model.calls == 0 and sent == []


# --- Das Kommando ------------------------------------------------------------------
def store_only(tmp_path) -> ScheduleStore:
    return ScheduleStore(tmp_path / "only.db")


def run(store, rest, *, conversation=CHAT_OWNER):
    return heartbeat.command(rest, schedules=store, principal=OWNER,
                             conversation=conversation)


def test_the_command_creates_one_beat_and_reports_its_rules(tmp_path) -> None:
    store = store_only(tmp_path)

    antwort = run(store, "5 look at the disk")

    takt = store.heartbeat_for(CHAT_OWNER)
    assert takt is not None and takt.interval_s == 300
    assert takt.heartbeat and takt.continuity and not takt.paused
    assert "every 5 min" in antwort and "silent" in antwort


def test_a_second_command_replaces_the_beat_instead_of_stacking(tmp_path) -> None:
    store = store_only(tmp_path)
    run(store, "5 look at the disk")

    antwort = run(store, "10 look at the queue")

    takte = [t for t in store.list_for(CHAT_OWNER) if t.heartbeat]
    assert len(takte) == 1 and takte[0].interval_s == 600
    assert "replaces" in antwort


def test_status_pause_resume_clear_round_trip(tmp_path) -> None:
    store = store_only(tmp_path)
    assert "No heartbeat" in run(store, "status")

    run(store, "5 look at the disk")
    assert "next in" in run(store, "status")

    assert "paused" in run(store, "pause")
    assert "paused" in run(store, "status")
    assert "already paused" in run(store, "pause")

    assert "resumed" in run(store, "resume")
    assert store.heartbeat_for(CHAT_OWNER).paused is False

    assert "cleared" in run(store, "clear")
    assert store.heartbeat_for(CHAT_OWNER) is None
    assert "nothing to clear" in run(store, "clear")


@pytest.mark.parametrize("rest", ["soon look at it", "5", "banana"])
def test_nonsense_gets_the_usage_line_not_a_beat(tmp_path, rest):
    store = store_only(tmp_path)

    antwort = run(store, rest)

    assert antwort == heartbeat.USAGE and store.heartbeat_for(CHAT_OWNER) is None


def test_a_rejected_interval_leaves_the_running_beat_alone(tmp_path) -> None:
    store = store_only(tmp_path)
    run(store, "5 look at the disk")

    antwort = run(store, "0 look at the disk")

    assert "No heartbeat:" in antwort
    takte = [t for t in store.list_for(CHAT_OWNER) if t.heartbeat]
    assert len(takte) == 1 and takte[0].interval_s == 300


def test_each_chat_sees_only_its_own_beat(tmp_path) -> None:
    store = store_only(tmp_path)
    run(store, "5 look at the disk")

    assert "No heartbeat" in run(store, "status", conversation=OTHER_CHAT)


# --- Bestand -----------------------------------------------------------------------
def test_a_database_written_before_the_beat_still_opens_and_dispatches(tmp_path) -> None:
    pfad = tmp_path / "old.db"
    conn = sqlite3.connect(pfad)
    conn.execute(
        "CREATE TABLE schedules (id TEXT PRIMARY KEY, conversation TEXT NOT NULL,"
        " principal TEXT NOT NULL, prompt TEXT NOT NULL, interval_s INTEGER NOT NULL,"
        " next_run REAL NOT NULL, created REAL NOT NULL, last_run REAL)")
    conn.execute("INSERT INTO schedules VALUES ('old1', ?, ?, 'watch the disk', 60, 0, 0, NULL)",
                 (CHAT_OWNER, str(OWNER)))
    conn.commit()
    conn.close()

    store = ScheduleStore(pfad)

    assert store.available
    alt = store.list_for(CHAT_OWNER)[0]
    assert alt.paused is False and alt.heartbeat is False
    assert len(store.due(now=100)) == 1
    assert store.heartbeat_for(CHAT_OWNER) is None


def test_the_listing_names_the_beat(tmp_path) -> None:
    store = store_only(tmp_path)
    run(store, "5 look at the disk")
    store.set_paused(store.heartbeat_for(CHAT_OWNER).id, conversation=CHAT_OWNER, paused=True)

    zeile = store.heartbeat_for(CHAT_OWNER).describe()

    assert "heartbeat" in zeile and "paused" in zeile
