"""Actual Telegram HTTP transport, worker, model router and kernel in one fixture.

Only the Telegram server and model are local doubles. No live bot is polled and
no provider credentials or operator conversation enter this test.
"""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import parse_qs

import pytest

from talos import telegram
from talos.commands import CommandCenter
from talos.eventlog import EventLog
from talos.provider import ModelPicker, ModelRouter, ModelSelection, Provider, ProviderRegistry
from talos.provider_errors import cli_failure
from talos.worker import Worker
from test_conductor import _build, _tool_call, OWNER, CHAT_OWNER


@pytest.mark.parametrize('kind,attempts', [('empty_response', 2), ('rate_limited', 1), ('network_failed', 1)])
def test_telegram_controls_recover_after_a_tool_and_provider_failure(tmp_path, monkeypatch, kind, attempts):
    inbound, outbound, prompts, failures, probes = [], [], [], [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            items = list(inbound)
            inbound.clear()
            self.reply(items)

        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            data = (json.loads(body) if 'application/json' in self.headers.get('Content-Type', '')
                    else {k: v[0] for k, v in parse_qs(body.decode()).items()})
            outbound.append(data)
            self.reply({'message_id': len(outbound)})

        def reply(self, data):
            body = json.dumps({'ok': True, 'result': data}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(telegram, '_BASE', f'http://127.0.0.1:{server.server_port}/bot{{token}}/{{method}}')
    channel = telegram.TelegramChannel(telegram.TelegramClient('local-fixture', 0))
    target = tmp_path / 'receipt.txt'

    class Backend:
        timeout_s = 10

        def __init__(self, selection):
            self.selection = selection
            self.calls = 0

        def validate(self):
            probes.append(self.selection.model)
            assert self.selection.model == 'healthy'

        def reason_strict(self, prompt, *, timeout_s):
            self.calls += 1
            prompts.append(prompt)
            if self.selection.model == 'healthy':
                assert 'ORBIT receipt' in prompt
                return 'The ORBIT receipt was written; the interrupted verification remains unverified.'
            if self.calls == 1:
                return _tool_call('write_file', {'path': str(target), 'content': 'exactly once'}, [])
            failures.append(kind)
            raise cli_failure('TALOS_PROVIDER_ERROR ' + json.dumps({
                'type': 'provider_error', 'kind': kind, 'prompt': 'private-fixture',
                'token': 'credential-fixture'}), '', 75, provider='fixture', model='limited')

    registry = ProviderRegistry([Provider('fixture', 'Fixture', ('limited', 'healthy'))])
    router = ModelRouter(registry, ModelSelection('fixture', 'limited'), Backend,
                         EventLog(tmp_path / 'models.db'))
    conductor, _ = _build(tmp_path, router)
    worker = Worker(lambda update: conductor.handle(update))
    commands = CommandCenter(conductor.log, conductor.approvals, conductor.executor.policy,
                             time.time(), 'fixture', router, worker, tmp_path,
                             memory=conductor.memory, model_picker=ModelPicker(registry, router))
    conductor = replace(conductor, commands=commands, send=channel.send,
                        send_structured=channel.send_structured,
                        begin_activity=channel.begin_activity)
    sequence = 0

    def receive(text):
        nonlocal sequence
        sequence += 1
        inbound.append({'update_id': sequence, 'message': {
            'message_id': sequence, 'from': {'id': int(OWNER.user_id)},
            'chat': {'id': int(OWNER.user_id)}, 'text': text}})
        result = channel.poll()
        assert len(result) == 1 and not channel.poll()
        return result[0]

    def complete(update):
        done = threading.Event()
        states = []
        assert worker.submit(update)
        def observe(state):
            states.append(state)
            if state in {'ended', 'failed', 'cancelled'}:
                done.set()
        worker.watch(update, observe)
        assert done.wait(10), states
        return states

    worker.start()
    try:
        assert not probes  # Startup never depends on a remote probe.
        states = complete(receive('Write the ORBIT receipt, then verify it.'))
        assert states[-1] == 'failed'
        assert failures == [kind] * attempts
        assert target.read_text() == 'exactly once'
        assert router.readiness()['state'] == 'unavailable'
        assert not worker.busy() and worker.pending() == 0
        for command in ('/model', '/status', '/queue', '/stop'):
            before = len(outbound)
            start = time.monotonic()
            assert conductor.handle(receive(command))
            assert len(outbound) > before and time.monotonic() - start < 2
        assert failures == [kind] * attempts  # Controls caused no model call.
        assert conductor.handle(receive('/model fixture healthy'))
        assert probes == ['healthy']
        assert complete(receive('?'))[-1] == 'ended'
        assert router.readiness()['state'] == 'ready'
        assert not worker.busy() and worker.pending() == 0
        results = [e for e in conductor.log.recent(100) if e['type'] == 'exec.result']
        assert len(results) == 1 and results[0]['payload']['status'] == 'done'
        assert target.read_text() == 'exactly once'
        rendered = json.dumps(outbound)
        assert 'ORBIT receipt' in rendered
        for forbidden in ('TOOL_CALL:', 'private-fixture', 'credential-fixture'):
            assert forbidden not in rendered
        assert len(conductor.memory.recall(CHAT_OWNER)) >= 4
    finally:
        worker.stop()
        server.shutdown()
        server.server_close()
        thread.join(2)
