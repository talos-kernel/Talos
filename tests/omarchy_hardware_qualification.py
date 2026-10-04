#!/usr/bin/env python3
"""Run repeatable, unprivileged Omarchy installed-hardware qualification.

This orchestrator only reads host/install identity and invokes
``omarchy_installed_e2e.py``.  It deliberately has no service-control or
administrator path.  One explicit chain root owns numbered append-only
generation records and their evidence.  The final summary is created atomically
once.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import socket
import stat
import subprocess
import sys
from typing import Any
import uuid


REPOSITORY = Path(__file__).resolve().parents[1]
HARNESS_RELATIVE = Path("tests/omarchy_installed_e2e.py")
DEFAULT_RUNTIME = Path("/Library/Application Support/TalosOmarchy/runtime/current")
DEFAULT_INSTALL_EVIDENCE = Path(
    "/Library/Application Support/TalosOmarchy/install-evidence.json")
LAUNCHD_LABELS = {
    "web": "org.talos.omarchy.web",
    "api": "org.talos.omarchy.api",
    "vm": "org.talos.omarchy.vm",
}
SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
GENERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
SCHEMA = "talos.omarchy.hardware-qualification/v2"
CHAIN_ROOT_SCHEMA = "talos.omarchy.hardware-qualification-chain-root/v1"
CHAIN_ENTRY_SCHEMA = "talos.omarchy.hardware-qualification-chain-entry/v1"
CHAIN_ROOT_RECORD = "chain-root.json"
CHAIN_ENTRIES_DIRECTORY = "entries"
CHAIN_EVIDENCE_DIRECTORY = "generations"
CONTROL_RESPONSE_LIMIT = 1024 * 1024


class QualificationError(RuntimeError):
    """A qualification invariant failed without exposing command output."""


@dataclass(frozen=True)
class Configuration:
    profile: Path
    chain_root: Path
    generation_label: str
    app_sha256: str
    archive_sha256: str
    count: int = 20
    browser: Path | None = None
    trial_timeout: int = 180
    repository: Path = REPOSITORY
    runtime_target: Path = DEFAULT_RUNTIME
    install_evidence: Path = DEFAULT_INSTALL_EVIDENCE


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z")


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def regular_file_bytes(path: Path) -> bytes:
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            raise QualificationError(f"required evidence path is a symlink: {path}")
    except OSError as error:
        raise QualificationError(f"required evidence file is unavailable: {path}") from error
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise QualificationError(f"required evidence file is unavailable: {path}") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise QualificationError(
                f"required evidence path is not a regular file: {path}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    except OSError as error:
        raise QualificationError(f"required evidence file cannot be read: {path}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def sha256_file(path: Path) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise QualificationError(f"required evidence file is unavailable: {path}") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise QualificationError(
                f"required evidence path is not a regular file: {path}")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise QualificationError(f"required evidence file cannot be read: {path}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return digest.hexdigest()


def tree_sha256(root: Path) -> str:
    """Hash names, kinds, executable modes, symlink targets, and file bytes."""
    try:
        root_metadata = root.lstat()
    except OSError as error:
        raise QualificationError(f"installed backend tree is unavailable: {root}") from error
    if not stat.S_ISDIR(root_metadata.st_mode) or root.is_symlink():
        raise QualificationError("installed backend tree is not a real directory")
    digest = hashlib.sha256()
    for item in sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix()):
        relative = item.relative_to(root).as_posix().encode("utf-8")
        metadata = item.lstat()
        mode = stat.S_IMODE(metadata.st_mode) & 0o111
        if stat.S_ISDIR(metadata.st_mode):
            kind, payload = b"directory", b""
        elif stat.S_ISLNK(metadata.st_mode):
            kind = b"symlink"
            payload = os.readlink(item).encode("utf-8")
        elif stat.S_ISREG(metadata.st_mode):
            kind = b"file"
            payload = bytes.fromhex(sha256_file(item))
        else:
            raise QualificationError(
                f"unsupported entry in installed backend tree: {item}")
        for value in (kind, relative, f"{mode:o}".encode(), payload):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest()


def strict_json_bytes(raw: bytes, description: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite number {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key}")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), parse_constant=reject_constant,
                          object_pairs_hook=unique_object)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise QualificationError(f"{description} is not strict JSON") from error


def atomic_create_json(path: Path, value: Any) -> None:
    """Create one JSON record atomically; never replace existing evidence."""
    if path.exists() or path.is_symlink():
        raise QualificationError(f"refusing to replace evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # A same-filesystem hard link publishes the complete inode and fails
            # atomically if another writer won the destination-name race.
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as error:
            raise QualificationError(f"refusing to replace evidence: {path}") from error
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        # Remove staging only after the published name is durable. Any earlier
        # failure retains the temp inode as interruption evidence; a failure
        # here retains both names.
        temporary.unlink()
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        # A leftover temp file is evidence of interruption; do not remove it.
        raise


def command_output(argv: list[str], *, cwd: Path | None = None,
                   timeout: int = 10,
                   environment: dict[str, str] | None = None) -> str:
    try:
        completed = subprocess.run(
            argv, cwd=cwd, check=False, capture_output=True, text=True,
            timeout=timeout, env=environment)
    except (OSError, subprocess.SubprocessError) as error:
        raise QualificationError(f"read-only identity command failed: {argv[0]}") from error
    if completed.returncode != 0:
        raise QualificationError(f"read-only identity command failed: {argv[0]}")
    return completed.stdout.strip()


def git_identity(repository: Path) -> dict[str, Any]:
    repository = repository.resolve(strict=True)
    status = command_output(
        ["/usr/bin/git", "status", "--porcelain=v1", "--untracked-files=all", "-z"],
        cwd=repository)
    if status:
        raise QualificationError("source repository is not a clean immutable HEAD")
    head = command_output(["/usr/bin/git", "rev-parse", "--verify", "HEAD"],
                          cwd=repository)
    tree = command_output(["/usr/bin/git", "rev-parse", "--verify", "HEAD^{tree}"],
                          cwd=repository)
    if not re.fullmatch(r"[0-9a-f]{40,64}", head) or not re.fullmatch(
            r"[0-9a-f]{40,64}", tree):
        raise QualificationError("git returned an invalid HEAD or tree identity")
    return {"head": head, "tree": tree, "clean": True}


def host_identity() -> dict[str, str]:
    model = command_output(["/usr/sbin/sysctl", "-n", "hw.model"])
    macos = command_output(["/usr/bin/sw_vers", "-productVersion"])
    arch = platform.machine().strip()
    if not model or not macos or not arch:
        raise QualificationError("host model, macOS version, or architecture is unavailable")
    # Deliberately do not call system_profiler or ioreg: no host serial belongs
    # in qualification evidence.
    return {"model": model, "macos": macos, "arch": arch}


def runtime_identity(target: Path) -> dict[str, str]:
    if not target.is_symlink():
        raise QualificationError("installed runtime/current is not an immutable release symlink")
    try:
        release = target.resolve(strict=True)
    except OSError as error:
        raise QualificationError("installed runtime target cannot be resolved") from error
    backend = release / "backend/talos"
    return {
        "target": str(target),
        "release": str(release),
        "release_name": release.name,
        "backend_tree_sha256": tree_sha256(backend),
    }


def install_identity(path: Path, runtime: dict[str, str]) -> dict[str, str]:
    raw = regular_file_bytes(path)
    raw_hash = hashlib.sha256(raw).hexdigest()
    parsed = strict_json_bytes(raw, "install evidence")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("release"), str):
        raise QualificationError("install evidence has no release identity")
    if parsed["release"] != runtime["release_name"]:
        raise QualificationError("install evidence release does not match runtime/current")
    return {"path": str(path), "sha256": raw_hash, "release": parsed["release"]}


def collect_static_binding(config: Configuration) -> dict[str, Any]:
    source = git_identity(config.repository)
    harness = (config.repository / HARNESS_RELATIVE).resolve(strict=True)
    runtime = runtime_identity(config.runtime_target)
    return {
        "source": source,
        "runtime": runtime,
        "install_evidence": install_identity(config.install_evidence, runtime),
        "harness": {
            "path": HARNESS_RELATIVE.as_posix(),
            "sha256": sha256_file(harness),
        },
        "host": host_identity(),
        "artifacts": {
            "app_sha256": config.app_sha256,
            "archive_sha256": config.archive_sha256,
        },
    }


def launchd_processes() -> dict[str, dict[str, Any]]:
    processes: dict[str, dict[str, Any]] = {}
    for role, label in LAUNCHD_LABELS.items():
        output = command_output(["/bin/launchctl", "print", "system/" + label])
        pid_match = re.search(r"^\s*pid = ([1-9][0-9]*)\s*$", output, re.MULTILINE)
        state_match = re.search(r"^\s*state = ([A-Za-z]+)\s*$", output, re.MULTILINE)
        if not pid_match or not state_match or state_match.group(1) != "running":
            raise QualificationError(f"{role} launchd process is not running with a PID")
        pid = int(pid_match.group(1))
        # BSD ps' lstart is otherwise locale- and timezone-dependent.  A
        # minimal fixed environment gives the process one canonical identity
        # without inheriting operator secrets.
        started = command_output(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="],
            environment={"LC_ALL": "C", "LANG": "C", "TZ": "UTC"})
        if not started or "\n" in started:
            raise QualificationError(f"{role} process start identity is unavailable")
        processes[role] = {
            "label": label,
            "pid": pid,
            "process_started_utc": started,
        }
    return processes


def make_marker() -> str:
    # Lowercase preserves 80 random bits while keeping ``echo <marker>`` below
    # the guest's 63-event keyboard report cap (uppercase needs Shift events).
    entropy = base64.b32encode(secrets.token_bytes(10)).decode("ascii").rstrip("=")
    return "tq" + entropy.lower()


def artifact_bytes(evidence: Path, name: str, *, png: bool = False) -> bytes:
    raw = regular_file_bytes(evidence / name)
    if png and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise QualificationError(f"E2E evidence is not a PNG: {name}")
    return raw


def validate_result(result: Any, marker: str, evidence: Path) -> None:
    if not isinstance(result, dict):
        raise QualificationError("E2E result root is not an object")
    try:
        evidence_mode = evidence.lstat().st_mode
    except OSError as error:
        raise QualificationError("E2E evidence directory is unavailable") from error
    if not stat.S_ISDIR(evidence_mode) or stat.S_ISLNK(evidence_mode):
        raise QualificationError("E2E evidence directory is not a real directory")
    exact = {
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
        "final_control": "paused",
        "final_vm": "paused",
    }
    for key, expected in exact.items():
        if key not in result or result[key] != expected or (
                isinstance(expected, bool) and result[key] is not expected):
            raise QualificationError(f"E2E result success field is invalid: {key}")
    for key, minimum, maximum in (
            ("visible_changed_pixels", 150, None),
            ("terminal_open_changed_pixels", 50000, None),
            ("type_latency_ms", 0, 5000),
            ("return_latency_ms", 0, 15000),
            ("final_job_count", 0, None)):
        value = result.get(key)
        if (type(value) is not int or value < minimum
                or (maximum is not None and value > maximum)):
            raise QualificationError(f"E2E result numeric field is invalid: {key}")
    for key, expected in (("return_request_count", 1),
                          ("baseline_marker_count", 0),
                          ("marker_count_before", 1)):
        if type(result.get(key)) is not int or result[key] != expected:
            raise QualificationError(f"E2E result count field is invalid: {key}")
    initial_hash, final_hash = result.get("initial_job_hash"), result.get("final_job_hash")
    if (not isinstance(initial_hash, str) or not SHA256.fullmatch(initial_hash)
            or final_hash != initial_hash):
        raise QualificationError("E2E result durable job hashes are invalid")
    cleanup = result.get("cleanup_samples")
    if not isinstance(cleanup, list) or len(cleanup) != 2:
        raise QualificationError("E2E result cleanup sample shape is invalid")
    expected_cleanup_names = ["05-cleanup-1.png", "05-cleanup-2.png"]
    for sample, expected_name in zip(cleanup, expected_cleanup_names):
        if (not isinstance(sample, dict)
                or sample.get("evidence") != expected_name
                or sample.get("marker_count") != 0
                or type(sample.get("changed_pixels")) is not int
                or sample["changed_pixels"] >= 5000
                or not isinstance(sample.get("sha256"), str)
                or not SHA256.fullmatch(sample["sha256"])):
            raise QualificationError("E2E result cleanup proof is invalid")
        actual_hash = hashlib.sha256(
            artifact_bytes(evidence, expected_name, png=True)).hexdigest()
        if sample["sha256"] != actual_hash:
            raise QualificationError("E2E cleanup PNG hash does not match its evidence")

    baseline_hash = result.get("baseline_sha256")
    if (not isinstance(baseline_hash, str) or not SHA256.fullmatch(baseline_hash)
            or baseline_hash != hashlib.sha256(artifact_bytes(
                evidence, "00-empty-workspace.png", png=True)).hexdigest()):
        raise QualificationError("E2E baseline PNG hash does not match its evidence")
    return_hash = result.get("return_ocr_frame_sha256")
    if (not isinstance(return_hash, str) or not SHA256.fullmatch(return_hash)
            or return_hash != hashlib.sha256(artifact_bytes(
                evidence, "02-ocr-frame.png", png=True)).hexdigest()):
        raise QualificationError("E2E return OCR frame hash does not match its evidence")

    setup = [
        {"op": "click", "x": 130, "y": 13, "button": 1},
        {"op": "key", "keys": "super+Return"},
        {"op": "click", "x": 320, "y": 420, "button": 1},
    ]
    typed = {"op": "type", "text": "echo " + marker}
    returned = {"op": "key", "keys": "Return"}
    cleanup_inputs = [{"op": "key", "keys": "super+W"}]
    trial_inputs = [*setup, typed, returned]
    all_inputs = [*trial_inputs, *cleanup_inputs]
    if result.get("trial_input_requests") != trial_inputs:
        raise QualificationError("E2E trial input request sequence is invalid")
    if result.get("input_requests") != all_inputs:
        raise QualificationError("E2E complete input request sequence is invalid")

    dispatch = {"input": "dispatched", "control": "human"}
    expected_type_transport = {"request": typed, "status": 200, "body": dispatch}
    expected_return_transport = {"request": returned, "status": 200, "body": dispatch}
    expected_cleanup_transport = [
        {"request": request, "status": 200, "body": dispatch}
        for request in cleanup_inputs
    ]
    if result.get("type_transport") != expected_type_transport:
        raise QualificationError("E2E type transport receipt is invalid")
    if result.get("return_transport") != expected_return_transport:
        raise QualificationError("E2E Return transport receipt is invalid")
    for name, expected in (
            ("type-transport.json", expected_type_transport),
            ("return-transport.json", expected_return_transport),
            ("cleanup-transport.json", expected_cleanup_transport)):
        actual = strict_json_bytes(artifact_bytes(evidence, name), name)
        if actual != expected:
            raise QualificationError(f"E2E transport evidence is invalid: {name}")


def read_result(path: Path, marker: str, evidence: Path) -> tuple[dict[str, Any], str]:
    raw = regular_file_bytes(path)
    digest = hashlib.sha256(raw).hexdigest()
    result = strict_json_bytes(raw, "E2E result.json")
    validate_result(result, marker, evidence)
    return result, digest


def installed_control(profile: Path, kind: str, args: dict[str, Any],
                      *, timeout: float = 5.0) -> dict[str, Any]:
    """Call the installed local control socket without exposing its owner secret."""
    env_path = profile / "talos.env"
    try:
        metadata = env_path.stat(follow_symlinks=False)
    except OSError as error:
        raise QualificationError("installed control profile is unavailable") from error
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077):
        raise QualificationError("installed control profile is not private")
    values: dict[str, str] = {}
    for raw_line in regular_file_bytes(env_path).decode("utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    endpoint = values.get("TALOS_COMPUTER_SOCKET", "")
    owner = values.get("TALOS_COMPUTER_OWNER_SHA256", "")
    if not os.path.isabs(endpoint) or not SHA256.fullmatch(owner):
        raise QualificationError("installed control profile is incomplete")
    frame = json.dumps(
        {"kind": kind, "owner": owner, "args": args},
        separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(timeout)
            channel.connect(endpoint)
            channel.sendall(frame)
            response = channel.makefile("rb").readline(CONTROL_RESPONSE_LIMIT + 1)
    except OSError as error:
        raise QualificationError("installed control socket did not answer") from error
    if (len(response) > CONTROL_RESPONSE_LIMIT or not response.endswith(b"\n")):
        raise QualificationError("installed control socket returned an incomplete receipt")
    value = strict_json_bytes(response, "installed control receipt")
    if not isinstance(value, dict) or value.get("error"):
        raise QualificationError("installed control socket refused fail-closed recovery")
    return value


def prove_installed_paused(profile: Path) -> dict[str, Any]:
    """Independently force and read back the installed paused/paused state."""
    final: dict[str, Any] = {}
    for _attempt in range(2):
        installed_control(profile, "action", {"op": "pause"})
        final = installed_control(profile, "read", {"op": "status"})
        if final.get("control") == "paused" and final.get("vm") == "paused":
            return final
    raise QualificationError(
        "parent cleanup could not prove control=paused and vm=paused")


def invoke_e2e(config: Configuration, evidence: Path, marker: str) -> int:
    argv = [
        sys.executable, str(config.repository / HARNESS_RELATIVE),
        "--profile", str(config.profile),
        "--evidence-dir", str(evidence),
        "--marker", marker,
    ]
    if config.browser is not None:
        argv.extend(("--browser", str(config.browser)))
    try:
        completed = subprocess.run(
            argv, check=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=config.trial_timeout)
    except subprocess.TimeoutExpired as error:
        try:
            prove_installed_paused(config.profile)
        except QualificationError as cleanup_error:
            raise QualificationError(
                "installed E2E timed out and parent cleanup could not prove paused/paused"
            ) from cleanup_error
        raise QualificationError(
            "installed E2E exceeded its trial timeout; parent proved paused/paused"
        ) from error
    except OSError as error:
        raise QualificationError("installed E2E could not be invoked") from error
    try:
        prove_installed_paused(config.profile)
    except QualificationError as error:
        raise QualificationError(
            "installed E2E ended without parent proof of paused/paused") from error
    return completed.returncode


def cohort_fingerprint(binding: dict[str, Any]) -> str:
    return canonical_hash(binding)


def process_fingerprint(processes: dict[str, dict[str, Any]]) -> str:
    return canonical_hash(processes)


def process_identity(processes: dict[str, dict[str, Any]], role: str) -> tuple[int, str]:
    process = processes.get(role)
    if (not isinstance(process, dict)
            or process.get("label") != LAUNCHD_LABELS[role]
            or type(process.get("pid")) is not int or process["pid"] <= 0
            or not isinstance(process.get("process_started_utc"), str)
            or not process["process_started_utc"]
            or "\n" in process["process_started_utc"]):
        raise QualificationError(f"{role} process identity is invalid")
    return process["pid"], process["process_started_utc"]


def validate_processes(processes: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(processes, dict) or set(processes) != set(LAUNCHD_LABELS):
        raise QualificationError("generation process roles are invalid")
    for role in LAUNCHD_LABELS:
        process_identity(processes, role)
    return processes


def require_real_directory(path: Path, description: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise QualificationError(f"{description} is unavailable") from error
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        raise QualificationError(f"{description} is not a real directory")


def initialize_chain_root(root: Path) -> None:
    if root.exists() or root.is_symlink():
        require_real_directory(root, "qualification chain root")
        if any(root.iterdir()):
            return
    else:
        root.mkdir(parents=True, mode=0o700, exist_ok=False)
    (root / CHAIN_ENTRIES_DIRECTORY).mkdir(mode=0o700)
    (root / CHAIN_EVIDENCE_DIRECTORY).mkdir(mode=0o700)
    manifest = {
        "schema": CHAIN_ROOT_SCHEMA,
        "chain_id": uuid.uuid4().hex,
        "created_at": utc_now(),
        "entries_directory": CHAIN_ENTRIES_DIRECTORY,
        "evidence_directory": CHAIN_EVIDENCE_DIRECTORY,
    }
    atomic_create_json(root / CHAIN_ROOT_RECORD, manifest)
    os.chmod(root / CHAIN_ROOT_RECORD, 0o400)


def entry_body(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if key != "entry_sha256"}


def assert_fresh_role_processes(
        current: dict[str, dict[str, Any]],
        prior_entries: list[dict[str, Any]]) -> None:
    validate_processes(current)
    for role in LAUNCHD_LABELS:
        identity = process_identity(current, role)
        for entry in prior_entries:
            prior = entry.get("processes")
            if prior is not None and identity == process_identity(prior, role):
                raise QualificationError(
                    f"{role} PID and process start identity are not fresh")


def load_chain(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
    """Validate the entire authoritative chain and return its current head."""
    require_real_directory(root, "qualification chain root")
    manifest = strict_json_bytes(
        regular_file_bytes(root / CHAIN_ROOT_RECORD), "qualification chain root")
    if (not isinstance(manifest, dict) or manifest.get("schema") != CHAIN_ROOT_SCHEMA
            or not re.fullmatch(r"[0-9a-f]{32}", str(manifest.get("chain_id", "")))
            or manifest.get("entries_directory") != CHAIN_ENTRIES_DIRECTORY
            or manifest.get("evidence_directory") != CHAIN_EVIDENCE_DIRECTORY):
        raise QualificationError("qualification chain root record is invalid")
    entries_directory = root / CHAIN_ENTRIES_DIRECTORY
    evidence_directory = root / CHAIN_EVIDENCE_DIRECTORY
    require_real_directory(entries_directory, "qualification chain entries directory")
    require_real_directory(evidence_directory, "qualification evidence directory")
    entry_paths = sorted(entries_directory.iterdir(), key=lambda path: path.name)
    entries: list[dict[str, Any]] = []
    head: str | None = None
    referenced_evidence: set[str] = set()
    labels: set[str] = set()
    binding_hash: str | None = None
    for number, path in enumerate(entry_paths, start=1):
        expected_name = f"{number:06d}.json"
        if path.name != expected_name:
            raise QualificationError("qualification chain entries are not contiguous")
        entry = strict_json_bytes(regular_file_bytes(path), "qualification chain entry")
        if (not isinstance(entry, dict) or entry.get("schema") != CHAIN_ENTRY_SCHEMA
                or entry.get("chain_id") != manifest["chain_id"]
                or entry.get("entry_number") != number
                or entry.get("previous_entry_sha256") != head
                or entry.get("status") not in {"passed", "failed"}
                or not GENERATION.fullmatch(str(entry.get("generation_label", "")))
                or type(entry.get("completed_trials")) is not int
                or entry["completed_trials"] < 0
                or not isinstance(entry.get("entry_sha256"), str)
                or not SHA256.fullmatch(entry["entry_sha256"])
                or entry["entry_sha256"] != canonical_hash(entry_body(entry))):
            raise QualificationError("qualification chain entry is invalid")
        if entry["generation_label"] in labels:
            raise QualificationError("generation label is already present in the chain")
        labels.add(entry["generation_label"])
        if entry["status"] == "failed" and number != len(entry_paths):
            raise QualificationError("a failed qualification chain entry is not terminal")
        relative = entry.get("evidence_root")
        expected_relative = (
            f"{CHAIN_EVIDENCE_DIRECTORY}/{number:06d}-{entry['generation_label']}")
        if relative != expected_relative or relative in referenced_evidence:
            raise QualificationError("qualification evidence root binding is invalid")
        referenced_evidence.add(relative)
        generation_root = root / relative
        require_real_directory(generation_root, "generation evidence root")
        summary_raw = regular_file_bytes(generation_root / "generation-summary.json")
        summary = strict_json_bytes(summary_raw, "generation summary")
        if (not isinstance(summary, dict) or summary.get("schema") != SCHEMA
                or summary.get("chain_id") != manifest["chain_id"]
                or summary.get("chain_entry_number") != number
                or summary.get("previous_chain_head_sha256") != head
                or summary.get("generation_label") != entry["generation_label"]
                or summary.get("status") != entry["status"]
                or summary.get("completed_trials") != entry["completed_trials"]
                or summary.get("binding_sha256") != entry.get("binding_sha256")
                or summary.get("generation_processes") != entry.get("processes")
                or summary.get("evidence_root") != relative
                or not isinstance(summary.get("trials"), list)
                or len(summary["trials"]) != entry["completed_trials"]
                or hashlib.sha256(summary_raw).hexdigest()
                != entry.get("generation_summary_sha256")
                or tree_sha256(generation_root) != entry.get("evidence_tree_sha256")):
            raise QualificationError("generation evidence does not match its chain entry")
        processes = entry.get("processes")
        process_hash = entry.get("process_generation_sha256")
        if ((processes is None) != (process_hash is None)
                or (processes is not None
                    and process_fingerprint(validate_processes(processes)) != process_hash)):
            raise QualificationError("qualification chain process identity is invalid")
        if entry["status"] == "passed":
            if (entry["completed_trials"] <= 0
                    or not isinstance(entry.get("binding_sha256"), str)
                    or not SHA256.fullmatch(entry["binding_sha256"])
                    or not isinstance(entry.get("process_generation_sha256"), str)
                    or not SHA256.fullmatch(entry["process_generation_sha256"])
                    or summary.get("requested_trials") != entry["completed_trials"]):
                raise QualificationError("passed qualification chain entry is invalid")
            if binding_hash is None:
                binding_hash = entry["binding_sha256"]
            elif entry["binding_sha256"] != binding_hash:
                raise QualificationError("qualification chain mixes evidence bindings")
            assert_fresh_role_processes(entry["processes"], entries)
        prior_completed = sum(item["completed_trials"] for item in entries)
        expected_cumulative = (
            prior_completed + entry["completed_trials"]
            if entry["status"] == "passed" else 0)
        expected_qualified = bool(
            entry["status"] == "passed" and expected_cumulative >= 60 and number >= 3)
        if (summary.get("reset") is not (entry["status"] == "failed")
                or summary.get("cumulative_consecutive_runs") != expected_cumulative
                or summary.get("generation_count") != number
                or summary.get("distinct_generation_labels") is not True
                or summary.get("distinct_process_generations")
                is not (entry["status"] == "passed")
                or summary.get("fresh_role_process_identities")
                is not (entry["status"] == "passed")
                or summary.get("three_generation_sixty_run_qualified")
                is not expected_qualified
                or (entry["status"] == "passed" and summary.get("failure") is not None)
                or (entry["status"] == "failed"
                    and not isinstance(summary.get("failure"), dict))
                or ((summary.get("binding") is None)
                    != (entry.get("binding_sha256") is None))
                or (summary.get("binding") is not None
                    and cohort_fingerprint(summary["binding"])
                    != entry.get("binding_sha256"))):
            raise QualificationError("generation summary chain totals are invalid")
        head = entry["entry_sha256"]
        entries.append(entry)
    actual_evidence = set()
    for path in evidence_directory.iterdir():
        require_real_directory(path, "generation evidence root")
        actual_evidence.add(f"{CHAIN_EVIDENCE_DIRECTORY}/{path.name}")
    if actual_evidence != referenced_evidence:
        raise QualificationError("qualification chain has orphaned or missing evidence roots")
    return manifest, entries, head


def seal_generation_files(root: Path) -> None:
    """Make every completed evidence file read-only against accidental writes."""
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise QualificationError("generation evidence contains a symlink")
        if stat.S_ISREG(mode):
            os.chmod(path, 0o400)


def append_chain_entry(
        *, root: Path, manifest: dict[str, Any], entries: list[dict[str, Any]],
        previous_head: str | None, summary: dict[str, Any],
        evidence_root: Path) -> dict[str, Any]:
    number = len(entries) + 1
    summary_path = evidence_root / "generation-summary.json"
    body = {
        "schema": CHAIN_ENTRY_SCHEMA,
        "chain_id": manifest["chain_id"],
        "entry_number": number,
        "previous_entry_sha256": previous_head,
        "generation_label": summary["generation_label"],
        "status": summary["status"],
        "completed_trials": summary["completed_trials"],
        "binding_sha256": summary["binding_sha256"],
        "process_generation_sha256": (
            process_fingerprint(summary["generation_processes"])
            if summary["generation_processes"] is not None else None),
        "processes": summary["generation_processes"],
        "evidence_root": summary["evidence_root"],
        "generation_summary_sha256": sha256_file(summary_path),
        "evidence_tree_sha256": tree_sha256(evidence_root),
    }
    entry = {**body, "entry_sha256": canonical_hash(body)}
    path = root / CHAIN_ENTRIES_DIRECTORY / f"{number:06d}.json"
    atomic_create_json(path, entry)
    os.chmod(path, 0o400)
    return entry


def validate_configuration(config: Configuration) -> Configuration:
    if os.geteuid() == 0:
        raise QualificationError("qualification must run as an unprivileged operator")
    if not GENERATION.fullmatch(config.generation_label):
        raise QualificationError("generation label must be 1-64 safe characters")
    if type(config.count) is not int or not 1 <= config.count <= 999:
        raise QualificationError("trial count must be between 1 and 999")
    if type(config.trial_timeout) is not int or config.trial_timeout < 30:
        raise QualificationError("trial timeout must be at least 30 seconds")
    if not SHA256.fullmatch(config.app_sha256) or not SHA256.fullmatch(
            config.archive_sha256):
        raise QualificationError("app and archive SHA-256 arguments must be explicit digests")
    profile = config.profile.resolve(strict=True)
    repository = config.repository.resolve(strict=True)
    if config.chain_root.is_symlink():
        raise QualificationError("chain root must not be a symlink")
    chain_root = config.chain_root.resolve()
    if chain_root == repository or repository in chain_root.parents:
        raise QualificationError("chain root must be outside the source repository")
    return Configuration(
        profile=profile, chain_root=chain_root,
        generation_label=config.generation_label,
        app_sha256=config.app_sha256.lower(),
        archive_sha256=config.archive_sha256.lower(), count=config.count,
        browser=(config.browser.resolve(strict=True) if config.browser else None),
        trial_timeout=config.trial_timeout, repository=repository,
        runtime_target=config.runtime_target,
        install_evidence=config.install_evidence,
    )


def failure_record(error: BaseException, ordinal: int,
                   result_hash: str | None = None) -> dict[str, Any]:
    message = str(error).replace("\n", " ")[:500]
    record: dict[str, Any] = {
        "status": "failed",
        "reset": True,
        "trial_ordinal": ordinal,
        "recorded_at": utc_now(),
        "error_type": type(error).__name__,
        "error": message,
    }
    if result_hash is not None:
        record["result_json_sha256"] = result_hash
    return record


def final_summary(*, config: Configuration, started_at: str,
                  binding: dict[str, Any] | None,
                  processes: dict[str, dict[str, Any]] | None,
                  manifest: dict[str, Any], entries: list[dict[str, Any]],
                  previous_head: str | None, entry_number: int,
                  evidence_relative: str,
                  trials: list[dict[str, Any]], failure: dict[str, Any] | None,
                  status: str) -> dict[str, Any]:
    passed = status == "passed"
    prior_runs = sum(entry["completed_trials"] for entry in entries)
    cumulative = prior_runs + len(trials) if passed else 0
    labels = [entry["generation_label"] for entry in entries]
    labels.append(config.generation_label)
    distinct_labels = len(labels) == len(set(labels))
    fresh_role_processes = passed and processes is not None
    summary = {
        "schema": SCHEMA,
        "chain_id": manifest["chain_id"],
        "chain_entry_number": entry_number,
        "previous_chain_head_sha256": previous_head,
        "evidence_root": evidence_relative,
        "generation_label": config.generation_label,
        "status": status,
        "reset": not passed,
        "started_at": started_at,
        "finished_at": utc_now(),
        "requested_trials": config.count,
        "completed_trials": len(trials),
        "cumulative_consecutive_runs": cumulative,
        "generation_count": entry_number,
        "distinct_generation_labels": distinct_labels,
        "distinct_process_generations": fresh_role_processes,
        "fresh_role_process_identities": fresh_role_processes,
        "three_generation_sixty_run_qualified": bool(
            passed and cumulative >= 60 and entry_number >= 3
            and distinct_labels and fresh_role_processes),
        "binding": binding,
        "binding_sha256": cohort_fingerprint(binding) if binding else None,
        "generation_processes": processes,
        "trials": trials,
        "failure": failure,
    }
    return summary


def run_generation(raw_config: Configuration) -> dict[str, Any]:
    config = validate_configuration(raw_config)
    initialize_chain_root(config.chain_root)
    manifest, entries, previous_head = load_chain(config.chain_root)
    if entries and entries[-1]["status"] == "failed":
        raise QualificationError(
            "qualification chain is closed by a failed entry; use a new empty chain root")
    if config.generation_label in {entry["generation_label"] for entry in entries}:
        raise QualificationError("generation label is already present in the chain")
    entry_number = len(entries) + 1
    evidence_relative = (
        f"{CHAIN_EVIDENCE_DIRECTORY}/{entry_number:06d}-{config.generation_label}")
    evidence_root = config.chain_root / evidence_relative
    evidence_root.mkdir(mode=0o700, exist_ok=False)
    started_at = utc_now()
    binding: dict[str, Any] | None = None
    baseline_processes: dict[str, dict[str, Any]] | None = None
    trials: list[dict[str, Any]] = []
    failure: dict[str, Any] | None = None
    active_ordinal = 0
    active_trial: Path | None = None
    active_result_hash: str | None = None
    try:
        binding = collect_static_binding(config)
        binding_hash = cohort_fingerprint(binding)
        baseline_processes = launchd_processes()
        if entries:
            if any(entry.get("binding_sha256") != binding_hash for entry in entries):
                raise QualificationError("previous chain belongs to a different qualification cohort")
            assert_fresh_role_processes(baseline_processes, entries)
        atomic_create_json(evidence_root / "generation-start.json", {
            "schema": SCHEMA,
            "chain_id": manifest["chain_id"],
            "chain_entry_number": entry_number,
            "previous_chain_head_sha256": previous_head,
            "evidence_root": evidence_relative,
            "generation_label": config.generation_label,
            "started_at": started_at,
            "requested_trials": config.count,
            "binding": binding,
            "binding_sha256": binding_hash,
            "generation_processes": baseline_processes,
        })
        markers: set[str] = set()
        for ordinal in range(1, config.count + 1):
            active_ordinal = ordinal
            active_result_hash = None
            if collect_static_binding(config) != binding:
                raise QualificationError("static source or installed binding changed before trial")
            before = launchd_processes()
            if before != baseline_processes:
                raise QualificationError("launchd process generation changed before trial")
            marker = make_marker()
            if marker in markers:
                raise QualificationError("qualification marker collision")
            markers.add(marker)
            active_trial = evidence_root / f"trial-{ordinal:03d}"
            active_trial.mkdir(mode=0o700)
            trial_started = utc_now()
            atomic_create_json(active_trial / "trial-start.json", {
                "schema": SCHEMA,
                "generation_label": config.generation_label,
                "trial_ordinal": ordinal,
                "marker": marker,
                "started_at": trial_started,
                "binding_sha256": binding_hash,
                "processes_before": before,
            })
            e2e_dir = active_trial / "e2e"
            returncode = invoke_e2e(config, e2e_dir, marker)
            result_path = e2e_dir / "result.json"
            if result_path.exists() and result_path.is_file() and not result_path.is_symlink():
                active_result_hash = sha256_file(result_path)
            if returncode != 0:
                raise QualificationError(
                    f"installed E2E failed with exit status {returncode}")
            _result, active_result_hash = read_result(result_path, marker, e2e_dir)
            after = launchd_processes()
            if after != before or after != baseline_processes:
                raise QualificationError("launchd process identity changed during trial")
            if collect_static_binding(config) != binding:
                raise QualificationError("static source or installed binding changed during trial")
            completed_at = utc_now()
            trial_record = {
                "generation_label": config.generation_label,
                "trial_ordinal": ordinal,
                "marker": marker,
                "started_at": trial_started,
                "finished_at": completed_at,
                "binding_sha256": binding_hash,
                "processes_before": before,
                "processes_after": after,
                "result_json_sha256": active_result_hash,
                "evidence": f"trial-{ordinal:03d}/e2e",
                "status": "passed",
            }
            atomic_create_json(active_trial / "qualification.json", trial_record)
            trials.append(trial_record)
            active_trial = None
        status = "passed"
    except BaseException as error:
        failure = failure_record(error, active_ordinal, active_result_hash)
        failure_path = ((active_trial / "failure.json") if active_trial is not None
                        else (evidence_root / "failure.json"))
        atomic_create_json(failure_path, failure)
        status = "failed"
    summary = final_summary(
        config=config, started_at=started_at, binding=binding,
        processes=baseline_processes, manifest=manifest, entries=entries,
        previous_head=previous_head, entry_number=entry_number,
        evidence_relative=evidence_relative,
        trials=trials, failure=failure, status=status)
    atomic_create_json(evidence_root / "generation-summary.json", summary)
    seal_generation_files(evidence_root)
    appended = append_chain_entry(
        root=config.chain_root, manifest=manifest, entries=entries,
        previous_head=previous_head, summary=summary, evidence_root=evidence_root)
    _manifest, _entries, current_head = load_chain(config.chain_root)
    if current_head != appended["entry_sha256"]:
        raise QualificationError("qualification chain current head validation failed")
    return summary


def parse(argv: list[str] | None = None) -> Configuration:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--chain-root", type=Path, required=True)
    parser.add_argument("--generation", required=True, dest="generation_label")
    parser.add_argument("--app-sha256", required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--browser", type=Path)
    parser.add_argument("--trial-timeout", type=int, default=180)
    parser.add_argument("--runtime-target", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--install-evidence", type=Path,
                        default=DEFAULT_INSTALL_EVIDENCE)
    options = parser.parse_args(argv)
    return Configuration(**vars(options))


def main(argv: list[str] | None = None) -> int:
    try:
        config = parse(argv)
        summary = run_generation(config)
    except (QualificationError, OSError) as error:
        print(f"qualification refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps({
        "status": summary["status"],
        "generation_label": summary["generation_label"],
        "completed_trials": summary["completed_trials"],
        "cumulative_consecutive_runs": summary["cumulative_consecutive_runs"],
        "three_generation_sixty_run_qualified":
            summary["three_generation_sixty_run_qualified"],
    }, sort_keys=True))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
