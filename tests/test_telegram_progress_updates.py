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
    assert 'Turn finished' in client.sent[-1][1]   # die Zusammenfassung traegt der Beleg
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
    assert sum('Turn finished' in text for text in alle) == 1
    spur = client.edited[-1][2]
    assert 'Work trail' in spur and 'Turn finished' not in spur
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
