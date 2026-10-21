from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from talos import proof
from talos.eventlog import Event, EventLog


def _events(home: Path) -> list[dict]:
    return [
        {"id": 1, "ts": 10.0, "run_id": "run-1", "type": "reason.started", "payload": {}},
        {"id": 2, "ts": 11.0, "run_id": "run-1", "type": "exec.intent", "payload": {
            "tool": "write_file", "verdict": "allow", "targets": [str(home / "secret-name.md")],
            "args": {"content": "PRIVATE-MARKER"},
        }},
        {"id": 3, "ts": 12.0, "run_id": "run-1", "type": "snapshot.taken", "payload": {
            "tool": "write_file", "snapshot_id": "snap-1",
            "entries": [[str(home / "secret-name.md"), None]],
        }},
        {"id": 4, "ts": 13.0, "run_id": "run-1", "type": "exec.result", "payload": {
            "tool": "write_file", "status": "done", "detail": "PRIVATE-RESULT",
        }},
        {"id": 5, "ts": 16.0, "run_id": "run-1", "type": "reason.done", "payload": {
            "status": "answered", "text": "PRIVATE-ANSWER",
        }},
    ]


def test_summary_is_evidence_only_and_redacts_home(tmp_path: Path) -> None:
    summary = proof.summarize_events(_events(tmp_path), home=tmp_path)

    assert summary["run_id"] == "run-1"
    assert summary["status"] == "answered"
    assert summary["duration_s"] == 6.0
    assert summary["tool_calls"] == 1
    assert summary["successful_calls"] == 1
    assert summary["artifacts"] == ["~/secret-name.md"]
    assert summary["tools"] == [{"tool": "write_file", "calls": 1, "successful": 1,
                                  "failed": 0, "last_status": "done"}]
    exported = json.dumps(summary)
    assert "PRIVATE-MARKER" not in exported
    assert "PRIVATE-RESULT" not in exported
    assert "PRIVATE-ANSWER" not in exported


def test_failed_write_never_claims_an_artifact(tmp_path: Path) -> None:
    events = _events(tmp_path)
    events[3]["payload"]["status"] = "error"

    summary = proof.summarize_events(events, home=tmp_path)

    assert summary["artifacts"] == []
    assert summary["successful_calls"] == 0
    assert summary["failed_calls"] == 1
    assert summary["tools"][0]["failed"] == 1


def test_collect_latest_and_cli_formats_are_read_only(tmp_path: Path, capsys) -> None:
    db = tmp_path / "eventlog.db"
    log = EventLog(db)
    for entry in _events(tmp_path):
        log.append(Event(entry["run_id"], "test", entry["type"], entry["payload"]), now=entry["ts"])
    log.close()
    before = db.stat().st_mtime_ns

    summaries = proof.collect(db)
    json_code = proof.run_proof(["--format", "json"], db=db)
    output = capsys.readouterr().out

    assert len(summaries) == 1
    assert json_code == 0
    assert json.loads(output)[0]["run_id"] == "run-1"
    assert db.stat().st_mtime_ns == before


def test_markdown_names_its_evidence_boundary(tmp_path: Path) -> None:
    rendered = proof.render_markdown([proof.summarize_events(_events(tmp_path), home=tmp_path)])

    assert "# Talos outcome proof" in rendered
    assert "Event-log evidence only" in rendered
    assert "write_file" in rendered
    assert "~/secret-name.md" in rendered
    assert "PRIVATE" not in rendered


@pytest.mark.parametrize("kind", ["regular", "hardlink", "symlink"])
def test_export_refuses_existing_targets_without_mutation(tmp_path: Path, capsys, kind) -> None:
    original = tmp_path / "original"
    original.write_text("operator-owned state")
    original.chmod(0o644)
    target = tmp_path / "proof.md"
    if kind == "hardlink":
        os.link(original, target)
    elif kind == "symlink":
        target.symlink_to(original)
    else:
        target.write_text("existing report")
    before = target.read_bytes()

    assert proof.run_proof(["--out", str(target)], db=tmp_path / "absent.db") == 2
    assert target.read_bytes() == before
    assert original.read_text() == "operator-owned state"
    assert stat.S_IMODE(original.stat().st_mode) == 0o644
    assert "written to" not in capsys.readouterr().out


def test_export_creates_private_new_file(tmp_path: Path, capsys) -> None:
    target = tmp_path / "proof.md"
    assert proof.run_proof(["--out", str(target)], db=tmp_path / "absent.db") == 0
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert "No matching run evidence" in target.read_text()
    assert "mode 600" in capsys.readouterr().out
