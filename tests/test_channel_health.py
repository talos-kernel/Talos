"""Polling recovery must be proven without hiding failures or replaying effects."""
import io
import json
import ast
from pathlib import Path

import pytest
import requests

from talos.channel import ChannelRegistry
from talos.eventlog import Event, EventLog
from talos.health import collect, run_health
from talos.telegram import TelegramChannel, TelegramClient


def test_unrecovered_channel_is_degraded_even_after_daily_window(tmp_path):
    db = tmp_path / "events.db"
    log = EventLog(db)
    log.append(Event("poll", "ingress", "channel.error",
                     {"channel": "telegram", "error": "timeout"}), now=100)
    data = collect(db_path=db, schedule_db=tmp_path / "none",
                   anchors_path=tmp_path / "absent", now=200000)
    assert data["event_log"]["errors_24h"] == 0
    assert data["status"] == "degraded"
    assert data["channels"]["telegram"]["status"] == "degraded"


def test_recovery_only_clears_its_channel_and_preserves_error_count(tmp_path):
    db = tmp_path / "events.db"
    log = EventLog(db)
    for name in ("telegram", "mail"):
        log.append(Event("poll", "ingress", "channel.error",
                         {"channel": name, "error": "timeout"}), now=100)
    log.append(Event("poll", "ingress", "channel.ready", {"channel": "telegram"}), now=101)
    kw = dict(db_path=db, schedule_db=tmp_path / "none", anchors_path=tmp_path / "absent", now=102)
    data = collect(**kw)
    assert data["status"] == "degraded"
    assert data["channels"]["telegram"]["status"] == "ready"
    assert data["channels"]["mail"]["status"] == "degraded"
    log.append(Event("poll", "ingress", "channel.ready", {"channel": "mail"}), now=102)
    data = collect(**kw)
    assert data["status"] == "ok"
    assert data["event_log"]["errors_24h"] == 2
    assert log.verify() is None


def test_failed_readiness_record_does_not_lose_received_command():
    class Channel:
        name = "test"
        def poll(self):
            return ["command"]
    calls = []
    def record(name):
        calls.append(name)
        if len(calls) == 1:
            raise OSError("log unavailable")
    registry = ChannelRegistry((Channel(),), on_ready=record)
    assert registry.poll_all() == ["command"]
    assert registry.poll_all() == ["command"]
    assert registry.poll_all() == ["command"]
    assert calls == ["test", "test"]


def test_composition_records_readiness_with_channel_name_only():
    source = Path(__file__).resolve().parents[1] / "talos" / "__main__.py"
    tree = ast.parse(source.read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "ChannelRegistry"]
    assert len(calls) == 1
    hook = next(k.value for k in calls[0].keywords if k.arg == "on_ready")
    recorded = []
    class Log:
        def append(self, event):
            recorded.append(event)
    callback = eval(compile(ast.Expression(hook), "<hook>", "eval"), {"log": Log(), "Event": Event})
    callback("telegram")
    assert recorded == [Event("poll", "ingress", "channel.ready", {"channel": "telegram"})]


def test_an_answer_does_not_prove_channel_recovery(tmp_path):
    db = tmp_path / "events.db"
    log = EventLog(db)
    log.append(Event("poll", "ingress", "channel.error", {"channel": "telegram"}), now=100)
    log.append(Event("task", "reasoner", "reason.done", {"status": "answered"}), now=101)
    data = collect(db_path=db, schedule_db=tmp_path / "none",
                   anchors_path=tmp_path / "absent", now=102)
    assert data["status"] == "degraded"


def test_degraded_cli_is_visible_but_keeps_warning_exit_contract(tmp_path):
    db = tmp_path / "events.db"
    log = EventLog(db)
    log.append(Event("poll", "ingress", "channel.error",
                     {"channel": "telegram", "error": "timeout"}), now=100)
    out = io.StringIO()
    code = run_health([], stdout=out, db_path=db, schedule_db=tmp_path / "none",
                      anchors_path=tmp_path / "absent", now=101)
    assert code == 0  # Exit 1 remains reserved for integrity failure.
    assert "status degraded" in out.getvalue()
    assert "telegram" in out.getvalue() and "recovery not yet confirmed" in out.getvalue()


@pytest.mark.parametrize("failure", ["timeout", "502", "401", "409", "dns"])
def test_real_channel_recovers_with_same_offset_and_no_send(monkeypatch, tmp_path, failure):
    db = tmp_path / "events.db"
    log = EventLog(db)
    now = [100.0]
    offsets = []
    responses = [[], failure, [{"update_id": 7, "message": {
        "from": {"id": 7}, "chat": {"id": 42}, "text": "hello"}}], []]

    def get(url, **kwargs):
        offsets.append(kwargs["params"]["offset"])
        result = responses.pop(0)
        if result == "timeout":
            raise requests.ReadTimeout(url)
        if result == "dns":
            raise requests.ConnectionError(url)
        response = requests.Response()
        response.status_code = int(result) if isinstance(result, str) else 200
        response.url = url
        response._content = json.dumps({"ok": response.status_code == 200,
                                       "result": result}).encode()
        return response

    def forbidden(*args, **kwargs):
        pytest.fail("Polling recovery must not send messages or replay actions")

    monkeypatch.setattr("talos.telegram.requests.get", get)
    monkeypatch.setattr("talos.telegram.requests.post", forbidden)
    def emit(kind, payload):
        log.append(Event("poll", "ingress", kind, payload), now=now[0])
    registry = ChannelRegistry(
        (TelegramChannel(TelegramClient("fixture-token", 20)),),
        on_error=lambda name, error: emit("channel.error", {"channel": name, "error": str(error)}),
        on_ready=lambda name: emit("channel.ready", {"channel": name}),
        clock=lambda: now[0], sleep=lambda seconds: None,
    )
    kw = dict(db_path=db, schedule_db=tmp_path / "none", anchors_path=tmp_path / "absent", now=102)
    assert registry.poll_all() == []  # First proven poll, even if empty.
    assert registry.poll_all() == []  # Failure is retained.
    assert collect(**kw)["status"] == "degraded"
    assert registry.poll_all() == []  # Backoff does not touch the network.
    assert offsets == [0, 0]
    now[0] += 1
    incoming = registry.poll_all()
    assert len(incoming) == 1 and incoming[0].text == "hello"
    assert registry.poll_all() == []
    assert offsets == [0, 0, 0, 8]
    assert collect(**kw)["status"] == "ok"
    rows = log.recent(20)
    assert [r["type"] for r in reversed(rows)] == ["channel.ready", "channel.error", "channel.ready"]
    assert "fixture-token" not in json.dumps(rows)
    assert log.verify() is None
