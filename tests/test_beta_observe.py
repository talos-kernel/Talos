"""The observation collector must be read-only and discard private details."""
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "beta-observe.py"
SPEC = importlib.util.spec_from_file_location("beta_observe", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def _runner(*, broken=False, restarts=0, anchor_ok=True):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["capture_output"] is True
        assert kwargs["timeout"] == 60
        if "health" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "status": "ok", "newest_error": "SECRET_PRIVATE_MESSAGE",
                "event_log": {"events_total": 200, "errors_24h": 2, "runs_24h": 3,
                              "newest_error": "SECRET_PRIVATE_MESSAGE"},
                "chain": {"chain_ok": True, "chain_broken_id": None, "chained": 180},
                "anchor": {"verify_ok": anchor_ok},
                "schedules": {"available": True, "pending": 1},
            }), "")
        if "verify" in argv:
            return subprocess.CompletedProcess(argv, 1 if broken else 0,
                                               "SECRET_PRIVATE_MESSAGE", "")
        if "version" in argv:
            return subprocess.CompletedProcess(argv, 0, "0.19.23-alpha\n", "")
        return subprocess.CompletedProcess(argv, 0,
                                           f"ActiveState=active\nNRestarts={restarts}\n"
                                           "ActiveEnterTimestamp=Fri 2026-09-25 00:01:00 CEST\n", "")

    return run, calls


def test_observation_keeps_only_whitelisted_metrics(tmp_path):
    run, calls = _runner()
    sample = module.collect(tmp_path, runner=run)
    target = tmp_path / "private" / "observations.jsonl"
    module.append_record(target, sample)
    stored = target.read_text()
    assert sample["ok"] is True
    assert sample["events_chained"] == 180
    assert sample["service_restarts"] == 0
    assert "SECRET_PRIVATE_MESSAGE" not in stored
    assert len(calls) == 4
    assert os.stat(target).st_mode & 0o777 == 0o600
    assert os.stat(target.parent).st_mode & 0o777 == 0o700


def test_failed_verify_is_recorded_without_raw_output(tmp_path):
    run, _ = _runner(broken=True)
    sample = module.collect(tmp_path, runner=run)
    assert sample["ok"] is False
    assert sample["verify_ok"] is False
    assert "SECRET_PRIVATE_MESSAGE" not in json.dumps(sample)


@pytest.mark.parametrize("options", [{"restarts": 1}, {"anchor_ok": False}])
def test_restart_or_failed_anchor_needs_attention(tmp_path, options):
    run, _ = _runner(**options)
    assert module.collect(tmp_path, runner=run)["ok"] is False


def test_output_symlink_is_rejected(tmp_path):
    real = tmp_path / "real"
    real.write_text("untouched")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(OSError):
        module.append_record(link, {"ok": True})
    assert real.read_text() == "untouched"
