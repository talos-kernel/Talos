"""Visible work during execution; only a delivered result survives cleanup.

⚠️ Diese Datei prueft den AUFRAEUM-Weg — und der ist seit dem 12.09. nicht mehr die
Vorgabe. Der Betreiber hat ausdruecklich verlangt, dass die Zwischenmeldungen stehen
bleiben, solange der Agent arbeitet: ein Zwischenstand, den er noch nicht gelesen hatte,
verschwand vor seinen Augen. Seitdem gilt `_keep_work_trail()` und `TALOS_TIDY_WORK_TRAIL=1`
schaltet das alte Verhalten wieder ein.

Die Faelle hier bleiben trotzdem vollstaendig erhalten, denn der Aufraeum-Weg existiert
weiter und muss genauso richtig sein wie vorher — er wird nur bewusst eingeschaltet.
Dass die Spur OHNE den Schalter stehen bleibt, beweist tests/test_work_trail.py.
"""
import os as _os

import pytest as _pytest


@_pytest.fixture(autouse=True)
def _aufraeumen_eingeschaltet(monkeypatch):
    """Der Schalter, unter dem diese Datei ueberhaupt etwas aussagt."""
    monkeypatch.setenv("TALOS_TIDY_WORK_TRAIL", "1")
    assert _os.environ["TALOS_TIDY_WORK_TRAIL"] == "1"

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import threading
from urllib.parse import parse_qs

import pytest

from talos import telegram
from talos.agent_loop import AgentProgress, ProgressStage
from talos.ux import EXPRESSIVE
from talos.working_display import WorkingDisplays
from talos.approval import ApprovalPicker
from talos.channel import CallbackQuery
from test_standing_flow import Rig, Scripted, call, msg, CHAT, OWNER, write_to
from test_chat_command_compatibility import wait_for


class Client:
    def __init__(self):
        self.messages, self.deleted, self.events = {}, [], []
        self.next_id = 0
        self.fail_send = False
        self.fail_delete = False

    def send_message(self, chat_id, text, **kwargs):
        if self.fail_send:
            raise OSError('offline')
        self.next_id += 1
        self.messages[self.next_id] = text
        self.events.append(('send', self.next_id, text))
        return self.next_id

    def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.messages[message_id] = text
        self.events.append(('edit', message_id, text))

    def delete_message(self, chat_id, message_id):
        if self.fail_delete:
            raise OSError('cannot delete')
        self.deleted.append(message_id)
        self.messages.pop(message_id, None)
        self.events.append(('delete', message_id, ''))

    def send_chat_action(self, *_):
        pass

    def answer_callback_query(self, *_):
        pass


def wired(rig, client, now=None):
    channel = telegram.TelegramChannel(client)
    return replace(rig.conductor, send=channel.send, begin_reply=channel.begin_reply,
        begin_activity=lambda chat: telegram.TelegramActivity(client, int(chat.split(':')[1]),
            heartbeat_s=0, clock=lambda: (now or [0])[0], style=EXPRESSIVE))


@pytest.mark.parametrize('streamed', [False, True])
def test_narration_and_tool_trail_are_visible_then_only_verified_result_remains(tmp_path, streamed):
    client = Client()
    target = tmp_path/'input.txt'
    target.write_text('bronze')
    class Model:
        calls = 0
        def reason(self, prompt, on_text=None):
            self.calls += 1
            if self.calls == 1:
                text = 'I will inspect the file.\n'+call('read_file', {'path': str(target)})
            elif self.calls == 2:
                assert 'bronze' in prompt
                assert any('I will inspect' in v for v in client.messages.values())
                assert any('Reading' in v for v in client.messages.values())
                text = 'The file contains bronze. I will check it once more.\n'+call('read_file', {'path': str(target)})
            else:
                assert any('The file contains bronze.' in v for v in client.messages.values())
                assert not client.deleted
                text = 'Verified result: bronze.'
            if streamed and on_text:
                for offset in range(0, len(text), 7):
                    on_text(text[offset:offset+7])
            return text
    model = Model()
    rig = Rig(tmp_path, model)
    conductor = wired(rig, client)
    assert conductor.handle(msg(30000, 'Check the file'))
    wait_for(lambda: len(client.messages) == 1)
    assert model.calls == 3 and len(client.messages) == 1
    assert 'Verified result: bronze.' in next(iter(client.messages.values()))
    assert client.deleted and not any('TOOL_CALL:' in v for v in client.messages.values())
    assert 'Verified result' in str(conductor.memory.recall(CHAT))
    with sqlite3.connect(tmp_path/'ev.db') as db:
        assert db.execute("select count(*) from events where type='exec.result'").fetchone()[0] == 2


@pytest.mark.parametrize('decision', ['allow this task', 'no', '/stop'])
def test_approval_continuation_or_cancellation_cleans_original_work(tmp_path, decision):
    client = Client()
    output = tmp_path/'out.txt'
    model = Scripted('I will create the file.\n'+write_to(output, 'checked'), 'The file was written.')
    rig = Rig(tmp_path, model)
    conductor = wired(rig, client)
    assert conductor.handle(msg(30001, 'Create the file'))
    temporary = {mid for mid, text in client.messages.items() if 'I will create' in text or 'Working' in text or 'Needs your approval' in text}
    assert temporary and not client.deleted and not output.exists()
    assert conductor.handle(msg(30002, decision))
    wait_for(lambda: temporary.isdisjoint(client.messages))
    assert temporary.isdisjoint(client.messages)
    assert output.exists() == (decision == 'allow this task')
    # Approval/decision messages belong to the conversation and are never swept.
    assert client.messages


@pytest.mark.parametrize('delete_fails', [False, True])
def test_cleanup_never_deletes_adopted_answer_or_other_task(delete_fails):
    client = Client()
    client.fail_delete = delete_fails
    work = WorkingDisplays()
    streams = [telegram.TelegramReply(client, 42) for _ in range(2)]
    keys = [('owner', 'chat', 'main'), ('owner', 'chat', 'side')]
    for stream, key in zip(streams, keys):
        work.add(key, stream)
        stream.complete_turn(key[2]+' progress\n'+call('read_file', {'path': 'file'}))
    side_id = next(mid for mid, text in client.messages.items() if text == 'side progress')
    streams[0].push('Main result')
    assert streams[0].adopt('Main result')
    answer_id = next(mid for mid, text in client.messages.items() if text == 'Main result')
    work.cleanup(keys[0])
    if not delete_fails:
        wait_for(lambda: len(client.messages) == 2)
    assert answer_id in client.messages and side_id in client.messages
    assert answer_id not in client.deleted and side_id not in client.deleted


def test_failed_final_delivery_preserves_visible_error_and_receipts(tmp_path):
    class FailedAnswerClient(Client):
        def edit_message_text(self, chat_id, message_id, text, **kwargs):
            if 'Result which could not be delivered' in text:
                raise OSError('offline')
            super().edit_message_text(chat_id, message_id, text, **kwargs)
    client = FailedAnswerClient()
    source = tmp_path/'in.txt'; source.write_text('contents')
    class Model:
        calls = 0
        def reason(self, prompt):
            self.calls += 1
            if self.calls == 1:
                return 'Reading the file.\n'+call('read_file', {'path': str(source)})
            client.fail_send = True
            return 'Result which could not be delivered'
    rig = Rig(tmp_path, Model())
    conductor = wired(rig, client)
    assert not conductor.handle(msg(30003, 'Read the file'))
    assert not client.deleted and client.messages
    assert any('could not deliver the answer' in value for value in client.messages.values())
    assert not conductor.memory.recall(CHAT)


def test_provider_failure_leaves_one_compact_failure_instead_of_a_working_card(tmp_path):
    client = Client()
    source = tmp_path/'in.txt'; source.write_text('contents')
    class Model:
        calls = 0
        def reason(self, prompt):
            self.calls += 1
            if self.calls == 1:
                return 'Reading the file.\n'+call('read_file', {'path': str(source)})
            raise RuntimeError('Provider unavailable token=synthetic-value')
    rig = Rig(tmp_path, Model())
    assert not wired(rig, client).handle(msg(30004, 'Read the file'))
    wait_for(lambda: len(client.messages) == 1)
    assert len(client.messages) == 1
    remaining = next(iter(client.messages.values()))
    assert 'failed' in remaining and '[REDACTED]' in remaining
    assert 'synthetic-value' not in remaining


def test_telegram_http_removes_only_temporary_ids_after_final_delivery(tmp_path, monkeypatch):
    client_state = Client()
    errors = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            method = self.path.rsplit('/', 1)[-1]
            raw = self.rfile.read(int(self.headers['Content-Length'])).decode()
            payload = {key: values[0] for key, values in parse_qs(raw).items()}
            chat = int(payload['chat_id'])
            if method == 'sendMessage':
                result = {'message_id': client_state.send_message(chat, payload['text'])}
            elif method == 'editMessageText':
                client_state.edit_message_text(chat, int(payload['message_id']), payload['text']); result = True
            elif method == 'deleteMessage':
                # Every deletion must come after the visible final answer exists.
                if not any('Final: bronze' in value for value in client_state.messages.values()):
                    errors.append('deleted before result')
                client_state.delete_message(chat, int(payload['message_id'])); result = True
            elif method == 'sendChatAction':
                result = True
            else:
                errors.append(method); result = False
            body = json.dumps({'ok': True, 'result': result}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setattr(telegram, '_BASE', f'http://127.0.0.1:{server.server_port}/bot{{token}}/{{method}}')
    source = tmp_path/'in.txt'; source.write_text('bronze')
    model = Scripted('I will check the file.\n'+call('read_file', {'path': str(source)}), 'Final: bronze')
    rig = Rig(tmp_path, model)
    try:
        assert wired(rig, telegram.TelegramClient('fixture', 0)).handle(msg(30005, 'Read the file'))
        wait_for(lambda: len(client_state.messages) == 1)
        assert not errors and len(client_state.messages) == 1 and client_state.deleted
        assert 'Final: bronze' in next(iter(client_state.messages.values()))
        with sqlite3.connect(tmp_path/'ev.db') as db:
            assert db.execute("select count(*) from events where type='exec.result'").fetchone()[0] == 1
        assert source.read_text() == 'bronze'
    finally:
        server.shutdown(); thread.join(2); server.server_close()


def test_cleanup_does_not_hold_the_task_while_telegram_is_blocked():
    entered, release = threading.Event(), threading.Event()
    class Display:
        def cleanup(self):
            entered.set()
            assert release.wait(3)
    work = WorkingDisplays()
    key = ('owner', 'chat', 'task')
    work.add(key, Display())
    try:
        work.cleanup(key)
        assert entered.wait(1)
        assert not release.is_set(), 'cleanup blocked its caller'
    finally:
        release.set()


def test_cancelled_model_response_cannot_post_late_narration():
    from talos.conductor import Conductor
    from talos.run_control import active
    client = Client()
    stream = telegram.TelegramReply(client, 42)
    with active(lambda: True):
        Conductor._complete_stream_turn(stream, 'Late progress\n'+call('read_file', {'path': 'file'}))
    assert not client.messages


def test_all_periodic_notes_are_removed_without_deleting_the_result():
    now = [0]
    client = Client()
    activity = telegram.TelegramActivity(client, 42, clock=lambda: now[0], heartbeat_s=0)
    activity.progress(AgentProgress(ProgressStage.TOOL, tool='read_file'))
    for timestamp in [60, 120]:
        now[0] = timestamp
        activity.tick()
    assert len(client.messages) == 2  # one activity card and one edited update
    assert any('2m 00s' in text for text in client.messages.values())
    result = client.send_message(42, 'Final result')
    activity.succeed()
    activity.cleanup()
    now[0] = 600
    activity.tick()
    assert client.messages == {result: 'Final result'}


def test_many_tool_updates_edit_one_narration_then_adopt_the_result():
    client = Client()
    reply = telegram.TelegramReply(client, 42)
    for index in range(20):
        reply.begin_turn()
        reply.complete_turn(f'Checked item {index}.\n'+call('read_file', {'path':'fixture'}))
        assert len(client.messages) == 1
    assert reply.adopt('Twenty items checked.')
    reply.cleanup()
    assert list(client.messages.values()) == ['Twenty items checked.']
    assert not client.deleted


def test_task_approval_button_continues_narration_but_edits_one_final_answer(tmp_path):
    client = Client()
    out = tmp_path/'out.txt'
    model = Scripted('I will write the file.\n'+write_to(out, 'bronze'),
        'The write succeeded. I will verify its contents.\n'+call('read_file', {'path': str(out)}),
        'Verified bronze result.')
    rig = Rig(tmp_path, model)
    channel = telegram.TelegramChannel(client)
    keyboards = []
    def structured(chat, message):
        keyboards.append(message)
        channel.send_structured(chat, message)
    conductor = replace(wired(rig, client), approval_picker=ApprovalPicker(), send_structured=structured)
    assert conductor.handle(msg(30006, 'Write and verify'))
    button = next(button for row in keyboards[-1].keyboard for button in row if button.label == '▶ Allow this task')
    prompt_id = max(client.messages)
    update = replace(msg(30007, ''), callback=CallbackQuery('fixture-query', button.data, prompt_id))
    assert conductor.handle(update)
    wait_for(lambda:len(client.messages)==1)
    assert client.messages.keys() == {prompt_id}
    assert 'Verified bronze result' in client.messages[prompt_id]
    assert any('The write succeeded.' in text for kind, _, text in client.events if kind in {'send','edit'})
    assert out.read_text() == 'bronze'
