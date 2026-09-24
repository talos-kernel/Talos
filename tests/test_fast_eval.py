"""The local fast evaluator is deterministic, bounded and network-free."""
from __future__ import annotations

from talos.fast_eval import FastEvalEngine, MAX_INPUT_CHARS


def test_fast_eval_classifies_without_a_provider():
    reply = FastEvalEngine().evaluate("Can the service be restarted?")

    assert "Fast evaluation (local)" in reply
    assert "Category: question (99%)" in reply
    assert "no external call" in reply


def test_fast_eval_classifies_instructions_and_opinions():
    engine = FastEvalEngine()

    assert "Category: instruction" in engine.evaluate("Please check the service")
    assert "Category: opinion" in engine.evaluate("In my opinion this is safer")


def test_fast_eval_is_bounded_and_does_not_claim_truth():
    reply = FastEvalEngine().evaluate("x" * (MAX_INPUT_CHARS + 100))

    assert "Soundness: not established" in reply
    assert "no external call" in reply


def test_fast_eval_empty_text_explains_usage():
    assert FastEvalEngine().evaluate(" ") == "Usage: /eval <statement or text>"


def test_fast_eval_exposes_structured_metadata_for_shared_preflight():
    result = FastEvalEngine().classify("Please check the service")

    assert result.category == "instruction"
    assert result.confidence == 0.91
    assert result.duration_s >= 0
