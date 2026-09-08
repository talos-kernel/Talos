import threading

from talos.telegram import QueueNotice
from talos.worker import Worker


def test_late_queued_send_is_edited_to_actual_terminal_state():
    entered, release = threading.Event(), threading.Event()
    shown = []
    class Client:
        def send_message(self, chat, text):
            entered.set(); assert release.wait(2)
            shown.append(text); return 41
        def edit_message_text(self, chat, message_id, text):
            assert message_id == 41
            shown.append(text)
    notice = QueueNotice(Client(), 1, interval=0)
    notice.update('queued'); assert entered.wait(2)
    notice.update('running'); notice.update('failed')
    notice.update('queued')  # delayed callback must not revive the turn
    release.set(); notice._thread.join(2)
    assert shown[-1] == QueueNotice.TEXT['failed']
    assert not notice._thread.is_alive()


def test_worker_finalizes_success_failure_and_drained_queue():
    entered, release = threading.Event(), threading.Event()
    first, second = object(), object()
    events = {id(first): [], id(second): []}
    def handle(item):
        entered.set(); assert release.wait(2)
        return False
    worker = Worker(handle); worker.start()
    try:
        assert worker.submit(first); assert entered.wait(2)
        worker.watch(first, events[id(first)].append)
        assert worker.submit(second)
        worker.watch(second, events[id(second)].append)
        assert worker.drain() == 1
        release.set()
    finally:
        release.set(); worker.stop()
    assert events[id(first)][-1] == 'failed'
    assert events[id(second)][-1] == 'cancelled'
    assert not worker.busy() and worker.pending() == 0


def test_completed_fast_turn_is_not_reported_as_still_waiting():
    item = object(); done = threading.Event()
    worker = Worker(lambda _: done.set()); worker.start()
    assert worker.submit(item); assert done.wait(2)
    worker.stop()
    states = []; worker.watch(item, states.append)
    assert states == ['ended']
