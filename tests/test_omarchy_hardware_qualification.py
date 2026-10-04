from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import omarchy_hardware_qualification as qualification


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
PNG_HEADER = b"\x89PNG\r\n\x1a\n"
BASELINE_PNG = PNG_HEADER + b"baseline"
CLEANUP_PNG = PNG_HEADER + b"cleanup"
RETURN_PNG = PNG_HEADER + b"return-ocr"
DEFAULT_MARKER = "TQMARKER0000000001"


def expected_inputs(marker: str) -> tuple[list[dict], list[dict]]:
    trial = [
        {"op": "click", "x": 130, "y": 13, "button": 1},
        {"op": "key", "keys": "super+Return"},
        {"op": "click", "x": 320, "y": 420, "button": 1},
        {"op": "type", "text": "echo " + marker},
        {"op": "key", "keys": "Return"},
    ]
    cleanup = [{"op": "key", "keys": "super+W"}]
    return trial, [*trial, *cleanup]


def valid_result(marker: str = DEFAULT_MARKER) -> dict:
    trial_inputs, all_inputs = expected_inputs(marker)
    dispatch = {"input": "dispatched", "control": "human"}
    typed = trial_inputs[-2]
    returned = trial_inputs[-1]
    cleanup_hash = hashlib.sha256(CLEANUP_PNG).hexdigest()
    return {
        "ok": True,
        "phase": "complete",
        "console_errors": [],
        "http_errors": [],
        "request_failures": [],
        "token_removed_from_url": True,
        "operator_input_without_agent_job": True,
        "mobile_no_overflow": True,
        "visible_marker_ocr": True,
        "terminal_cleanup": True,
        "return_request_count": 1,
        "final_control": "paused",
        "final_vm": "paused",
        "baseline_marker_count": 0,
        "marker_count_before": 1,
        "visible_changed_pixels": 151,
        "terminal_open_changed_pixels": 50001,
        "type_latency_ms": 20,
        "return_latency_ms": 21,
        "final_job_count": 0,
        "initial_job_hash": DIGEST_A,
        "final_job_hash": DIGEST_A,
        "baseline_sha256": hashlib.sha256(BASELINE_PNG).hexdigest(),
        "return_ocr_frame_sha256": hashlib.sha256(RETURN_PNG).hexdigest(),
        "trial_input_requests": trial_inputs,
        "input_requests": all_inputs,
        "type_transport": {"request": typed, "status": 200, "body": dispatch},
        "return_transport": {"request": returned, "status": 200, "body": dispatch},
        "cleanup_samples": [
            {"evidence": "05-cleanup-1.png", "marker_count": 0,
             "changed_pixels": 0, "sha256": cleanup_hash},
            {"evidence": "05-cleanup-2.png", "marker_count": 0,
             "changed_pixels": 1, "sha256": cleanup_hash},
        ],
    }


def write_e2e_evidence(evidence: Path, marker: str = DEFAULT_MARKER,
                       result: dict | None = None) -> dict:
    evidence.mkdir(parents=True)
    (evidence / "00-empty-workspace.png").write_bytes(BASELINE_PNG)
    (evidence / "02-ocr-frame.png").write_bytes(RETURN_PNG)
    (evidence / "05-cleanup-1.png").write_bytes(CLEANUP_PNG)
    (evidence / "05-cleanup-2.png").write_bytes(CLEANUP_PNG)
    value = valid_result(marker) if result is None else result
    cleanup_inputs = value["input_requests"][-1:]
    dispatch = {"input": "dispatched", "control": "human"}
    transports = {
        "type-transport.json": value["type_transport"],
        "return-transport.json": value["return_transport"],
        "cleanup-transport.json": [
            {"request": request, "status": 200, "body": dispatch}
            for request in cleanup_inputs
        ],
    }
    for name, transport in transports.items():
        (evidence / name).write_text(
            json.dumps(transport, sort_keys=True) + "\n", encoding="utf-8")
    (evidence / "result.json").write_text(
        json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return value


def binding() -> dict:
    return {
        "source": {"head": "1" * 40, "tree": "2" * 40, "clean": True},
        "runtime": {
            "target": "/installed/runtime/current",
            "release": "/installed/runtime/generation",
            "release_name": "generation",
            "backend_tree_sha256": "3" * 64,
        },
        "install_evidence": {
            "path": "/installed/install-evidence.json",
            "sha256": "4" * 64,
            "release": "generation",
        },
        "harness": {"path": "tests/omarchy_installed_e2e.py", "sha256": "5" * 64},
        "host": {"model": "Mac15,7", "macos": "15.7", "arch": "arm64"},
        "artifacts": {"app_sha256": DIGEST_A, "archive_sha256": DIGEST_B},
    }


def processes(seed: int = 0) -> dict:
    return {
        role: {
            "label": f"org.talos.omarchy.{role}",
            "pid": seed + index,
            "process_started_utc": f"Sat Oct  4 10:00:0{index} 2026",
        }
        for index, role in enumerate(("web", "api", "vm"), start=1)
    }


def configuration(tmp_path: Path, *, label: str = "generation-1", count: int = 2,
                  chain_name: str = "chain") -> qualification.Configuration:
    repository = tmp_path / "repository"
    profile = tmp_path / "profile"
    repository.mkdir(exist_ok=True)
    profile.mkdir(exist_ok=True)
    return qualification.Configuration(
        profile=profile,
        chain_root=tmp_path / chain_name,
        generation_label=label,
        app_sha256=DIGEST_A,
        archive_sha256=DIGEST_B,
        count=count,
        repository=repository,
    )


def install_generation_mocks(monkeypatch, *, process_seed: int = 0,
                             process_values: dict | None = None,
                             fail_ordinal: int | None = None):
    calls = []
    markers = iter(f"TQMARKER{index:010d}" for index in range(1, 100))
    monkeypatch.setattr(qualification.os, "geteuid", lambda: 501)
    monkeypatch.setattr(qualification, "collect_static_binding", lambda _config: binding())
    observed_processes = process_values or processes(process_seed)
    monkeypatch.setattr(qualification, "launchd_processes", lambda: observed_processes)
    monkeypatch.setattr(qualification, "make_marker", lambda: next(markers))

    def invoke(_config, evidence, marker):
        ordinal = len(calls) + 1
        calls.append((evidence, marker))
        result = valid_result(marker)
        if ordinal == fail_ordinal:
            result["ok"] = False
        write_e2e_evidence(evidence, marker, result)
        return 9 if ordinal == fail_ordinal else 0

    monkeypatch.setattr(qualification, "invoke_e2e", invoke)
    return calls


def test_launchd_process_identity_uses_only_read_only_commands(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == "/bin/launchctl":
            role = argv[-1].rsplit(".", 1)[-1]
            pid = {"web": 101, "api": 102, "vm": 103}[role]
            return SimpleNamespace(returncode=0, stdout=f"state = running\npid = {pid}\n")
        return SimpleNamespace(returncode=0, stdout="Sat Oct  4 10:11:12 2026\n")

    monkeypatch.setattr(qualification.subprocess, "run", run)
    observed = qualification.launchd_processes()

    assert observed["vm"]["pid"] == 103
    assert observed["vm"]["process_started_utc"] == "Sat Oct  4 10:11:12 2026"
    commands = [argv for argv, _kwargs in calls]
    assert all(command[:2] in (["/bin/launchctl", "print"], ["/bin/ps", "-p"])
               for command in commands)
    assert not any("kickstart" in command or "bootout" in command
                   for command in commands)
    ps_environments = [kwargs["env"] for argv, kwargs in calls if argv[0] == "/bin/ps"]
    assert ps_environments == [
        {"LC_ALL": "C", "LANG": "C", "TZ": "UTC"},
        {"LC_ALL": "C", "LANG": "C", "TZ": "UTC"},
        {"LC_ALL": "C", "LANG": "C", "TZ": "UTC"},
    ]


def test_result_validation_rejects_a_claim_without_independent_cleanup_proof(
        tmp_path):
    evidence = tmp_path / "e2e"
    result = valid_result()
    write_e2e_evidence(evidence, result=result)
    qualification.validate_result(result, DEFAULT_MARKER, evidence)
    result["cleanup_samples"][1]["marker_count"] = 1

    with pytest.raises(qualification.QualificationError, match="cleanup proof"):
        qualification.validate_result(result, DEFAULT_MARKER, evidence)


def test_result_validation_allows_distinct_authenticated_cleanup_frames(tmp_path):
    evidence = tmp_path / "e2e"
    result = valid_result()
    second = PNG_HEADER + b"different-cleanup"
    result["cleanup_samples"][1]["sha256"] = hashlib.sha256(second).hexdigest()
    write_e2e_evidence(evidence, result=result)
    (evidence / "05-cleanup-2.png").write_bytes(second)

    qualification.validate_result(result, DEFAULT_MARKER, evidence)


@pytest.mark.parametrize(
    ("artifact", "message"),
    [
        ("00-empty-workspace.png", "baseline PNG hash"),
        ("02-ocr-frame.png", "return OCR frame hash"),
        ("05-cleanup-1.png", "cleanup PNG hash"),
        ("05-cleanup-2.png", "cleanup PNG hash"),
    ],
)
def test_result_validation_hashes_referenced_pngs(tmp_path, artifact, message):
    evidence = tmp_path / "e2e"
    result = write_e2e_evidence(evidence)
    (evidence / artifact).write_bytes(PNG_HEADER + b"tampered")

    with pytest.raises(qualification.QualificationError, match=message):
        qualification.validate_result(result, DEFAULT_MARKER, evidence)


def test_result_validation_refuses_symlinked_evidence(tmp_path):
    evidence = tmp_path / "e2e"
    result = write_e2e_evidence(evidence)
    external = tmp_path / "external.png"
    external.write_bytes(BASELINE_PNG)
    (evidence / "00-empty-workspace.png").unlink()
    (evidence / "00-empty-workspace.png").symlink_to(external)

    with pytest.raises(qualification.QualificationError, match="symlink"):
        qualification.validate_result(result, DEFAULT_MARKER, evidence)


def test_result_validation_rejects_extra_return_input(tmp_path):
    evidence = tmp_path / "e2e"
    result = valid_result()
    result["trial_input_requests"].append({"op": "key", "keys": "Return"})
    result["input_requests"].insert(-2, {"op": "key", "keys": "Return"})
    write_e2e_evidence(evidence, result=result)

    with pytest.raises(qualification.QualificationError, match="trial input"):
        qualification.validate_result(result, DEFAULT_MARKER, evidence)


def test_atomic_publication_never_clobbers_a_racing_destination(tmp_path, monkeypatch):
    target = tmp_path / "record.json"
    real_link = qualification.os.link

    def racing_link(source, destination, **kwargs):
        Path(destination).write_bytes(b"racing writer\n")
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(qualification.os, "link", racing_link)
    with pytest.raises(qualification.QualificationError, match="refusing to replace"):
        qualification.atomic_create_json(target, {"ours": True})

    assert target.read_bytes() == b"racing writer\n"
    interrupted = list(tmp_path.glob(".record.json.*.tmp"))
    assert len(interrupted) == 1
    assert json.loads(interrupted[0].read_text(encoding="utf-8")) == {"ours": True}


def test_marker_uses_eighty_random_bits_and_stays_keyboard_safe(monkeypatch):
    requested = []

    def token_bytes(size):
        requested.append(size)
        return bytes(range(size))

    monkeypatch.setattr(qualification.secrets, "token_bytes", token_bytes)
    marker = qualification.make_marker()

    assert requested == [10]
    assert marker.startswith("tq")
    assert len(marker) == 18
    assert marker.isalnum() and marker == marker.lower()
    assert len("echo " + marker) * 2 <= 63


def test_parent_cleanup_forces_pause_and_requires_independent_readback(monkeypatch,
                                                                       tmp_path):
    calls = []
    responses = iter((
        {"control": "paused", "vm": "paused"},
        {"control": "paused", "vm": "paused"},
    ))

    def control(profile, kind, args, *, timeout=5.0):
        calls.append((profile, kind, args, timeout))
        return next(responses)

    monkeypatch.setattr(qualification, "installed_control", control)
    profile = tmp_path / "profile"

    final = qualification.prove_installed_paused(profile)

    assert final == {"control": "paused", "vm": "paused"}
    assert calls == [
        (profile, "action", {"op": "pause"}, 5.0),
        (profile, "read", {"op": "status"}, 5.0),
    ]


def test_parent_cleanup_repauses_when_first_readback_observes_takeover(monkeypatch,
                                                                       tmp_path):
    calls = []
    responses = iter((
        {"control": "paused", "vm": "paused"},
        {"control": "human", "vm": "running"},
        {"control": "paused", "vm": "paused"},
        {"control": "paused", "vm": "paused"},
    ))

    def control(profile, kind, args, *, timeout=5.0):
        calls.append((kind, args))
        return next(responses)

    monkeypatch.setattr(qualification, "installed_control", control)

    assert qualification.prove_installed_paused(tmp_path / "profile") == {
        "control": "paused", "vm": "paused"}
    assert calls == [
        ("action", {"op": "pause"}),
        ("read", {"op": "status"}),
        ("action", {"op": "pause"}),
        ("read", {"op": "status"}),
    ]


def test_trial_timeout_runs_parent_pause_proof_before_failing(monkeypatch, tmp_path):
    config = configuration(tmp_path)
    calls = []

    def run(*args, **kwargs):
        raise qualification.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(qualification.subprocess, "run", run)
    monkeypatch.setattr(
        qualification, "prove_installed_paused",
        lambda profile: calls.append(profile) or {
            "control": "paused", "vm": "paused"})

    with pytest.raises(qualification.QualificationError,
                       match="parent proved paused/paused"):
        qualification.invoke_e2e(config, tmp_path / "evidence", DEFAULT_MARKER)

    assert calls == [config.profile]


def test_generation_records_bound_result_hashes_and_unique_markers(tmp_path, monkeypatch):
    calls = install_generation_mocks(monkeypatch)
    config = configuration(tmp_path)

    summary = qualification.run_generation(config)

    assert summary["status"] == "passed"
    assert summary["completed_trials"] == 2
    assert summary["cumulative_consecutive_runs"] == 2
    assert len({trial["marker"] for trial in summary["trials"]}) == 2
    assert len(calls) == 2
    evidence_root = config.chain_root / summary["evidence_root"]
    for trial in summary["trials"]:
        result = evidence_root / trial["evidence"] / "result.json"
        assert trial["result_json_sha256"] == hashlib.sha256(result.read_bytes()).hexdigest()
        assert trial["binding_sha256"] == summary["binding_sha256"]
    assert (evidence_root / "generation-start.json").is_file()
    assert (evidence_root / "generation-summary.json").is_file()
    assert all(path.stat().st_mode & 0o222 == 0
               for path in evidence_root.rglob("*") if path.is_file())
    _manifest, entries, head = qualification.load_chain(config.chain_root)
    assert len(entries) == 1
    assert entries[0]["evidence_root"] == summary["evidence_root"]
    assert entries[0]["entry_sha256"] == head
    assert (config.chain_root / "entries/000001.json").stat().st_mode & 0o222 == 0


def test_first_failure_aborts_preserves_evidence_and_atomically_resets(tmp_path, monkeypatch):
    calls = install_generation_mocks(monkeypatch, fail_ordinal=2)
    config = configuration(tmp_path, count=4)

    summary = qualification.run_generation(config)

    assert summary["status"] == "failed"
    assert summary["reset"] is True
    assert summary["completed_trials"] == 1
    assert summary["cumulative_consecutive_runs"] == 0
    assert summary["failure"]["trial_ordinal"] == 2
    assert "result_json_sha256" in summary["failure"]
    assert len(calls) == 2
    evidence_root = config.chain_root / summary["evidence_root"]
    assert (evidence_root / "trial-001/qualification.json").is_file()
    assert (evidence_root / "trial-002/e2e/result.json").is_file()
    assert (evidence_root / "trial-002/failure.json").is_file()
    assert not (evidence_root / "trial-003").exists()
    assert not list(evidence_root.rglob("*.tmp"))
    _manifest, entries, _head = qualification.load_chain(config.chain_root)
    assert [entry["status"] for entry in entries] == ["failed"]

    install_generation_mocks(monkeypatch, process_seed=2000)
    with pytest.raises(qualification.QualificationError, match="new empty chain root"):
        qualification.run_generation(configuration(tmp_path, label="generation-2"))
    assert (evidence_root / "trial-002/failure.json").is_file()
    assert not (config.chain_root / "entries/000002.json").exists()

    fresh = configuration(
        tmp_path, label="generation-2", chain_name="replacement-chain")
    replacement = qualification.run_generation(fresh)
    assert replacement["status"] == "passed"
    assert replacement["chain_entry_number"] == 1


def test_chain_current_head_tampering_is_refused(tmp_path, monkeypatch):
    install_generation_mocks(monkeypatch, process_seed=1000)
    config = configuration(tmp_path)
    qualification.run_generation(config)
    entry_path = config.chain_root / "entries/000001.json"
    entry = json.loads(entry_path.read_text(encoding="utf-8"))
    entry["previous_entry_sha256"] = "0" * 64
    entry_path.chmod(0o600)
    entry_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    with pytest.raises(qualification.QualificationError, match="chain entry"):
        qualification.load_chain(config.chain_root)


def test_each_role_process_identity_must_be_fresh(tmp_path, monkeypatch):
    first_processes = processes(1000)
    install_generation_mocks(monkeypatch, process_values=first_processes)
    first = configuration(tmp_path, label="generation-1")
    assert qualification.run_generation(first)["status"] == "passed"

    second_processes = processes(2000)
    install_generation_mocks(monkeypatch, process_values=second_processes)
    second = configuration(tmp_path, label="generation-2")
    assert qualification.run_generation(second)["status"] == "passed"

    third_processes = processes(3000)
    third_processes["web"] = first_processes["web"]
    install_generation_mocks(monkeypatch, process_values=third_processes)
    third = configuration(tmp_path, label="generation-3")
    failed = qualification.run_generation(third)

    assert failed["status"] == "failed"
    assert "web PID and process start identity are not fresh" in failed["failure"]["error"]
    _manifest, entries, _head = qualification.load_chain(third.chain_root)
    assert [entry["status"] for entry in entries] == ["passed", "passed", "failed"]


def test_three_fresh_generations_chain_to_sixty_consecutive_runs(tmp_path, monkeypatch):
    final = None
    for generation in range(1, 4):
        install_generation_mocks(monkeypatch, process_seed=generation * 1000)
        config = configuration(tmp_path, label=f"generation-{generation}", count=20)
        final = qualification.run_generation(config)

    assert final is not None
    assert final["status"] == "passed"
    assert final["generation_count"] == 3
    assert final["cumulative_consecutive_runs"] == 60
    assert final["distinct_generation_labels"] is True
    assert final["distinct_process_generations"] is True
    assert final["fresh_role_process_identities"] is True
    assert final["three_generation_sixty_run_qualified"] is True
    _manifest, entries, head = qualification.load_chain(config.chain_root)
    assert [entry["entry_number"] for entry in entries] == [1, 2, 3]
    assert entries[1]["previous_entry_sha256"] == entries[0]["entry_sha256"]
    assert entries[2]["previous_entry_sha256"] == entries[1]["entry_sha256"]
    assert head == entries[2]["entry_sha256"]
