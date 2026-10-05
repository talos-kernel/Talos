from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
from types import SimpleNamespace
import zlib

import pytest

from tests import qmp_hardware_qualification as qualification


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
QEMU_SHA256 = "6" * 64
KERNEL_SHA256 = "7" * 64
INITRD_SHA256 = "8" * 64
DISK_SHA256 = "9" * 64
MANIFEST_SHA256 = "c" * 64
PNG_HEADER = b"\x89PNG\r\n\x1a\n"
DEFAULT_MARKER = "TQMARKER0000000001"


def png_chunk(kind: bytes, value: bytes) -> bytes:
    return (struct.pack(">I", len(value)) + kind + value
            + struct.pack(">I", zlib.crc32(kind + value) & 0xffffffff))


def framebuffer_png(changed_pixels: int = 0) -> bytes:
    width, height, stride = 1440, 900, 1440 * 3
    pixels = bytearray(stride * height)
    remaining = changed_pixels
    for y in range(36, 884):
        take = min(remaining, 1424 - 16)
        if take:
            start = (y * width + 16) * 3
            pixels[start:start + take * 3] = b"\xff\xff\xff" * take
            remaining -= take
        if not remaining:
            break
    assert remaining == 0
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        rows.extend(pixels[y * stride:(y + 1) * stride])

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (PNG_HEADER + png_chunk(b"IHDR", header)
            + png_chunk(b"IDAT", zlib.compress(bytes(rows)))
            + png_chunk(b"IEND", b""))


def png_with_idat(payload: bytes) -> bytes:
    header = struct.pack(">IIBBBBB", 1440, 900, 8, 2, 0, 0, 0)
    return (PNG_HEADER + png_chunk(b"IHDR", header)
            + png_chunk(b"IDAT", payload) + png_chunk(b"IEND", b""))


BASELINE_PNG = framebuffer_png()
CLEANUP_PNG_1 = BASELINE_PNG
CLEANUP_PNG_2 = framebuffer_png(1)
RETURN_PNG = framebuffer_png(151)
TERMINAL_OPEN_PNG = framebuffer_png(50001)
FOCUSED_TERMINAL_PNG = framebuffer_png(50002)


def malformed_pngs() -> list[tuple[str, bytes]]:
    bad_crc = bytearray(BASELINE_PNG)
    bad_crc[29] ^= 0x01
    return [
        ("truncation", BASELINE_PNG[:-2]),
        ("missing-iend", BASELINE_PNG[:-12]),
        ("bad-crc", bytes(bad_crc)),
        ("oversized-payload", png_with_idat(
            b"x" * (qualification.PNG_MAX_COMPRESSED_SIZE + 1))),
        ("oversized-decompression", png_with_idat(zlib.compress(
            b"\x00" * (qualification.PNG_DECOMPRESSED_SIZE + 1)))),
        ("trailing-bytes", BASELINE_PNG + b"trailing"),
    ]


def expected_inputs(marker: str) -> tuple[list[dict], list[dict]]:
    trial = [
        {"op": "click", "x": 130, "y": 13, "button": 1},
        {"op": "key", "keys": "super+Return"},
        {"op": "click", "x": 320, "y": 420, "button": 1},
        {"op": "key", "keys": "ctrl+c"},
        {"op": "type", "text": "echo " + marker},
        {"op": "key", "keys": "Return"},
    ]
    cleanup = [
        {"op": "click", "x": 320, "y": 420, "button": 1},
        {"op": "type", "text": "exit"},
        {"op": "key", "keys": "Return"},
    ]
    return trial, [*trial, *cleanup]


def valid_result(marker: str = DEFAULT_MARKER) -> dict:
    trial_inputs, all_inputs = expected_inputs(marker)
    dispatch = {"input": "dispatched", "control": "human"}
    typed = trial_inputs[-2]
    returned = trial_inputs[-1]
    cleanup_inputs = all_inputs[len(trial_inputs):]
    cleanup_receipts = [
        {"request": request, "status": 200, "body": dispatch}
        for request in cleanup_inputs
    ]
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
        "focused_terminal_changed_pixels": 50002,
        "terminal_launch_latency_ms": 25000,
        "type_latency_ms": 20,
        "return_latency_ms": 21,
        "final_job_count": 0,
        "initial_job_hash": DIGEST_A,
        "final_job_hash": DIGEST_A,
        "baseline_sha256": hashlib.sha256(BASELINE_PNG).hexdigest(),
        "terminal_open_sha256": hashlib.sha256(TERMINAL_OPEN_PNG).hexdigest(),
        "focused_terminal_sha256": hashlib.sha256(FOCUSED_TERMINAL_PNG).hexdigest(),
        "return_ocr_frame_sha256": hashlib.sha256(RETURN_PNG).hexdigest(),
        "trial_input_requests": trial_inputs,
        "input_requests": all_inputs,
        "type_transport": {"request": typed, "status": 200, "body": dispatch},
        "return_transport": {"request": returned, "status": 200, "body": dispatch},
        "cleanup_transport_receipts": cleanup_receipts,
        "cleanup_samples": [
            {"evidence": "05-cleanup-1.png", "marker_count": 0,
             "changed_pixels": 0,
             "sha256": hashlib.sha256(CLEANUP_PNG_1).hexdigest()},
            {"evidence": "05-cleanup-2.png", "marker_count": 0,
             "changed_pixels": 1,
             "sha256": hashlib.sha256(CLEANUP_PNG_2).hexdigest()},
        ],
    }


def write_e2e_evidence(evidence: Path, marker: str = DEFAULT_MARKER,
                       result: dict | None = None) -> dict:
    evidence.mkdir(parents=True)
    (evidence / "00-empty-workspace.png").write_bytes(BASELINE_PNG)
    (evidence / "00-terminal-open.png").write_bytes(TERMINAL_OPEN_PNG)
    (evidence / "00-focused-terminal.png").write_bytes(FOCUSED_TERMINAL_PNG)
    (evidence / "02-ocr-frame.png").write_bytes(RETURN_PNG)
    (evidence / "05-cleanup-1.png").write_bytes(CLEANUP_PNG_1)
    (evidence / "05-cleanup-2.png").write_bytes(CLEANUP_PNG_2)
    value = valid_result(marker) if result is None else result
    cleanup_inputs = value["input_requests"][len(value["trial_input_requests"]):]
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


def install_evidence_value(
        release: str = "/installed/runtime/generation") -> dict:
    install_root = Path(release).parent.parent
    return {
        "release": Path(release).name,
        "network": "qemu-user-nat",
        "host_forwards": [],
        "host_shares": False,
        "clipboard": False,
        "manifest": {
            "path": str(Path(release) / "guest/manifest.json"),
            "sha256": MANIFEST_SHA256,
        },
        "qemu": {
            "path": str(Path(release) / "vm-runtime/bin/qemu-system-x86_64"),
            "sha256": QEMU_SHA256,
        },
        "kernel": {
            "path": str(Path(release) / "guest/boot/vmlinuz"),
            "sha256": KERNEL_SHA256,
        },
        "initrd": {
            "path": str(Path(release) / "guest/boot/initrd.img"),
            "sha256": INITRD_SHA256,
        },
        "disk": {
            "path": str(install_root / "vm/rootfs.ext4"),
            "sha256": DISK_SHA256,
            "bytes": 25_769_803_776,
            "format": "raw",
        },
        "machine": "pc-q35-9.2",
        "accelerator": "tcg,thread=multi",
        "cpu": "max",
        "architecture": "x86_64",
        "distribution": "debian",
        "desktop": "hyprland",
        "geometry": [1440, 900],
        "pci_devices": [[0x1B36, 0x0008], [0x1AF4, 0x1000]],
    }


def binding() -> dict:
    install = install_evidence_value()
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
            **install,
        },
        "harness": {"path": "tests/qmp_installed_e2e.py", "sha256": "5" * 64},
        "host": {"model": "Mac15,7", "macos": "15.7", "arch": "arm64"},
        "artifacts": {"app_sha256": DIGEST_A, "archive_sha256": DIGEST_B},
    }


def processes(seed: int = 0) -> dict:
    return {
        role: {
            "label": f"org.talos.qmp.{role}",
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


def test_runtime_identity_rejects_release_outside_runtime_directory(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    outside = tmp_path / "outside/release"
    (outside / "backend/talos").mkdir(parents=True)
    current = runtime / "current"
    current.symlink_to(outside)

    with pytest.raises(
            qualification.QualificationError, match="sibling release"):
        qualification.runtime_identity(current)


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


def runtime_binding(
        release: str = "/installed/runtime/generation") -> dict[str, str]:
    release_path = Path(release)
    return {
        "target": str(release_path.parent / "current"),
        "release": str(release_path),
        "release_name": release_path.name,
        "backend_tree_sha256": "3" * 64,
    }


def materialize_installed_files(tmp_path: Path) -> tuple[dict, dict, dict[str, Path]]:
    release = tmp_path / "installed/runtime/generation"
    evidence = install_evidence_value(str(release))
    files = {
        "qemu": Path(evidence["qemu"]["path"]),
        "kernel": Path(evidence["kernel"]["path"]),
        "initrd": Path(evidence["initrd"]["path"]),
        "disk": Path(evidence["disk"]["path"]),
        "manifest": Path(evidence["manifest"]["path"]),
    }
    contents = {
        "qemu": b"arm64-host-qemu-x86-target",
        "kernel": b"x86-kernel",
        "initrd": b"x86-initrd",
        "disk": b"diskdata",
    }
    for name, raw in contents.items():
        files[name].parent.mkdir(parents=True, exist_ok=True)
        files[name].write_bytes(raw)
        evidence[name]["sha256"] = hashlib.sha256(raw).hexdigest()
    evidence["disk"]["bytes"] = len(contents["disk"])
    manifest = {
        "schema": "talos.qmp-vm/v1",
        "guestArchitecture": evidence["architecture"],
        "guestDistribution": evidence["distribution"],
        "guestDesktop": evidence["desktop"],
        "geometry": evidence["geometry"],
        "qemuBinary": files["qemu"].relative_to(release / "vm-runtime").as_posix(),
        "qemuSha256": evidence["qemu"]["sha256"],
        "qemuMachine": evidence["machine"],
        "qemuCpu": evidence["cpu"],
        "kernel": files["kernel"].relative_to(release / "guest").as_posix(),
        "kernelSha256": evidence["kernel"]["sha256"],
        "initrd": files["initrd"].relative_to(release / "guest").as_posix(),
        "initrdSha256": evidence["initrd"]["sha256"],
        "kernelCommandLine": "root=/dev/vda rw console=hvc0",
        "diskFormat": evidence["disk"]["format"],
        "diskBytes": evidence["disk"]["bytes"],
        "diskSha256": evidence["disk"]["sha256"],
        "pciDevices": evidence["pci_devices"],
    }
    files["manifest"].parent.mkdir(parents=True, exist_ok=True)
    files["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    evidence["manifest"]["sha256"] = hashlib.sha256(
        files["manifest"].read_bytes()).hexdigest()
    return runtime_binding(str(release)), evidence, files


def write_install_evidence(path: Path, value: dict | None = None) -> bytes:
    raw = (json.dumps(
        install_evidence_value() if value is None else value,
        sort_keys=True,
    ) + "\n").encode()
    path.write_bytes(raw)
    return raw


def test_install_identity_binds_complete_fixed_guest_contract(tmp_path):
    runtime, evidence, _files = materialize_installed_files(tmp_path)
    evidence_path = tmp_path / "install-evidence.json"
    raw = write_install_evidence(evidence_path, evidence)

    observed = qualification.install_identity(evidence_path, runtime)

    assert observed == {
        "path": str(evidence_path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        **evidence,
    }


@pytest.mark.parametrize("artifact", ["manifest", "qemu", "kernel", "initrd", "disk"])
def test_install_identity_hashes_each_actual_installed_artifact(tmp_path, artifact):
    runtime, evidence, files = materialize_installed_files(tmp_path)
    evidence_path = tmp_path / "install-evidence.json"
    write_install_evidence(evidence_path, evidence)
    files[artifact].write_bytes(files[artifact].read_bytes() + b"tampered")

    with pytest.raises(qualification.QualificationError, match="installed"):
        qualification.install_identity(evidence_path, runtime)


def test_install_identity_rejects_symlinked_and_non_regular_installed_files(tmp_path):
    runtime, evidence, files = materialize_installed_files(tmp_path)
    evidence_path = tmp_path / "install-evidence.json"
    write_install_evidence(evidence_path, evidence)
    outside = tmp_path / "outside-qemu"
    outside.write_bytes(files["qemu"].read_bytes())
    files["qemu"].unlink()
    files["qemu"].symlink_to(outside)
    with pytest.raises(qualification.QualificationError, match="symbolic link"):
        qualification.install_identity(evidence_path, runtime)

    runtime, evidence, files = materialize_installed_files(tmp_path / "second")
    evidence_path = tmp_path / "second-install-evidence.json"
    write_install_evidence(evidence_path, evidence)
    files["kernel"].unlink()
    files["kernel"].mkdir()
    with pytest.raises(qualification.QualificationError, match="not a regular file"):
        qualification.install_identity(evidence_path, runtime)


def test_install_identity_independently_checks_disk_byte_count(tmp_path):
    runtime, evidence, _files = materialize_installed_files(tmp_path)
    evidence["disk"]["bytes"] += 1
    evidence_path = tmp_path / "install-evidence.json"
    write_install_evidence(evidence_path, evidence)

    with pytest.raises(qualification.QualificationError, match="byte count"):
        qualification.install_identity(evidence_path, runtime)


def test_install_identity_cross_checks_installed_signed_manifest(tmp_path):
    runtime, evidence, files = materialize_installed_files(tmp_path)
    manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
    manifest["guestDistribution"] = "ubuntu"
    files["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    evidence["manifest"]["sha256"] = hashlib.sha256(
        files["manifest"].read_bytes()).hexdigest()
    evidence_path = tmp_path / "install-evidence.json"
    write_install_evidence(evidence_path, evidence)

    with pytest.raises(qualification.QualificationError,
                       match="manifest does not match"):
        qualification.install_identity(evidence_path, runtime)


@pytest.mark.parametrize("devices", [
    [[0x1B36, 0x0008]],
    [[0x1AF4, 0x1000], [0x1B36, 0x0008], [0x1AF4, 0x1000]],
])
def test_install_identity_requires_exactly_one_expected_virtio_nic(tmp_path, devices):
    runtime, evidence, _files = materialize_installed_files(tmp_path)
    evidence["pci_devices"] = devices
    evidence_path = tmp_path / "install-evidence.json"
    write_install_evidence(evidence_path, evidence)

    with pytest.raises(qualification.QualificationError,
                       match="exactly one expected virtio NIC"):
        qualification.install_identity(evidence_path, runtime)


@pytest.mark.parametrize("field", sorted(qualification.INSTALL_EVIDENCE_FIELDS))
def test_install_identity_requires_every_installer_evidence_field(tmp_path, field):
    evidence = install_evidence_value()
    evidence.pop(field)
    evidence_path = tmp_path / "install-evidence.json"
    write_install_evidence(evidence_path, evidence)

    with pytest.raises(qualification.QualificationError,
                       match="unknown or missing fields"):
        qualification.install_identity(evidence_path, runtime_binding())


@pytest.mark.parametrize(
    ("field_path", "replacement"),
    [
        (("architecture",), "aarch64"),
        (("distribution",), "ubuntu"),
        (("desktop",), "gnome"),
        (("geometry",), [1920, 1080]),
        (("qemu", "path"),
         "/installed/runtime/generation/vm-runtime/bin/qemu-system-aarch64"),
        (("qemu", "sha256"), "A" * 64),
        (("manifest", "sha256"), "short"),
        (("machine",), "q35"),
        (("cpu",), "host"),
        (("kernel", "sha256"), "short"),
        (("initrd", "sha256"), "short"),
        (("initrd", "path"), "/installed/runtime/generation/guest/boot/vmlinuz"),
        (("disk", "sha256"), "short"),
        (("disk", "bytes"), True),
        (("disk", "format"), "vmdk"),
        (("pci_devices",), []),
        (("pci_devices",), [[0x1AF4, True]]),
        (("network",), "bridge"),
        (("host_forwards",), ["tcp:2222"]),
        (("host_shares",), True),
        (("clipboard",), True),
        (("accelerator",), "hvf"),
    ],
)
def test_install_identity_rejects_mutated_guest_or_isolation_identity(
        tmp_path, field_path, replacement):
    runtime, original, _files = materialize_installed_files(tmp_path)
    evidence = json.loads(json.dumps(original))
    target = evidence
    for name in field_path[:-1]:
        target = target[name]
    target[field_path[-1]] = replacement
    evidence_path = tmp_path / "install-evidence.json"
    write_install_evidence(evidence_path, evidence)

    with pytest.raises(qualification.QualificationError, match="install evidence"):
        qualification.install_identity(evidence_path, runtime)


def test_install_identity_rejects_unknown_fields_and_wrong_release_paths(tmp_path):
    evidence = install_evidence_value()
    evidence["offline"] = True
    evidence_path = tmp_path / "install-evidence.json"
    write_install_evidence(evidence_path, evidence)
    with pytest.raises(qualification.QualificationError,
                       match="unknown or missing fields"):
        qualification.install_identity(evidence_path, runtime_binding())

    evidence = install_evidence_value()
    evidence["disk"]["path"] = "/tmp/rootfs.ext4"
    write_install_evidence(evidence_path, evidence)
    with pytest.raises(qualification.QualificationError, match="disk identity"):
        qualification.install_identity(evidence_path, runtime_binding())


def test_collect_static_binding_carries_validated_install_identity(tmp_path, monkeypatch):
    runtime, evidence, _files = materialize_installed_files(tmp_path)
    repository = tmp_path / "repository"
    harness = repository / qualification.HARNESS_RELATIVE
    harness.parent.mkdir(parents=True)
    harness.write_text("# installed E2E\n", encoding="utf-8")
    evidence_path = tmp_path / "install-evidence.json"
    raw = write_install_evidence(evidence_path, evidence)
    runtime_target = tmp_path / "runtime/current"
    config = qualification.Configuration(
        profile=tmp_path / "profile",
        chain_root=tmp_path / "chain",
        generation_label="generation-1",
        app_sha256=DIGEST_A,
        archive_sha256=DIGEST_B,
        repository=repository,
        runtime_target=runtime_target,
        install_evidence=evidence_path,
    )
    monkeypatch.setattr(qualification, "git_identity", lambda _repository: {
        "head": "1" * 40, "tree": "2" * 40, "clean": True})
    monkeypatch.setattr(
        qualification, "runtime_identity", lambda _target: runtime)
    monkeypatch.setattr(qualification, "host_identity", lambda: {
        "model": "Mac15,7", "macos": "15.7", "arch": "arm64"})

    observed = qualification.collect_static_binding(config)

    assert observed["install_evidence"] == {
        "path": str(evidence_path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        **evidence,
    }
    assert observed["harness"] == {
        "path": "tests/qmp_installed_e2e.py",
        "sha256": hashlib.sha256(harness.read_bytes()).hexdigest(),
    }


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


@pytest.mark.parametrize(("case", "raw"), malformed_pngs())
def test_qualification_png_decoder_rejects_malformed_input(case, raw):
    with pytest.raises(qualification.QualificationError):
        qualification.png_pixels(raw, case)


def test_result_validation_allows_distinct_authenticated_cleanup_frames(tmp_path):
    evidence = tmp_path / "e2e"
    result = valid_result()
    second = framebuffer_png(2)
    result["cleanup_samples"][1]["sha256"] = hashlib.sha256(second).hexdigest()
    result["cleanup_samples"][1]["changed_pixels"] = 2
    write_e2e_evidence(evidence, result=result)
    (evidence / "05-cleanup-2.png").write_bytes(second)

    qualification.validate_result(result, DEFAULT_MARKER, evidence)


@pytest.mark.parametrize(
    ("artifact", "message"),
    [
        ("00-empty-workspace.png", "baseline PNG hash"),
        ("00-terminal-open.png", "terminal PNG hash"),
        ("00-focused-terminal.png", "terminal PNG hash"),
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


@pytest.mark.parametrize(
    "artifact", ["00-terminal-open.png", "00-focused-terminal.png"])
def test_result_validation_requires_terminal_visual_artifacts(tmp_path, artifact):
    evidence = tmp_path / "e2e"
    result = write_e2e_evidence(evidence)
    (evidence / artifact).unlink()

    with pytest.raises(qualification.QualificationError, match="unavailable"):
        qualification.validate_result(result, DEFAULT_MARKER, evidence)


@pytest.mark.parametrize(
    "field", ["terminal_open_changed_pixels", "focused_terminal_changed_pixels"])
def test_result_validation_recomputes_terminal_visual_delta(tmp_path, field):
    evidence = tmp_path / "e2e"
    result = valid_result()
    result[field] += 1
    write_e2e_evidence(evidence, result=result)

    with pytest.raises(qualification.QualificationError, match="delta"):
        qualification.validate_result(result, DEFAULT_MARKER, evidence)


def test_result_validation_rejects_terminal_launch_after_absolute_deadline(tmp_path):
    evidence = tmp_path / "e2e"
    result = valid_result()
    result["terminal_launch_latency_ms"] = 25001
    write_e2e_evidence(evidence, result=result)

    with pytest.raises(qualification.QualificationError,
                       match="terminal_launch_latency_ms"):
        qualification.validate_result(result, DEFAULT_MARKER, evidence)


def test_result_validation_recomputes_cleanup_visual_delta(tmp_path):
    evidence = tmp_path / "e2e"
    result = valid_result()
    result["cleanup_samples"][1]["changed_pixels"] = 2
    write_e2e_evidence(evidence, result=result)

    with pytest.raises(qualification.QualificationError,
                       match="cleanup PNG delta"):
        qualification.validate_result(result, DEFAULT_MARKER, evidence)


def test_result_validation_strictly_decodes_cleanup_png(tmp_path):
    evidence = tmp_path / "e2e"
    result = valid_result()
    malformed = CLEANUP_PNG_2[:-12]
    result["cleanup_samples"][1]["sha256"] = hashlib.sha256(malformed).hexdigest()
    write_e2e_evidence(evidence, result=result)
    (evidence / "05-cleanup-2.png").write_bytes(malformed)

    with pytest.raises(qualification.QualificationError, match="missing IEND"):
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
    assert len(marker) == 21 and marker.startswith("X")
    assert set(marker[1:]) <= set("23456789abcdefgh")
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
    generation_start = json.loads(
        (evidence_root / "generation-start.json").read_text(encoding="utf-8"))
    sealed_summary = json.loads(
        (evidence_root / "generation-summary.json").read_text(encoding="utf-8"))
    assert generation_start["binding"] == sealed_summary["binding"] == binding()
    assert sealed_summary["binding"]["install_evidence"]["architecture"] == "x86_64"
    assert sealed_summary["binding"]["install_evidence"]["qemu"] == {
        "path": ("/installed/runtime/generation/vm-runtime/bin/"
                 "qemu-system-x86_64"),
        "sha256": QEMU_SHA256,
    }
    assert sealed_summary["binding_sha256"] == qualification.canonical_hash(
        sealed_summary["binding"])
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
