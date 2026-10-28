"""Long work remains visible without exposing model/tool data or replaying effects."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from talos import telegram
from talos.agent_loop import AgentProgress, ProgressStage
from talos.ux import EXPRESSIVE
from test_telegram_ux import FakeTelegramClient, _tool, _result


def setup_activity(client=None, **kwargs):
    client = client or FakeTelegramClient()
    activity = telegram.TelegramActivity(client, 42, clock=lambda: client.now[0],
                                         heartbeat_s=0, style=EXPRESSIVE, **kwargs)
    return client, activity


def updates(client):
    return [text for _, text, _ in client.sent if ' · Update · ' in text]


def test_long_tool_reports_receipts_and_current_work_once_per_minute():
    client, activity = setup_activity()
    activity.progress(_tool('read_file', summary='private-path'))
    activity.progress(_result('read_file', 'done'))
    activity.progress(_tool('web_fetch', summary='private-url'))
    activity.progress(_result('web_fetch', 'error'))
    activity.progress(_tool('delegate', summary='TOOL_CALL: secret prompt'))
    client.now[0] = 59
    activity.tick()
    assert not updates(client)
    client.now[0] = 60
    activity.tick()
    activity.tick()
    assert len(updates(client)) == 1
    text = updates(client)[0]
    assert '1 tool actions completed; 1 failed or refused.' in text
    assert 'Delegating' in text and 'Still waiting' in text
    assert not any(value in text for value in ['private-path', 'private-url', 'secret', 'TOOL_CALL'])
    client.now[0] = 120
    activity.tick()
    assert len(updates(client)) == 1
    assert any(' · Update · 2m 00s' in text for _, _, text, _ in client.edited)
    activity.succeed()


def test_slow_inference_also_gets_an_update_but_short_answers_stay_quiet():
    client, activity = setup_activity()
    activity.progress(AgentProgress(ProgressStage.THINKING))
    client.now[0] = 30
    activity.tick()
    assert not client.sent
    client.now[0] = 60
    activity.tick()
    assert 'no new result yet' in updates(client)[0]
    assert activity._message_id is None
    activity.succeed()


@pytest.mark.parametrize('terminal', ['succeed', 'fail'])
def test_no_new_updates_or_live_edits_after_terminal_state(terminal):
    client, activity = setup_activity()
    activity.progress(_tool('delegate', summary='delegating'))
    client.now[0] = 60
    activity.tick()
    getattr(activity, terminal)('finished')
    sent, edits = len(client.sent), len(client.edited)
    client.now[0] = 600
    activity.tick()
    activity.progress(_tool('read_file', summary='late event'))
    assert len(client.sent) == sent and len(client.edited) == edits


def test_pending_approval_does_not_send_repetitive_waiting_updates():
    client, activity = setup_activity()
    activity.progress(_tool('write_file', summary='out.txt'))
    activity.progress(_result('write_file', 'needs_human'))
    client.now[0] = 120
    activity.tick()
    assert not updates(client)
    activity.succeed()


def test_failed_notification_is_bounded_and_does_not_stop_activity():
    class FailingClient(FakeTelegramClient):
        attempts = 0
        def send_message(self, chat_id, text, **kwargs):
            if ' · Update · ' in text:
                self.attempts += 1
                raise OSError('offline')
            return super().send_message(chat_id, text, **kwargs)
    client, activity = setup_activity(FailingClient())
    activity.progress(_tool('read_file', summary='read'))
    client.now[0] = 60
    activity.tick()
    activity.tick()
    assert client.attempts == 1
    activity.progress(_result('read_file', 'done'))
    activity.succeed()
    assert activity._finished
    assert 'Reply done' in client.sent[-1][1]   # die Zusammenfassung traegt der Beleg
    assert 'Work trail' in client.edited[-1][2]    # die Anzeige friert als Spur ein


def test_the_frozen_display_does_not_repeat_the_receipt_summary():
    """Gemessen am 18.09.2026: der Endstand stand doppelt im Chat — als Kopf der
    eingefrorenen Anzeige und noch einmal als Beleg. Die Zusammenfassung gehoert
    genau einmal in den Chat: in den Beleg; die Anzeige bleibt reine Arbeitsspur."""
    client, activity = setup_activity()
    activity.progress(_tool('read_file', summary='read'))
    activity.progress(_result('read_file', 'done'))
    activity.succeed('✓ 5s · 1k tok · testmodell')
    alle = [text for _, text, _ in client.sent] + [text for _, _, text, _ in client.edited]
    assert sum('Reply done' in text for text in alle) == 1
    spur = client.edited[-1][2]
    assert 'Work trail' in spur and 'Reply done' not in spur
    assert 'testmodell' not in spur               # die Quittung steht nur im Beleg


def test_failed_update_edit_does_not_send_more_notifications_or_retry_tools():
    class FailedEditClient(FakeTelegramClient):
        attempts = 0
        def edit_message_text(self, chat_id, message_id, text, **kwargs):
            if ' · Update · ' in text:
                self.attempts += 1
                raise OSError('offline')
            return super().edit_message_text(chat_id, message_id, text, **kwargs)
    client, activity = setup_activity(FailedEditClient())
    activity.progress(_tool('read_file', summary='read'))
    for timestamp in (60, 120, 120, 180):
        client.now[0] = timestamp
        activity.tick()
    assert len(updates(client)) == 1 and client.attempts == 2
    assert activity._tool_calls == 1
    activity.succeed()


def test_real_heartbeat_delivers_update_through_telegram_http_without_new_tool_calls(monkeypatch):
    received, errors = [], []
    got_update = threading.Event()
    now = [0.0]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            method = self.path.rsplit('/', 1)[-1]
            if method not in {'sendChatAction', 'sendMessage', 'editMessageText', 'deleteMessage'}:
                errors.append(method)
            payload = {key: value[0] for key, value in parse_qs(
                self.rfile.read(int(self.headers['Content-Length'])).decode()).items()}
            received.append((method, payload))
            body = json.dumps({'ok': True, 'result': {'message_id': len(received)}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            if ' · Update · ' in payload.get('text', ''):
                got_update.set()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    monkeypatch.setattr(telegram, '_BASE', f'http://127.0.0.1:{server.server_port}/bot{{token}}/{{method}}')
    activity = None
    try:
        activity = telegram.TelegramActivity(telegram.TelegramClient('fixture', 0), 42,
            clock=lambda: now[0], heartbeat_s=0.01, style=EXPRESSIVE)
        activity.progress(_tool('delegate', summary='private task'))
        now[0] = 61
        assert got_update.wait(3), 'heartbeat did not deliver a fresh Telegram message'
        activity.fail('Cancelled.')
        activity._beat.join(2)
        terminal = len(received)
        now[0] = 600
        activity.tick()
        assert len(received) == terminal and not errors
        notes = [p['text'] for m, p in received if m == 'sendMessage' and ' · Update · ' in p.get('text', '')]
        assert len(notes) == 1 and 'Delegating' in notes[0]
        assert 'private task' not in notes[0] and activity._tool_calls == 1
        assert all(p['chat_id'] == '42' for _, p in received)
    finally:
        if activity:
            activity.succeed()
        server.shutdown()
        serving.join(2)
        server.server_close()


@pytest.mark.parametrize('status, state', [
    ('done', 'Reply done'), ('error', 'Reply done with issues'),
])
@pytest.mark.parametrize('count', [None, 0, 1, 2])
def test_reply_receipt_reports_only_pending_job_count(status, state, count):
    queried = []
    def background_jobs(conversation):
        queried.append(conversation)
        return count
    client, activity = setup_activity(
        name='Talos', conversation='telegram:42',
        background_jobs=background_jobs if count is not None else None,
    )
    activity.progress(_tool('delegate_code', summary='private model text'))
    activity.progress(_result('delegate_code', status))
    client.now[0] = 15
    activity.succeed()
    receipt = client.sent[-1][1]
    glyph = '⚠️' if status == 'error' else '🏁'
    assert receipt.splitlines()[0] == f'{glyph} Talos · {state} — ⏱ 15s · 1 tool call'
    assert queried == (['telegram:42'] if count is not None else [])
    if count:
        assert receipt.splitlines()[-1] == (
            f"⏳ {count} background job{'s' if count != 1 else ''} still running "
            '· result follows separately'
        )
    else:
        assert 'background job' not in receipt and '⏳' not in receipt
    assert 'private model text' not in receipt
    trail = client.edited[-1][2]
    assert 'Work trail — 🏁 reply receipt below' in trail
    assert 'Reply done' not in trail and 'background job' not in trail
    # Terminal delivery is append-only and idempotent.
    sent = len(client.sent)
    activity.succeed()
    assert len(client.sent) == sent


def test_background_query_failure_never_prevents_the_reply_receipt():
    def broken(_):
        raise RuntimeError('private exception details')
    client, activity = setup_activity(background_jobs=broken)
    activity.progress(_tool('delegate_codex', summary='private prompt'))
    activity.succeed()
    assert 'Reply done' in client.sent[-1][1]
    assert 'background job' not in client.sent[-1][1]
    assert 'private' not in client.sent[-1][1]
    assert 'Work trail' in client.edited[-1][2]


@pytest.mark.parametrize('value', ['private model text', True, -1, 1.5])
def test_background_query_never_renders_non_count_data(value):
    client, activity = setup_activity(background_jobs=lambda _: value)
    activity.progress(_tool('delegate_agy', summary='work'))
    activity.succeed()
    assert 'Reply done' in client.sent[-1][1]
    assert 'background job' not in client.sent[-1][1]
    assert 'private model text' not in client.sent[-1][1]


def test_pending_approval_has_no_reply_receipt_even_with_pending_jobs():
    queried = []
    def background_jobs(conversation):
        queried.append(conversation)
        return 2
    client, activity = setup_activity(background_jobs=background_jobs)
    activity.progress(_tool('write_file', summary='out.txt'))
    activity.progress(_result('write_file', 'needs_human'))
    sent = len(client.sent)
    activity.succeed(receipt=False)
    assert len(client.sent) == sent
    assert not queried and activity._receipt_id is None
    assert 'Needs your approval' in client.edited[-1][2]
    assert 'Reply done' not in client.edited[-1][2]


def test_tool_free_reply_stays_quiet_with_pending_jobs():
    client, activity = setup_activity(background_jobs=lambda _: 2)
    activity.succeed()
    assert not client.sent


@pytest.mark.parametrize('issues', [False, True])
def test_receipt_send_failure_uses_reply_wording_in_frozen_display(issues):
    class ReceiptFailure(FakeTelegramClient):
        def send_message(self, chat_id, text, **kwargs):
            if ' · Reply done' in text:
                raise OSError('offline')
            return super().send_message(chat_id, text, **kwargs)
    client, activity = setup_activity(ReceiptFailure(), background_jobs=lambda _: 1)
    activity.progress(_tool('delegate_code', summary='work'))
    activity.progress(_result('delegate_code', 'error' if issues else 'done'))
    activity.succeed()
    assert activity._receipt_id is None
    expected = 'Reply done with issues' if issues else 'Reply done'
    assert expected in client.edited[-1][2]
    assert not any('Reply done' in text for _, text, _ in client.sent)


def test_service_wires_live_conversation_scoped_desks_through_telegram(monkeypatch):
    """Exercise the real composition prefix without starting polling or services."""
    from types import SimpleNamespace
    from talos import __main__ as main, dag, notify, paths

    pushes, dags = notify.CompletionDesk(), dag.DagDesk()
    monkeypatch.setattr(main.notify, 'CompletionDesk', lambda: pushes)
    monkeypatch.setattr(main.dag, 'DagDesk', lambda: dags)
    monkeypatch.setattr(paths, 'initialize_profile', lambda: None)
    monkeypatch.setattr(main, 'load_config', lambda **_: SimpleNamespace(
        eventlog_db=None, allowed_principals=(), bot_token='fixture',
        poll_timeout_s=0, status_style='expressive',
    ))
    monkeypatch.setattr(main, 'EventLog', lambda _: None)
    client = FakeTelegramClient()
    monkeypatch.setattr(main, 'TelegramClient', lambda *_, **__: client)
    channels = []
    class ChannelCaptured(Exception):
        pass
    def capture_channel(*args, **kwargs):
        channels.append(telegram.TelegramChannel(*args, **kwargs))
        raise ChannelCaptured
    monkeypatch.setattr(main, 'TelegramChannel', capture_channel)
    with pytest.raises(ChannelCaptured):
        main.run(once=True)
    channel = channels[0]
    pushes.watch(notify.Watch('other', 'telegram:99', short='private other task'))
    dags.register(dag.Dag('other-graph', 'telegram:99', {}, 0))
    # Register AFTER channel construction: the callback must read live desk state.
    def receipt_for_chat():
        activity = channel.begin_activity('telegram:42')
        try:
            activity.progress(_tool('delegate_code', summary='private task'))
            activity.succeed()
            assert client.sent[-1][0] == 42
            return client.sent[-1][1]
        finally:
            activity.succeed()
            if activity._beat:
                activity._beat.join(1)
    assert 'background job' not in receipt_for_chat()
    for number, tool in enumerate(('delegate_code', 'delegate_agy', 'delegate_codex'), 1):
        pushes.watch(notify.Watch(tool, 'telegram:42', short='private task', tool=tool))
        text = receipt_for_chat()
        assert f"⏳ {number} background job{'s' if number != 1 else ''} still running" in text
        assert 'private' not in text
    dags.register(dag.Dag('own-graph', 'telegram:42', {}, 0))
    assert '⏳ 4 background jobs still running' in receipt_for_chat()
    for watch in pushes.pending():
        pushes.drop(watch.job_id)
    assert '⏳ 1 background job still running' in receipt_for_chat()
    dags.drop('own-graph')
    assert 'background job' not in receipt_for_chat()
