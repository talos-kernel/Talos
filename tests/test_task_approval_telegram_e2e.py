"""Telegram HTTP -> callback -> worker -> kernel -> real files -> durable receipt.

Transport server and model are fixtures. No real bot is polled or messaged.
The UI oracle is Telegram's actual outgoing message/keyboard payload, not a
browser rendering; browser console checks do not apply to this native UI.
"""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import threading
from urllib.parse import parse_qs

from talos import telegram
from talos.approval import ApprovalPicker
from talos.worker import Worker
from test_standing_flow import OWNER, CHAT, Rig, Scripted, call, write_to


def test_telegram_task_button_executes_once_and_leaves_no_queue(tmp_path, monkeypatch):
    inbound, outbound, http_errors = [], [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, result):
            data = json.dumps({'ok': True, 'result': result}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.path.split('?')[0].endswith('/getUpdates'):
                http_errors.append(self.path)
            items = list(inbound)
            inbound.clear()
            self.reply(items)

        def do_POST(self):
            data = self.rfile.read(int(self.headers['Content-Length'])).decode()
            payload = (json.loads(data) if 'application/json' in self.headers.get('Content-Type', '')
                       else {k: v[0] for k, v in parse_qs(data).items()})
            outbound.append((self.path.rsplit('/', 1)[-1], payload))
            self.reply({'message_id': len(outbound)})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(telegram, '_BASE', f'http://127.0.0.1:{server.server_port}/bot{{token}}/{{method}}')
    channel = telegram.TelegramChannel(telegram.TelegramClient('fixture', 0))
    a, b, later = (tmp_path / n for n in ('first', 'second', 'later'))
    rig = Rig(tmp_path, Scripted(write_to(a, 'one'), write_to(b, 'two'),
                                call('read_file', {'path': str(b)}), 'Both files are checked.', write_to(later)))
    conductor = replace(rig.conductor, approval_picker=ApprovalPicker(), send=channel.send,
                        send_structured=channel.send_structured)
    worker = Worker(lambda update: conductor.handle(update))
    worker.start()
    sequence = 0

    def complete(*, text=None, callback=None):
        nonlocal sequence
        sequence += 1
        update = {'update_id': sequence}
        if callback is not None:
            update['callback_query'] = {'id': f'query-{sequence}', 'from': {'id': int(OWNER.user_id)},
                'data': callback, 'message': {'message_id': 1, 'chat': {'id': int(OWNER.user_id)}}}
        else:
            update['message'] = {'message_id': sequence, 'from': {'id': int(OWNER.user_id)},
                                 'chat': {'id': int(OWNER.user_id)}, 'text': text}
        inbound.append(update)
        received = channel.poll()
        assert len(received) == 1
        done = threading.Event()
        states = []
        assert worker.submit(received[0])
        def observe(state):
            states.append(state)
            if state in {'ended', 'failed', 'cancelled'}:
                done.set()
        worker.watch(received[0], observe)
        assert done.wait(5), states
        assert states[-1] == 'ended', states

    def task_buttons():
        buttons = []
        for method, payload in outbound:
            markup = payload.get('reply_markup')
            if isinstance(markup, str):
                markup = json.loads(markup)
            for row in (markup or {}).get('inline_keyboard', []):
                buttons.extend(b for b in row if b['text'] == '▶ Allow this task')
        return buttons

    try:
        complete(text='Create both files and check them')
        assert not a.exists() and len(task_buttons()) == 1
        complete(callback=task_buttons()[0]['callback_data'])
        assert a.read_text() == 'one' and b.read_text() == 'two'
        assert len(task_buttons()) == 1  # no new approval between effects
        assert conductor.approvals.get(CHAT) is None
        assert not worker.busy() and worker.pending() == 0
        rendered = json.dumps(outbound)
        assert 'Both files are checked.' in rendered and 'TOOL_CALL:' not in rendered
        assert not http_errors
        with sqlite3.connect(f'file:{tmp_path / "ev.db"}?mode=ro', uri=True) as db:
            assert db.execute("select count(*) from events where type='approval.task_granted'").fetchone()[0] == 1
            assert db.execute("select count(*) from events where type='exec.result' and json_extract(payload_json,'$.status')='done'").fetchone()[0] == 3
        complete(text='A new task: create another file')
        assert not later.exists() and len(task_buttons()) == 2
        assert not worker.busy() and worker.pending() == 0
    finally:
        worker.stop()
        server.shutdown()
        server.server_close()
        thread.join(2)
