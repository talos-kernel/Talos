"""Ein-Slot-Worker — damit der Poll-Loop frei bleibt.

Warum es das gibt: der Reasoner ist ein blockierender Subprozess mit bis zu 180 s
Timeout. Solange er lief, rief niemand `get_updates` auf — des Betreibers `/stop` wurde nicht
einmal *abgeholt*, geschweige denn verarbeitet. Ein Abbruch-Kommando, das erst nach
dem Ende des Laufs ankommt, ist keins.

Also: Denken läuft hier, in genau einem Hintergrund-Thread. Kommandos und Freigaben
bleiben im Poll-Thread und überholen die Warteschlange. Ein Thread, nicht mehr —
zwei parallele Läufe würden sich an denselben Dateien und an derselben Ein-Chat-
Freigabe in die Quere kommen.

Die Warteschlange ist absichtlich klein und begrenzt: lieber sichtbar ablehnen als
still 200 Nachrichten aufstauen, die the operator längst vergessen hat.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable

MAX_QUEUE = 8

_STOP = object()  # Sentinel: beendet den Thread von innen, ohne Flag-Polling


class Worker:
    """Serialisiert Aufgaben in einem Thread. Alle Methoden sind von außen aufrufbar."""

    def __init__(
        self,
        handle: Callable[[object], object],
        *,
        max_queue: int = MAX_QUEUE,
        on_error: Callable[[BaseException], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._handle = handle
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._busy = threading.Event()
        self._busy_since = 0.0
        self._clock = clock
        self._on_error = on_error
        self._thread = threading.Thread(target=self._loop, name="talos-worker", daemon=True)
        self._state_lock = threading.RLock()
        self._states: dict[int, str] = {}
        self._items: dict[int, object] = {}
        self._listeners: dict[int, Callable[[str], None]] = {}
        self._current: object | None = None

    def start(self) -> None:
        self._thread.start()

    def submit(self, item: object) -> bool:
        """False, wenn die Warteschlange voll ist — der Aufrufer sagt the operator dann Bescheid."""
        try:
            self._transition(item, "queued")
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            self._transition(item, "refused")
            return False

    def watch(self, item: object, listener: Callable[[str], None]) -> None:
        with self._state_lock:
            self._listeners[id(item)] = listener
            state = self._states.get(id(item), "ended")
        listener(state)

    def _transition(self, item: object, state: str) -> None:
        with self._state_lock:
            key = id(item)
            self._items[key] = item
            if self._states.get(key) == "cancelled":
                state = "cancelled"
            self._states[key] = state
            listener = self._listeners.get(key)
            # Keep a small terminal history for a notice registered after a fast turn.
            terminal = {"ended", "failed", "cancelled", "refused"}
            for old in list(self._states):
                if len(self._states) <= MAX_QUEUE + 32:
                    break
                if self._states[old] in terminal and old != key:
                    self._states.pop(old, None); self._listeners.pop(old, None); self._items.pop(old, None)
        if listener is not None:
            try:
                listener(state)  # listeners only enqueue display updates; no network IO
            except Exception:
                pass

    def mark_cancelled(self) -> None:
        if self._current is not None:
            self._transition(self._current, "cancelled")

    def pending(self) -> int:
        return self._queue.qsize()

    def busy(self) -> bool:
        return self._busy.is_set()

    def busy_since(self) -> float | None:
        """Wann der laufende Auftrag begann — oder None, wenn gerade keiner laeuft.

        Gibt es, damit der Kanal sagen kann „laeuft seit 42 s", ohne es zu schaetzen.
        Ein Wartehinweis ohne gemessene Zahl waere entweder nichtssagend („bin
        beschaeftigt") oder erfunden — und erfundene Zahlen sind hier verboten.
        """
        return self._busy_since if self._busy.is_set() else None

    def drain(self) -> int:
        """Verwirft alles Wartende. Der bereits laufende Job bleibt unberührt —
        den beendet der Reasoner-Abbruch, nicht die Queue."""
        dropped = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return dropped
            self._queue.task_done()
            self._transition(item, "cancelled")
            dropped += 1

    def stop(self, timeout: float = 5.0) -> None:
        self._queue.put(_STOP)
        self._thread.join(timeout)

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                self._queue.task_done()
                return
            self._busy_since = self._clock()
            self._busy.set()
            self._current = item
            self._transition(item, "running")
            state = "ended"
            try:
                if self._handle(item) is False:
                    state = "failed"
            except Exception as error:  # ein kaputter Lauf darf den Worker nicht killen
                state = "failed"
                if self._on_error is not None:
                    self._on_error(error)
            finally:
                self._busy.clear()
                self._current = None
                self._transition(item, state)
                self._queue.task_done()
