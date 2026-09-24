"""A tiny local evaluator for fast, reproducible Talos checks.

Nibbles' speed comes from a specialised evaluation path: a small bounded input,
no conversation history, no tools, two narrow questions, and a compact result.
Talos uses the same shape locally, but deliberately keeps the first engine
deterministic so `/eval` is free, networkless and stable in E2E tests.

This is classification, not truth and not permission. The policy kernel remains
the only authority for effects.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

MAX_INPUT_CHARS = 6_000

_QUESTION_PREFIX = re.compile(
    r"^(?:who|what|when|where|why|how|is|are|am|can|could|do|does|did|which|wie|was|wann|wo|warum|wie|ist|sind|kann|könnte)\b",
    re.IGNORECASE,
)
_INSTRUCTION_PREFIX = re.compile(
    r"^(?:please\s+|bitte\s+|run\b|check\b|inspect\b|read\b|write\b|change\b|add\b|remove\b|deploy\b|test\b|prüf|prüfe|lies\b|schreib\b|ändere\b|deploye\b|teste\b)",
    re.IGNORECASE,
)
_OPINION_MARKER = re.compile(
    r"\b(?:i think|in my opinion|i believe|ich denke|meiner meinung|ich finde|should be|sollte)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FastEvalResult:
    category: str
    confidence: float
    duration_s: float

    def render(self) -> str:
        return (
            "Fast evaluation (local)\n\n"
            f"Category: {self.category} ({self.confidence:.0%})\n"
            "Soundness: not established by the local classifier\n\n"
            f"Measured: {self.duration_s:.4f}s · 0 tokens · no external call"
        )


class FastEvalEngine:
    """Bounded local classifier with no provider, key or network dependency."""

    def evaluate(self, text: str) -> str:
        statement = text.strip()[:MAX_INPUT_CHARS]
        if not statement:
            return "Usage: /eval <statement or text>"
        result = self.classify(statement)
        return result.render()

    def classify(self, text: str) -> FastEvalResult:
        """Return metadata for the shared preflight without exposing user text.

        The result is a routing/observability hint only. Callers must not use it to
        grant permissions, skip the policy kernel, or replace the configured reasoner.
        """
        statement = text.strip()[:MAX_INPUT_CHARS]
        if not statement:
            raise ValueError("cannot classify empty text")
        started = time.perf_counter()
        category, confidence = self._classify(statement)
        return FastEvalResult(category, confidence, time.perf_counter() - started)

    @staticmethod
    def _classify(statement: str) -> tuple[str, float]:
        if statement.endswith("?") or _QUESTION_PREFIX.match(statement):
            return "question", 0.99 if statement.endswith("?") else 0.93
        if _INSTRUCTION_PREFIX.match(statement):
            return "instruction", 0.91
        if _OPINION_MARKER.search(statement):
            return "opinion", 0.95
        return "fact", 0.60


__all__ = ["FastEvalEngine", "FastEvalResult", "MAX_INPUT_CHARS"]
