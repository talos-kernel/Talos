"""Telegram HTTP and the production routing seams under overlapping real tool runs.

Only the remote Telegram API and model are fixtures. Channels, polling, routing,
worker, conductor, kernel, file reads, memory and SQLite receipts are real.
"""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import threading
from urllib.parse import parse_qs

import pytest

from talos import telegram
from talos.__main__ import _queued_on_purpose, _steers_the_running_task
from talos.worker import Worker
from talos.provider import ModelRouter, ModelSelection, Provider, ProviderRegistry
from test_standing_flow import OWNER, CHAT, call
from test_chat_command_compatibility import background_rig, wait_for


@pytest.mark.parametrize('alias', ['btw', 'bg', 'background'])
@pytest.mark.parametrize('fallback_mode', [False, True])
def test_background_finishes_while_main_runs_then_steer_and_queue_work(tmp_path, monkeypatch, alias, fallback_mode):
    inbound, outbound, errors = [], [], []
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
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
                errors.append(self.path)
            with lock:
                items = list(inbound)
                inbound.clear()
            self.reply(items)
        def do_POST(self):
            method = self.path.rsplit('/', 1)[-1]
            if method not in {'sendMessage', 'editMessageText', 'answerCallbackQuery'}:
                errors.append(method)
            data = self.rfile.read(int(self.headers['Content-Length'])).decode()
            payload = (json.loads(data) if 'application/json' in self.headers.get('Content-Type', '')
                       else {k: v[0] for k, v in parse_qs(data).items()})
            with lock:
                outbound.append((method, payload))
            self.reply({'message_id': len(outbound)})

    main_started, release_main = threading.Event(), threading.Event()
    main_finished = threading.Event()
    source = tmp_path / 'source.txt'
    source.write_text('verified file contents')
    prompts = []
    counts = {}
    class Reasoner:
        def reason(self, prompt):
            name = threading.current_thread().name
            prompts.append((name, prompt))
            counts[name] = counts.get(name, 0) + 1
            if name.startswith('talos-bg-'):
                assert 'main-only context' not in prompt
                if counts[name] == 1:
                    return call('read_file', {'path': str(source)})
                assert 'verified file contents' in prompt
                return 'BACKGROUND_CHECKED'
            if counts[name] == 1:
                main_started.set()
                assert release_main.wait(5)
                return call('read_file', {'path': str(source)})
            if counts[name] == 2:
                assert 'Use the exact receipt' in prompt
                assert 'BACKGROUND_CHECKED' not in prompt
                main_finished.set()
                return 'MAIN_CHECKED'
            assert 'separate next turn' in prompt
            return 'QUEUED_CHECKED'
        def cancel(self):
            return False

    rig = background_rig(tmp_path, Reasoner())
    registry = ProviderRegistry([Provider('fixture', 'Fixture', ('model',))])
    router = ModelRouter(registry, ModelSelection('fixture', 'model'), lambda _: Reasoner(), rig.log)
    if fallback_mode:
        from talos.fallback import FallbackReasoner
        from talos.provider_errors import ReasonerFailure
        class Limited:
            def reason(self, prompt):
                raise ReasonerFailure('Fixture limit', kind='rate_limited')
        primary = ModelRouter(registry, ModelSelection('fixture', 'model'), lambda _: Limited(), rig.log)
        router = FallbackReasoner(primary, (ModelSelection('fixture', 'backup'),),
                                  lambda _: Reasoner(), rig.log)
    object.__setattr__(rig.conductor, 'reasoner', router)
    object.__setattr__(rig.commands, 'reasoner', router)
    rig.conductor.memory.remember(CHAT, asked='main-only context', answered='keep this')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    monkeypatch.setattr(telegram, '_BASE', f'http://127.0.0.1:{server.server_port}/bot{{token}}/{{method}}')
    channel = telegram.TelegramChannel(telegram.TelegramClient('fixture', 0))
    conductor = replace(rig.conductor, send=channel.send, send_structured=channel.send_structured)
    worker = Worker(conductor.handle)
    object.__setattr__(conductor, 'commands', replace(rig.commands, worker=worker, memory=conductor.memory))
    worker.start()
    sequence = 0

    def route(text):
        nonlocal sequence
        sequence += 1
        with lock:
            inbound.append({'update_id': sequence, 'message': {'message_id': sequence,
                'from': {'id': int(OWNER.user_id)}, 'chat': {'id': int(OWNER.user_id)}, 'text': text}})
        updates = channel.poll()
        assert len(updates) == 1
        update = updates[0]
        queued = _queued_on_purpose(update)
        if queued is not None:
            assert worker.submit(queued)
        elif conductor.is_inline(update):
            assert conductor.handle(update)
        elif not _steers_the_running_task(conductor, conductor.questions, update):
            assert worker.submit(update)

    def rendered():
        with lock:
            return json.dumps(outbound)

    try:
        route('Main task')
        assert main_started.wait(5)
        route(f'/{alias}\nIndependent file check')
        wait_for(lambda: 'BACKGROUND_CHECKED' in rendered() and conductor.background.busy() == 0)
        assert not main_finished.is_set() and worker.busy()
        assert conductor.redirect.is_open()
        count_before = len(prompts)
        route('/whoami')
        route('/tasks')
        route('/steer Use the exact receipt')
        route('/q separate next turn')
        route('/queue')
        assert len(prompts) == count_before, 'control commands invoked the provider'
        assert 'Allowed: yes' in rendered() and 'Waiting: 1' in rendered()
        assert 'No background tasks running' in rendered()
        assert worker.pending() == 1
        release_main.set()
        wait_for(lambda: 'QUEUED_CHECKED' in rendered() and not worker.busy())
        assert main_finished.is_set() and worker.pending() == 0
        assert not errors and 'TOOL_CALL:' not in rendered()
        history = str(conductor.memory.recall(CHAT))
        assert 'MAIN_CHECKED' in history and 'QUEUED_CHECKED' in history
        assert 'BACKGROUND_CHECKED' not in history
        assert source.read_text() == 'verified file contents'
        with sqlite3.connect(f'file:{tmp_path / "ev.db"}?mode=ro', uri=True) as db:
            assert db.execute("select count(*) from events where type='background.finished'").fetchone()[0] == 1
            assert db.execute("select count(*) from events where type='exec.result' and json_extract(payload_json,'$.status')='done'").fetchone()[0] == 2
            assert db.execute("select count(*) from events where type='command.steered'").fetchone()[0] == 1
    finally:
        release_main.set()
        worker.stop()
        wait_for(lambda: conductor.background.busy() == 0)
        server.shutdown()
        server.server_close()
        server_thread.join(2)
