"""Cancellation context shared by a run and its synchronous delegated calls.

This carries only a stop predicate, never permission. Each child thread captures
the predicate explicitly; unrelated foreground/background runs cannot replace it.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable

_stop: ContextVar[Callable[[], bool]] = ContextVar('talos_run_stop', default=lambda: False)
_reasoner: ContextVar[object | None] = ContextVar('talos_run_reasoner', default=None)


def current_stop() -> Callable[[], bool]:
    return _stop.get()


def current_reasoner() -> object | None:
    return _reasoner.get()


@contextmanager
def active(stop: Callable[[], bool], *, reasoner: object | None = None):
    token = _stop.set(stop)
    model_token = _reasoner.set(reasoner)
    try:
        yield
    finally:
        _stop.reset(token)
        _reasoner.reset(model_token)
