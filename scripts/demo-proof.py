#!/usr/bin/env python3
"""Replay small, existing regression fixtures; never present them as live model runs."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "files": (
        "File read/write and the receipt binding an action to its grant",
        (
            "tests/test_tools.py::test_read_file_returns_content",
            "tests/test_tools.py::test_write_file_creates_and_writes",
            "tests/test_executor.py::test_allowed_write_runs_and_returns_result",
            "tests/test_executor.py::test_receipt_names_the_grant_that_authorised_the_run",
        ),
    ),
    "approval": (
        "Explicit task approval, no carryover to the next task, one-use capabilities",
        (
            "tests/test_executor.py::test_irreversible_needs_human_and_does_not_run",
            "tests/test_capability.py::test_needs_human_without_approval_yields_no_token",
            "tests/test_capability.py::test_token_is_single_use",
            "tests/test_task_approval_flow.py::test_task_approval_runs_different_actions_until_completion",
            "tests/test_task_approval_flow.py::test_once_then_task_keeps_the_original_task_id",
        ),
    ),
    "recovery": (
        "A failed fixture write rolls back; edits to audit records are detected",
        (
            "tests/test_executor.py::test_runner_exception_rolls_back",
            "tests/test_executor.py::test_verify_failed_rolls_back",
            "tests/test_eventlog.py::test_a_fresh_chain_verifies",
            "tests/test_eventlog.py::test_editing_an_event_names_its_id",
        ),
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("all", *CASES), default="all")
    args = parser.parse_args()
    print("Fixture demonstrations: real Talos components, controlled test runners.\n"
          "No model calls. Not a live conversation or performance benchmark.", flush=True)
    selected = CASES if args.case == "all" else {args.case: CASES[args.case]}
    for name, (description, nodes) in selected.items():
        print(f"\n{name}: {description}", flush=True)
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *nodes], cwd=ROOT, check=False,
        )
        if result.returncode:
            print(f"Fixture group {name} failed; no success claim.", file=sys.stderr)
            return result.returncode
    print("Selected fixture groups passed. Model-backed recipes still need a real run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
