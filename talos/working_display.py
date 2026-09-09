"""Keep temporary channel displays together across approval continuations.

Keys come from the conductor, never model text. This owns display handles only:
no messages from the operator, receipts, permissions or tool execution.
"""
from collections import OrderedDict
import threading


class WorkingDisplays:
    def __init__(self) -> None:
        self._items = OrderedDict()
        self._lock = threading.Lock()
        self._cleanup_slots = threading.BoundedSemaphore(4)

    def add(self, key: tuple[str, str, str], *handles: object) -> None:
        with self._lock:
            items = self._items.setdefault(key, [])
            items.extend(h for h in handles if callable(getattr(h, 'cleanup', None)))
            self._items.move_to_end(key)
            # Expired approvals must not keep display objects forever. Forgetting
            # an old handle does not delete chat content without a delivered result.
            while len(self._items) > 256:
                self._items.popitem(last=False)

    def cleanup(self, key: tuple[str, str, str]) -> None:
        with self._lock:
            items = self._items.pop(key, ())
        if not items or not self._cleanup_slots.acquire(blocking=False):
            return  # Under a channel outage, keep the trail rather than queue work.
        def remove():
            try:
                for handle in items:
                    try:
                        handle.cleanup()
                    except Exception:
                        pass
            finally:
                self._cleanup_slots.release()
        # Deleting old UI must not keep the worker/queue busy after its result.
        threading.Thread(target=remove, name='talos-ui-cleanup', daemon=True).start()
