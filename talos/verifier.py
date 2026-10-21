"""Verifier — TOCTOU-Bindung vor Ausführung + Ergebnisprüfung danach.

Vorbild ist das verbreitete Exec-Approval-Binding: der Zustand einer
Zieldatei wird zum Entscheidungszeitpunkt als realpath + sha256 gebunden und **unmittelbar vor
dem Ausführen erneut geprüft**. Weicht er ab, wird fail-closed abgebrochen — so kann zwischen
Freigabe und Spawn nichts untergeschoben werden.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

_ABSENT = "absent"


def _digest(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        return _ABSENT
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


@dataclass(frozen=True)
class Binding:
    original: str
    realpath: str
    sha256: str
    parent_dev: int | None
    parent_ino: int | None


def _binding(target: str) -> Binding:
    realpath = os.path.realpath(target)
    try:
        parent = os.stat(os.path.dirname(realpath) or os.curdir)
        parent_dev: int | None = parent.st_dev
        parent_ino: int | None = parent.st_ino
    except FileNotFoundError:
        parent_dev = parent_ino = None
    return Binding(target, realpath, _digest(target), parent_dev, parent_ino)


def bind(targets: tuple[str, ...]) -> tuple[Binding, ...]:
    """Bindet jeden Zielpfad an (realpath, sha256) zum Entscheidungszeitpunkt."""
    return tuple(_binding(t) for t in targets)


def recheck(bindings: tuple[Binding, ...]) -> bool:
    """True nur, wenn JEDE Bindung unverändert ist. Sonst fail-closed."""
    for b in bindings:
        if os.path.realpath(b.original) != b.realpath:
            return False
        if _digest(b.original) != b.sha256:
            return False
        try:
            parent = os.stat(os.path.dirname(b.realpath) or os.curdir)
        except FileNotFoundError:
            # ``None`` is itself a bound state: an absent parent must not appear
            # between policy evaluation and execution.
            if b.parent_dev is not None:
                return False
        else:
            if b.parent_dev is None:
                return False
            if (parent.st_dev, parent.st_ino) != (b.parent_dev, b.parent_ino):
                return False
    return True


def verify_result(expected: object, actual: object) -> bool:
    """Deterministischer Erwartet-vs-Tatsächlich-Vergleich."""
    return expected == actual
