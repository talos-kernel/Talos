"""Explicit human consent for one foreground task, with no time-based expiry.

This is broader than an exact-action standing rule: it covers later NEEDS_HUMAN
actions in the same task. It never changes the kernel verdict or a capability.
Only the conductor can carry the task ID; model arguments cannot select a grant.
State is process-local and is not restored into a different task after restart.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Callable

from .channel import Trust
from .eventlog import Event
from .executor import Executor, Outcome, Status
from .policy import ToolRequest

TASK_WORDS = frozenset({"allow this task"})
TASK_NOTICE = (
    "▶ Allow this task: automatically approve all approval-required actions in this "
    "task until it ends or you stop it; no time limit. Later commands and targets "
    "are included. Kernel denials still apply. New tasks need their own approval."
)


def is_task_approval(text: str) -> bool:
    return text.strip().lower().rstrip("!.") in TASK_WORDS


@dataclass(frozen=True)
class TaskConsent:
    principal: str
    conversation: str
    approved: bool = False


class TaskApprovals:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskConsent] = {}
        self._lock = threading.Lock()

    def open(self, task_id: str, principal: object, conversation: str) -> None:
        with self._lock:
            # Only one foreground task per owner/chat. This also removes abandoned,
            # expired *pending* decisions without expiring a running approved task.
            self._tasks = {key: value for key, value in self._tasks.items()
                           if (value.principal, value.conversation) != (str(principal), conversation)}
            self._tasks[task_id] = TaskConsent(str(principal), conversation)

    def approve(self, task_id: str, principal: object, conversation: str) -> bool:
        with self._lock:
            value = self._tasks.get(task_id)
            if value is None or (value.principal, value.conversation) != (str(principal), conversation):
                return False
            self._tasks[task_id] = TaskConsent(value.principal, value.conversation, True)
            return True

    def state(self, task_id: str, principal: object, conversation: str) -> bool | None:
        with self._lock:
            value = self._tasks.get(task_id)
            if value is None or (value.principal, value.conversation) != (str(principal), conversation):
                return None
            return value.approved

    def finish(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)

    def cancel(self, principal: object, conversation: str) -> None:
        with self._lock:
            self._tasks = {key: value for key, value in self._tasks.items()
                           if (value.principal, value.conversation) != (str(principal), conversation)}

    def clear(self) -> None:
        with self._lock:
            self._tasks.clear()


@dataclass(frozen=True)
class TaskExecutor:
    """Pass the owner's decision into the existing kernel/token pipeline per call.

Not installed on the shared executor: delegates, schedules, distillation and other
conversations never inherit it. Cancellation is checked before each new action;
an already admitted action remains subject to the existing cancellation backend.
"""

    executor: Executor
    approvals: TaskApprovals
    task_id: str
    principal: object
    conversation: str
    trust: Callable[[], Trust]

    @property
    def log(self):
        return self.executor.log

    def run(self, req: ToolRequest, run_id: str) -> Outcome:
        approved = self.approvals.state(self.task_id, self.principal, self.conversation)
        if approved is None or str(req.identity) != str(self.principal) or self.trust() is not Trust.FULL:
            self.log.append(Event(run_id, "policy", "approval.task_refused",
                                  {"task_id": self.task_id, "tool": req.tool}))
            return Outcome(Status.DENIED, "Task approval ended or does not belong to this request.")
        if approved:
            self.log.append(Event(run_id, "human", "approval.task_used",
                                  {"task_id": self.task_id, "tool": req.tool}))
        return self.executor.run(req, run_id, human_approved=approved)
