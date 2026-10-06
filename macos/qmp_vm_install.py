#!/usr/bin/env python3
"""Install the QMP VM Computer behind a dedicated macOS service account.

The installer consumes a signed VM bundle, an already-provisioned guest disk and a
Talos app/source tree. It never downloads, mounts host folders or accepts credentials
on its command line. QEMU's unprivileged user-mode network provides outbound NAT
without a root network helper.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import plistlib
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import time

SERVICE_USER = "talosqmpd"
SERVICE_GROUP = "talosqmpd"
CLIENT_GROUP = "talosqmpclients"
ROOT = Path("/Library/Application Support/TalosQmpVm")
LAUNCHD = Path("/Library/LaunchDaemons")
LABELS = ("org.talos.qmp.web", "org.talos.qmp.api",
          "org.talos.qmp.vm")
RETIRED_LABELS = ("org.talos.qmp.network",)
PROFILE_KEYS = (
    "TALOS_COMPUTER_BACKEND", "TALOS_COMPUTER_DESKTOP",
    "TALOS_COMPUTER_OWNER_SHA256", "TALOS_COMPUTER_ROOT",
    "TALOS_COMPUTER_SOCKET", "TALOS_COMPUTER_VIEW_KEY_FILE",
    "TALOS_COMPUTER_VIEW_URL",
)
MANIFEST_FIELDS = frozenset({
    "schema", "guestArchitecture", "guestDistribution", "guestDesktop", "geometry",
    "qemuBinary", "qemuSha256", "qemuMachine", "qemuCpu", "kernel",
    "kernelSha256", "initrd", "initrdSha256", "kernelCommandLine", "diskFormat",
    "diskBytes", "diskSha256", "pciDevices",
})
SHA256 = re.compile(r"[0-9a-f]{64}")
Q35_MACHINE = re.compile(r"pc-q35-[1-9][0-9]*\.[0-9]+")
MAX_MANIFEST_BYTES = 65_536
MAX_PCI_DEVICES = 32


def _run(*argv: str, capture=False, check=True):
    return subprocess.run(argv, check=check, text=True,
                          capture_output=capture)


def _launchd_label_is_absent(label: str) -> bool:
    result = _run("/bin/launchctl", "print", "system/" + label,
                  capture=True, check=False)
    diagnostic = f'Could not find service "{label}" in domain for system'
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    # Neither a nonzero result nor generic launchctl text proves absence alone.
    return result.returncode == 113 and diagnostic in output


def _remove_retired_launchd_services() -> None:
    for label in RETIRED_LABELS:
        result = _run("/bin/launchctl", "bootout", "system/" + label,
                      capture=True, check=False)
        if not _launchd_label_is_absent(label):
            if result.returncode == 0:
                reason = "launchctl still reports the label loaded after bootout"
            else:
                detail = (result.stderr or result.stdout or "").strip()
                reason = detail or f"exit status {result.returncode}"
            raise SystemExit(f"failed to unload retired launchd service {label}: {reason}")
        (LAUNCHD / (label + ".plist")).unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record(path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"required regular file is missing: {path}")
    return {"path": str(path), "sha256": _sha256(path)}


def _strict_json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _declared_path(base: Path, value, label: str, *, executable=False) -> Path:
    if (not isinstance(value, str) or not value or "\\" in value or "\0" in value
            or len(value) > 1024):
        raise SystemExit(f"{label} path is invalid")
    relative = PurePosixPath(value)
    if (relative.is_absolute() or str(relative) != value
            or any(part in {"", ".", ".."} for part in relative.parts)):
        raise SystemExit(f"{label} path must be a canonical relative path")
    if base.is_symlink() or not base.is_dir():
        raise SystemExit(f"{label} base directory is missing or is a symbolic link")
    path = base.joinpath(*relative.parts)
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SystemExit(f"{label} must not be a symbolic link")
    if not path.is_file():
        raise SystemExit(f"{label} is not a regular file")
    if executable and not os.access(path, os.X_OK):
        raise SystemExit(f"{label} is not executable")
    return path


def verify_declared_files(runtime: Path, guest: Path, manifest: dict) -> bool:
    files = (
        (runtime, manifest["qemuBinary"], "QEMU binary", manifest["qemuSha256"], True),
        (guest, manifest["kernel"], "guest kernel", manifest["kernelSha256"], False),
        (guest, manifest["initrd"], "guest initrd", manifest["initrdSha256"], False),
    )
    for base, relative, label, expected, executable in files:
        path = _declared_path(base, relative, label, executable=executable)
        if _sha256(path) != expected:
            raise SystemExit(f"{label} hash does not match the VM manifest")
    return True


def load_bundle_manifest(resources: Path) -> dict:
    runtime, guest = resources / "runtime", resources / "guest"
    manifest_path = _declared_path(guest, "manifest.json", "guest manifest")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise SystemExit("guest manifest exceeds its size bound")
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {value}")),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise SystemExit(f"guest manifest is not strict JSON: {error}") from error
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS:
        raise SystemExit("guest manifest has unknown or missing fields")
    if (manifest["schema"] != "talos.qmp-vm/v1"
            or manifest["guestArchitecture"] != "x86_64"
            or manifest["guestDistribution"] != "debian"
            or manifest["guestDesktop"] != "hyprland"
            or manifest["geometry"] != [1440, 900]):
        raise SystemExit("guest manifest identity is not x86_64 Debian Hyprland at 1440x900")
    if (not isinstance(manifest["qemuBinary"], str)
            or PurePosixPath(manifest["qemuBinary"]).name != "qemu-system-x86_64"):
        raise SystemExit("guest manifest must select the x86_64 QEMU target binary")
    for field in ("qemuSha256", "kernelSha256", "initrdSha256", "diskSha256"):
        if not isinstance(manifest[field], str) or not SHA256.fullmatch(manifest[field]):
            raise SystemExit(f"guest manifest {field} is not a lowercase SHA-256")
    if (not isinstance(manifest["qemuMachine"], str)
            or not Q35_MACHINE.fullmatch(manifest["qemuMachine"])):
        raise SystemExit("guest manifest QEMU machine must be a versioned pc-q35-X.Y")
    if manifest["qemuCpu"] != "max":
        raise SystemExit("guest manifest QEMU CPU must be max")
    commandline = manifest["kernelCommandLine"]
    if (not isinstance(commandline, str) or not commandline.strip()
            or commandline != commandline.strip()
            or any(character in commandline for character in "\r\n\0")):
        raise SystemExit("guest manifest kernel command line must be one nonempty line")
    if (not isinstance(manifest["diskFormat"], str)
            or manifest["diskFormat"] not in {"raw", "qcow2"}):
        raise SystemExit("guest manifest disk format must be raw or qcow2")
    if type(manifest["diskBytes"]) is not int or manifest["diskBytes"] <= 0:
        raise SystemExit("guest manifest disk byte count must be a positive integer")
    devices = manifest["pciDevices"]
    if (not isinstance(devices, list) or not 1 <= len(devices) <= MAX_PCI_DEVICES
            or any(not isinstance(item, list) or len(item) != 2
                   or any(type(number) is not int or not 0 <= number <= 0xffff
                          for number in item)
                   for item in devices)):
        raise SystemExit("guest manifest PCI devices must be bounded vendor/device integer pairs")
    # Validate paths before hashes so no manifest-controlled path can leave its subtree.
    _declared_path(runtime, manifest["qemuBinary"], "QEMU binary", executable=True)
    _declared_path(guest, manifest["kernel"], "guest kernel")
    _declared_path(guest, manifest["initrd"], "guest initrd")
    verify_declared_files(runtime, guest, manifest)
    return manifest


def copy_bundle_resources(resources: Path, release: Path, manifest: dict) -> None:
    _copytree(resources / "runtime", release / "vm-runtime")
    _copytree(resources / "guest", release / "guest")
    source_manifest = _declared_path(resources / "guest", "manifest.json", "guest manifest")
    copied_manifest = _declared_path(release / "guest", "manifest.json", "copied guest manifest")
    if _sha256(copied_manifest) != _sha256(source_manifest):
        raise SystemExit("copied guest manifest hash does not match its source")
    verify_declared_files(release / "vm-runtime", release / "guest", manifest)


def verify_disk_image(path: Path, manifest: dict, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"{label} is missing, not regular, or is a symbolic link")
    if path.stat().st_size != manifest["diskBytes"]:
        raise SystemExit(f"{label} size does not match the VM manifest")
    if _sha256(path) != manifest["diskSha256"]:
        raise SystemExit(f"{label} hash does not match the VM manifest")
    return path.resolve(strict=True)


def install_disk(source: Path, target: Path, manifest: dict, *, uid: int, gid: int) -> None:
    source = verify_disk_image(source, manifest, "source guest disk")
    if not target.exists() and not target.is_symlink():
        _run("/bin/cp", "-c", str(source), str(target))
    verify_disk_image(target, manifest, "installed guest disk")
    os.chown(target, uid, gid)
    os.chmod(target, 0o600)


def install_evidence(release_id: str, release: Path, disk: Path, manifest: dict) -> dict:
    qemu = release / "vm-runtime" / manifest["qemuBinary"]
    kernel = release / "guest" / manifest["kernel"]
    initrd = release / "guest" / manifest["initrd"]
    return {
        "release": release_id, "network": "qemu-user-nat", "host_forwards": [],
        "host_shares": False, "clipboard": False,
        "manifest": _record(release / "guest/manifest.json"),
        "qemu": _record(qemu), "kernel": _record(kernel), "initrd": _record(initrd),
        "disk": {"path": str(disk), "sha256": _sha256(disk),
                 "bytes": disk.stat().st_size, "format": manifest["diskFormat"]},
        "machine": manifest["qemuMachine"], "accelerator": "tcg,thread=multi",
        "cpu": manifest["qemuCpu"], "architecture": manifest["guestArchitecture"],
        "distribution": manifest["guestDistribution"], "desktop": manifest["guestDesktop"],
        "geometry": list(manifest["geometry"]),
        "pci_devices": [list(item) for item in manifest["pciDevices"]],
    }


def qemu_arguments(root: Path, manifest: dict) -> list[str]:
    vm = root / "vm"
    release = root / "runtime/current"
    width, height = manifest["geometry"]
    return [
        str(release / "vm-runtime" / manifest["qemuBinary"]),
        "-name", "Talos QMP VM - NAT",
        # Q35 creates a PS/2 mouse even with -nodefaults. The visual preflight
        # requires exactly one absolute pointer, supplied by virtio-tablet below.
        "-machine", manifest["qemuMachine"] + ",i8042=off",
        "-accel", "tcg,thread=multi",
        "-cpu", manifest["qemuCpu"], "-smp", "4", "-m", "8192M",
        "-nodefaults", "-no-user-config",
        "-netdev", "user,id=talos-qmp-net,ipv6=off",
        "-device", ("virtio-net-pci,id=talos-qmp-nic,"
                    "netdev=talos-qmp-net,mac=52:54:00:12:34:56,romfile="),
        "-serial", "none", "-monitor", "none",
        "-qmp", f"unix:{vm / 'qmp.sock'},server=on,wait=off",
        "-pidfile", str(vm / "qemu.pid"),
        "-action", "reboot=reset,shutdown=poweroff",
        "-kernel", str(release / "guest" / manifest["kernel"]),
        "-initrd", str(release / "guest" / manifest["initrd"]),
        "-append", manifest["kernelCommandLine"],
        "-drive", (f"if=none,id=root,file={vm / 'rootfs.ext4'},"
                   f"format={manifest['diskFormat']},cache=writeback"),
        "-device", "virtio-blk-pci,drive=root,serial=talos-qmp-root",
        "-device", f"virtio-gpu-pci,max_outputs=1,xres={width},yres={height},romfile=",
        "-display", "none",
        "-device", "virtio-keyboard-pci,romfile=",
        "-device", "virtio-tablet-pci,romfile=",
        "-object", "rng-random,id=rng,filename=/dev/urandom",
        "-device", "virtio-rng-pci,rng=rng",
        "-device", "virtio-serial-pci,id=serial",
        "-chardev", f"file,id=console,path={vm / 'console.log'}",
        "-device", "virtconsole,bus=serial.0,nr=0,chardev=console",
    ]


def service_config(root: Path, *, agent_uid: int, client_gid: int,
                   owner: str, secret: str, manifest: dict) -> dict:
    vm, state = root / "vm", root / "state"
    return {
        "root": str(root), "owner": owner, "agent_uid": agent_uid,
        "client_gid": client_gid, "qmp": str(vm / "qmp.sock"),
        "pid_file": str(vm / "qemu.pid"), "disk": str(vm / "rootfs.ext4"),
        "state": str(state / "jobs"), "captures": str(state / "captures"),
        "scratch": str(state / "scratch"),
        "control_socket": str(root / "run/control.sock"),
        "view_url": "http://127.0.0.1:8830", "origin": "http://127.0.0.1:8830",
        "view_secret": secret, "viewer": "snapshot",
        "web_state": str(state / "web"),
        "architecture": manifest["guestArchitecture"],
        "distribution": manifest["guestDistribution"],
        "desktop": manifest["guestDesktop"],
        "geometry": list(manifest["geometry"]),
        "pci_devices": [list(item) for item in manifest["pciDevices"]],
    }


def launch_plists(root: Path, service_user: str = SERVICE_USER,
                  service_group: str = SERVICE_GROUP) -> dict[str, dict]:
    release = root / "runtime/current"
    python = str(release / "python/bin/python3")
    common = {
        "UserName": service_user, "GroupName": service_group,
        "RunAtLoad": True, "KeepAlive": True, "ProcessType": "Background",
        "ThrottleInterval": 3, "Umask": 63,
    }
    logs = root / "logs"
    vm = common | {
        "Label": LABELS[2], "ProgramArguments": [],
        "WorkingDirectory": str(root / "vm"),
        "EnvironmentVariables": {"HOME": "/var/empty", "LANG": "en_US.UTF-8",
                                 "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": str(logs / "vm.log"),
        "StandardErrorPath": str(logs / "vm.log"),
    }
    api = common | {
        "Label": LABELS[1],
        "ProgramArguments": [python, "-m", "talos.computer.qmp_service"],
        "EnvironmentVariables": {
            "HOME": "/var/empty", "LANG": "en_US.UTF-8",
            "PYTHONPATH": str(release / "backend"), "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "TALOS_QMP_VM_CONFIG": str(root / "config.json"),
        },
        "StandardOutPath": str(logs / "api.log"),
        "StandardErrorPath": str(logs / "api.log"),
    }
    web = common | {
        "Label": LABELS[0],
        "ProgramArguments": [python, "-m", "talos.computer.web"],
        "EnvironmentVariables": {
            "HOME": "/var/empty", "LANG": "en_US.UTF-8",
            "PYTHONPATH": str(release / "backend"), "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "TALOS_COMPUTER_CONFIG": str(root / "config.json"),
        },
        "StandardOutPath": str(logs / "web.log"),
        "StandardErrorPath": str(logs / "web.log"),
    }
    return {item["Label"]: item for item in (vm, api, web)}


def _ds_value(kind: str, name: str, attribute: str) -> str:
    output = _run("/usr/bin/dscl", ".", "-read", f"/{kind}/{name}", attribute,
                  capture=True).stdout.strip()
    return output.rsplit(":", 1)[1].strip()


def _listed_ids(output: str) -> set[int]:
    return {int(line.rsplit(None, 1)[1]) for line in output.splitlines()
            if line.split() and line.split()[-1].lstrip("-").isdigit()}


def _ensure_service_identity() -> tuple[int, int]:
    user_path, group_path = f"/Users/{SERVICE_USER}", f"/Groups/{SERVICE_GROUP}"
    user_exists = _run("/usr/bin/dscl", ".", "-read", user_path,
                       capture=True, check=False).returncode == 0
    group_exists = _run("/usr/bin/dscl", ".", "-read", group_path,
                        capture=True, check=False).returncode == 0
    if user_exists != group_exists:
        raise SystemExit("partial Talos QMP VM service identity exists; refusing to repair it")
    if not user_exists:
        occupied = _listed_ids(_run(
            "/usr/bin/dscl", ".", "-list", "/Users", "UniqueID", capture=True).stdout)
        occupied |= _listed_ids(_run(
            "/usr/bin/dscl", ".", "-list", "/Groups", "PrimaryGroupID", capture=True).stdout)
        identity = next((value for value in range(400, 500) if value not in occupied), None)
        if identity is None:
            raise SystemExit("no private service identity ID is available")
        try:
            _run("/usr/bin/dscl", ".", "-create", group_path)
            for attribute, value in (
                ("RealName", "Talos QMP VM Service"),
                ("PrimaryGroupID", str(identity)), ("Password", "*"),
            ):
                _run("/usr/bin/dscl", ".", "-create", group_path, attribute, value)
            _run("/usr/bin/dscl", ".", "-create", user_path)
            for attribute, value in (
                ("RealName", "Talos QMP VM Service"), ("UniqueID", str(identity)),
                ("PrimaryGroupID", str(identity)), ("UserShell", "/usr/bin/false"),
                ("NFSHomeDirectory", "/var/empty"), ("IsHidden", "1"), ("Password", "*"),
            ):
                _run("/usr/bin/dscl", ".", "-create", user_path, attribute, value)
        except Exception:
            # These exact records did not exist before this invocation. Remove a
            # partial identity so the next run cannot mistake it for a valid one.
            _run("/usr/bin/dscl", ".", "-delete", user_path, check=False)
            _run("/usr/bin/dscl", ".", "-delete", group_path, check=False)
            raise
    uid = int(_ds_value("Users", SERVICE_USER, "UniqueID"))
    gid = int(_ds_value("Groups", SERVICE_GROUP, "PrimaryGroupID"))
    if (uid != gid
            or int(_ds_value("Users", SERVICE_USER, "PrimaryGroupID")) != gid
            or _ds_value("Users", SERVICE_USER, "UserShell") != "/usr/bin/false"
            or _ds_value("Users", SERVICE_USER, "NFSHomeDirectory") != "/var/empty"
            or _ds_value("Users", SERVICE_USER, "IsHidden") != "1"):
        raise SystemExit("service identity is not locked down")
    return uid, gid


def _ensure_client_group(operator: str) -> int:
    exists = _run("/usr/bin/dscl", ".", "-read", f"/Groups/{CLIENT_GROUP}",
                  capture=True, check=False).returncode == 0
    if not exists:
        occupied = _listed_ids(_run(
            "/usr/bin/dscl", ".", "-list", "/Groups", "PrimaryGroupID", capture=True).stdout)
        gid = next((value for value in range(505, 600) if value not in occupied), None)
        if gid is None:
            raise SystemExit("no private client group ID is available")
        _run("/usr/bin/dscl", ".", "-create", f"/Groups/{CLIENT_GROUP}")
        _run("/usr/bin/dscl", ".", "-create", f"/Groups/{CLIENT_GROUP}",
             "RealName", "Talos QMP VM Clients")
        _run("/usr/bin/dscl", ".", "-create", f"/Groups/{CLIENT_GROUP}",
             "PrimaryGroupID", str(gid))
        _run("/usr/bin/dscl", ".", "-create", f"/Groups/{CLIENT_GROUP}", "Password", "*")
    gid = int(_ds_value("Groups", CLIENT_GROUP, "PrimaryGroupID"))
    for member in (SERVICE_USER, operator):
        _run("/usr/sbin/dseditgroup", "-o", "edit", "-a", member, "-t", "user", CLIENT_GROUP)
    return gid


def _copytree(source: Path, target: Path) -> None:
    if target.exists():
        raise SystemExit(f"refusing existing release path: {target}")
    shutil.copytree(source, target, symlinks=True, copy_function=shutil.copy2)


def _write(path: Path, data: bytes, mode: int, uid: int, gid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.chown(temporary, uid, gid)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _merge_profile(path: Path, values: dict[str, str], uid: int, gid: int) -> None:
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    kept = [line for line in existing
            if line.partition("=")[0].strip() not in PROFILE_KEYS]
    text = "\n".join([*kept, *(f"{key}={values[key]}" for key in PROFILE_KEYS)]) + "\n"
    _write(path, text.encode(), 0o600, uid, gid)


def configure_profile(options) -> None:
    """Apply the root-created one-shot handoff without giving root profile access."""
    if os.geteuid() == 0:
        raise SystemExit("profile configuration must run as the operator")
    handoff = options.handoff.resolve(strict=True)
    properties = handoff.lstat()
    if (handoff.is_symlink() or not stat.S_ISREG(properties.st_mode)
            or properties.st_uid != os.getuid() or properties.st_mode & 0o077):
        raise SystemExit("profile handoff is not a private operator-owned file")
    value = json.loads(handoff.read_text(encoding="utf-8"))
    if set(value) != {"profile", "key", "link", "secret", "values"}:
        raise SystemExit("profile handoff has unexpected fields")
    profile = options.profile.resolve(strict=True)
    if str(profile) != value["profile"] or profile.is_symlink() or profile.stat().st_uid != os.getuid():
        raise SystemExit("profile handoff does not match this operator profile")
    if set(value["values"]) != set(PROFILE_KEYS):
        raise SystemExit("profile handoff capability set changed")
    token = value["secret"]
    if not isinstance(token, str) or not 32 <= len(token) <= 128 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise SystemExit("profile handoff token is invalid")
    key, link = Path(value["key"]), Path(value["link"])
    if key.parent != profile or link.parent != profile:
        raise SystemExit("profile handoff targets leave the Talos profile")
    _write(key, (token + "\n").encode(), 0o600, os.getuid(), os.getgid())
    _write(link, (value["values"]["TALOS_COMPUTER_VIEW_URL"] + "/#token=" + token + "\n").encode(),
           0o600, os.getuid(), os.getgid())
    _merge_profile(profile / "talos.env", value["values"], os.getuid(), os.getgid())
    handoff.unlink()


def install(options) -> None:
    if os.geteuid() != 0:
        raise SystemExit("QMP VM installation must run as root")
    operator = pwd.getpwuid(options.operator_uid)
    if operator.pw_name != options.operator:
        raise SystemExit("operator name and UID do not match")
    if options.vm_bundle.is_symlink():
        raise SystemExit("VM bundle must not be a symbolic link")
    vm_bundle = options.vm_bundle.resolve(strict=True)
    if vm_bundle.suffix != ".app" or not vm_bundle.is_dir():
        raise SystemExit("VM bundle must be a signed macOS .app")
    resources = vm_bundle / "Contents/Resources"
    talos = options.talos_app.resolve(strict=True) / "Contents/Resources"
    backend = options.backend.resolve(strict=True)
    profile = Path(operator.pw_dir) / "Library/Application Support/Talos"
    if options.profile != profile:
        raise SystemExit("profile path does not match the operator account")
    # Authenticate and validate every caller-controlled artifact before unloading a
    # service, creating an identity, making a directory or changing a release link.
    _run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(vm_bundle))
    _run("/usr/sbin/spctl", "--assess", "--type", "execute", str(vm_bundle))
    manifest = load_bundle_manifest(resources)
    source_qemu = _declared_path(
        resources / "runtime", manifest["qemuBinary"], "QEMU binary", executable=True)
    _run("/usr/bin/lipo", "-verify_arch", "arm64", str(source_qemu))
    disk = verify_disk_image(options.disk, manifest, "source guest disk")
    _run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(options.talos_app.resolve()))
    if not (talos / "python/bin/python3").exists() or not (backend / "talos").is_dir():
        raise SystemExit("Talos Python runtime or backend is missing")
    target_disk = ROOT / "vm/rootfs.ext4"
    if target_disk.exists() or target_disk.is_symlink():
        verify_disk_image(target_disk, manifest, "installed guest disk")
    if options.handoff.exists():
        raise SystemExit("refusing an existing profile handoff")

    service_uid, service_gid = _ensure_service_identity()
    client_gid = _ensure_client_group(operator.pw_name)

    for label in LABELS:
        _run("/bin/launchctl", "bootout", "system/" + label, check=False)
    _remove_retired_launchd_services()
    root = ROOT
    for folder, mode, gid in (
        (root, 0o755, 0), (root / "runtime", 0o755, 0),
        (root / "vm", 0o700, service_gid), (root / "state", 0o710, client_gid),
        (root / "state/jobs", 0o700, service_gid),
        (root / "state/scratch", 0o700, service_gid),
        (root / "state/web", 0o700, service_gid),
        (root / "state/captures", 0o710, client_gid),
        (root / "run", 0o710, client_gid), (root / "logs", 0o700, service_gid),
    ):
        folder.mkdir(parents=True, exist_ok=True)
        os.chown(folder, service_uid if folder not in {root, root / "runtime"} else 0, gid)
        os.chmod(folder, mode)

    release_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    release = root / "runtime" / release_id
    release.mkdir(mode=0o755)
    copy_bundle_resources(resources, release, manifest)
    _copytree(talos / "python", release / "python")
    (release / "backend").mkdir(mode=0o755)
    _copytree(backend / "talos", release / "backend/talos")
    for parent, folders, files in os.walk(release):
        os.chown(parent, 0, service_gid)
        os.chmod(parent, 0o755)
        for name in files:
            item = Path(parent) / name
            if not item.is_symlink():
                os.chown(item, 0, service_gid)
                os.chmod(item, item.stat().st_mode & 0o755)
    installed_qemu = release / "vm-runtime" / manifest["qemuBinary"]
    _run("/usr/bin/codesign", "--verify", "--strict", str(installed_qemu))
    _run("/usr/bin/lipo", "-verify_arch", "arm64", str(installed_qemu))
    verify_declared_files(release / "vm-runtime", release / "guest", manifest)
    current = root / "runtime/current"
    next_current = root / "runtime" / (".current-" + release_id)
    next_current.symlink_to(release.name)
    os.lchown(next_current, 0, service_gid)
    os.replace(next_current, current)

    target_disk = root / "vm/rootfs.ext4"
    install_disk(disk, target_disk, manifest, uid=service_uid, gid=service_gid)

    secret = secrets.token_urlsafe(32)
    owner = hashlib.sha256(f"cli:{options.operator_uid}".encode()).hexdigest()
    config = service_config(root, agent_uid=options.operator_uid, client_gid=client_gid,
                            owner=owner, secret=secret, manifest=manifest)
    _write(root / "config.json", (json.dumps(config, indent=2) + "\n").encode(),
           0o640, 0, service_gid)
    plists = launch_plists(root)
    plists[LABELS[2]]["ProgramArguments"] = qemu_arguments(root, manifest)
    for label, value in plists.items():
        target = LAUNCHD / (label + ".plist")
        _write(target, plistlib.dumps(value, fmt=plistlib.FMT_XML), 0o644, 0, 0)
        _run("/usr/bin/plutil", "-lint", str(target))

    key, link = profile / "qmp-view.key", profile / "computer.url"
    profile_values = {
        "TALOS_COMPUTER_BACKEND": "qmp", "TALOS_COMPUTER_DESKTOP": "1",
        "TALOS_COMPUTER_OWNER_SHA256": owner,
        "TALOS_COMPUTER_ROOT": str(root / "state"),
        "TALOS_COMPUTER_SOCKET": config["control_socket"],
        "TALOS_COMPUTER_VIEW_KEY_FILE": str(key),
        "TALOS_COMPUTER_VIEW_URL": config["view_url"],
    }
    _write(options.handoff, (json.dumps({
        "profile": str(profile), "key": str(key), "link": str(link),
        "secret": secret, "values": profile_values,
    }) + "\n").encode(), 0o600, options.operator_uid, operator.pw_gid)

    evidence = install_evidence(release_id, release, target_disk, manifest)
    _write(root / "install-evidence.json", (json.dumps(evidence, indent=2) + "\n").encode(),
           0o644, 0, 0)
    for label in reversed(LABELS):
        _run("/bin/launchctl", "bootstrap", "system", str(LAUNCHD / (label + ".plist")))


def parse(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--configure-profile", action="store_true")
    parser.add_argument("--operator")
    parser.add_argument("--operator-uid", type=int)
    parser.add_argument("--vm-bundle", type=Path)
    parser.add_argument("--talos-app", type=Path)
    parser.add_argument("--backend", type=Path)
    parser.add_argument("--disk", type=Path)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--handoff", type=Path, required=True)
    options = parser.parse_args(argv)
    if options.install == options.configure_profile:
        parser.error("choose exactly one of --install and --configure-profile")
    if options.install and any(getattr(options, name) is None for name in
                               ("operator", "operator_uid", "vm_bundle", "talos_app", "backend", "disk")):
        parser.error("--install requires operator, VM bundle, Talos app, backend and disk inputs")
    return options


if __name__ == "__main__":
    arguments = parse()
    install(arguments) if arguments.install else configure_profile(arguments)
