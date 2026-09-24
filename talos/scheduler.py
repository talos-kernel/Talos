"""Service-owned schedule dispatch. CLI sessions never consume calendar slots."""
import threading
import time

from .channel import Inbound, Principal, Trust
from .eventlog import Event, new_run_id


def dispatch_due(*, schedules, registry, allowed_principals, unattended,
                 continuity, conductor, log, now=None):
    for task in schedules.due(now=now):
        try:
            principal = Principal.parse(task.principal)
            # An absent/delivery-only channel cannot own this task. Do not consume
            # its slot, and do not loosen the conductor's independent trust check.
            if registry.trust_of(principal.channel) is Trust.NOTIFY:
                continue
            # A heartbeat beats only while nothing else runs: a beat that falls into a
            # running task is dropped, not queued. Its slot stays unclaimed (mark_run is
            # inside the lock), so it fires at the next idle pass. Holding the lock across
            # the run keeps a foreground task from starting inside the beat; the conductor
            # re-enters the same RLock on this thread.
            lock = conductor.execution_lock if task.heartbeat else None
            if lock is not None and not lock.acquire(blocking=False):
                log.append(Event(new_run_id(), "schedule", "heartbeat.busy",
                                 {"id": task.id}))
                continue
            try:
                if not schedules.mark_run(task.id, now=now, expected_next_run=task.next_run):
                    continue
                if principal not in allowed_principals:
                    log.append(Event(new_run_id(), "schedule", "schedule.refused",
                                     {"id": task.id,
                                      "reason": "principal no longer allowed"}))
                    continue
                with unattended.active():
                    ready = continuity.prepare(task, principal, run_id=new_run_id())
                    if ready is None:
                        continue
                    update = Inbound(principal=principal, conversation=task.conversation,
                                     text=ready.text,
                                     dedup_key=f"schedule:{task.id}:{task.next_run}")
                    log.append(Event(new_run_id(), "schedule", "schedule.fired", {"id": task.id}))
                    conductor.handle(update, before_reply=ready.before_reply)
            finally:
                if lock is not None:
                    lock.release()
        except Exception:
            # No prompt, output, command or exception text in scheduler metadata.
            log.append(Event(new_run_id(), "schedule", "schedule.error",
                             {"id": task.id, "reason": "scheduled execution failed"}))


def start_scheduler(*, service_mode, interval_s=5, **dependencies):
    if not service_mode or not dependencies['schedules'].available:
        return None

    def tick():
        while True:
            time.sleep(interval_s)
            try:
                dispatch_due(**dependencies)
            except Exception:
                dependencies['log'].append(Event(new_run_id(), "schedule", "schedule.error",
                                                   {"reason": "scheduler tick failed"}))

    thread = threading.Thread(target=tick, daemon=True, name="talos-schedules")
    thread.start()
    return thread
