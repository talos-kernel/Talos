#!/usr/bin/env python3
"""Install the offline Omarchy Computer behind a dedicated macOS service account.

The installer is intentionally explicit: it consumes a verified Try Omarchy app,
an already-provisioned guest disk and a Talos app/source tree. It never downloads,
enables networking, mounts host folders or accepts credentials on its command line.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import time

SERVICE_USER = "talosomarchyd"
SERVICE_GROUP = "talosomarchyd"
CLIENT_GROUP = "talosomarchyclients"
ROOT = Path("/Library/Application Support/TalosOmarchy")
LAUNCHD = Path("/Library/LaunchDaemons")
LABELS = ("org.talos.omarchy.web", "org.talos.omarchy.api", "org.talos.omarchy.vm")
PROFILE_KEYS = (
    "TALOS_COMPUTER_BACKEND", "TALOS_COMPUTER_DESKTOP",
    "TALOS_COMPUTER_OWNER_SHA256", "TALOS_COMPUTER_ROOT",
    "TALOS_COMPUTER_SOCKET", "TALOS_COMPUTER_VIEW_KEY_FILE",
    "TALOS_COMPUTER_VIEW_URL",
)


def _run(*argv: str, capture=False, check=True):
    return subprocess.run(argv, check=check, text=True,
                          capture_output=capture)


def _record(path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"required regular file is missing: {path}")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def qemu_arguments(root: Path, manifest: dict) -> list[str]:
    commandline = " ".join(
        token for token in manifest["kernelCommandLine"].split()
        if not token.startswith("mitigations=")
    )
    vm = root / "vm"
    release = root / "runtime/current"
    return [
        str(release / "omarchy/bin/Try Omarchy"),
        "-name", "Talos Omarchy - OFFLINE",
        "-machine", "virt,accel=hvf,gic-version=3",
        "-cpu", "host,pmu=off", "-smp", "4", "-m", "8192M",
        "-nodefaults", "-nic", "none", "-serial", "none", "-monitor", "none",
        "-qmp", f"unix:{vm / 'qmp.sock'},server=on,wait=off",
        "-pidfile", str(vm / "qemu.pid"),
        "-action", "reboot=reset,shutdown=poweroff",
        "-kernel", str(release / "guest/vmlinuz-linux"),
        "-initrd", str(release / "guest/initramfs-linux.img"),
        "-append", commandline + " omarchy.qemu_virgl=1",
        "-drive", f"if=none,id=root,file={vm / 'rootfs.ext4'},format=raw,cache=writeback",
        "-device", "virtio-blk-pci,drive=root,serial=talos-omarchy-root",
        "-device", "virtio-gpu-pci,max_outputs=1,xres=1440,yres=900,romfile=",
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
                   owner: str, secret: str) -> dict:
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
        "ProgramArguments": [python, "-m", "talos.computer.omarchy_service"],
        "EnvironmentVariables": {
            "HOME": "/var/empty", "LANG": "en_US.UTF-8",
            "PYTHONPATH": str(release / "backend"), "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "TALOS_OMARCHY_CONFIG": str(root / "config.json"),
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
        raise SystemExit("partial Talos Omarchy service identity exists; refusing to repair it")
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
                ("RealName", "Talos Omarchy Service"),
                ("PrimaryGroupID", str(identity)), ("Password", "*"),
            ):
                _run("/usr/bin/dscl", ".", "-create", group_path, attribute, value)
            _run("/usr/bin/dscl", ".", "-create", user_path)
            for attribute, value in (
                ("RealName", "Talos Omarchy Service"), ("UniqueID", str(identity)),
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
             "RealName", "Talos Omarchy Clients")
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
        raise SystemExit("Omarchy installation must run as root")
    operator = pwd.getpwuid(options.operator_uid)
    if operator.pw_name != options.operator:
        raise SystemExit("operator name and UID do not match")
    service_uid, service_gid = _ensure_service_identity()
    client_gid = _ensure_client_group(operator.pw_name)

    omarchy = options.omarchy_app.resolve(strict=True) / "Contents/Resources"
    talos = options.talos_app.resolve(strict=True) / "Contents/Resources"
    backend = options.backend.resolve(strict=True)
    disk = options.disk.resolve(strict=True)
    profile = Path(operator.pw_dir) / "Library/Application Support/Talos"
    if options.profile != profile:
        raise SystemExit("profile path does not match the operator account")
    if disk.is_symlink() or not disk.is_file() or disk.stat().st_size != 25_769_803_776:
        raise SystemExit("guest disk is missing or has unexpected size")
    _run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(options.omarchy_app.resolve()))
    _run("/usr/sbin/spctl", "--assess", "--type", "execute", str(options.omarchy_app.resolve()))
    _run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(options.talos_app.resolve()))
    if not (talos / "python/bin/python3").exists() or not (backend / "talos").is_dir():
        raise SystemExit("Talos Python runtime or backend is missing")

    for label in LABELS:
        _run("/bin/launchctl", "bootout", "system/" + label, check=False)
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
    _copytree(omarchy / "runtime", release / "omarchy")
    (release / "guest").mkdir(mode=0o755)
    for name in ("vmlinuz-linux", "initramfs-linux.img", "launch.plist", "LICENSE.omarchy"):
        shutil.copy2(omarchy / "guest" / name, release / "guest" / name)
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
    _run("/usr/bin/codesign", "--verify", "--strict",
         str(release / "omarchy/bin/Try Omarchy"))
    current = root / "runtime/current"
    next_current = root / "runtime" / (".current-" + release_id)
    next_current.symlink_to(release.name)
    os.lchown(next_current, 0, service_gid)
    os.replace(next_current, current)

    target_disk = root / "vm/rootfs.ext4"
    if not target_disk.exists():
        _run("/bin/cp", "-c", str(disk), str(target_disk))
    if target_disk.is_symlink() or target_disk.stat().st_size != disk.stat().st_size:
        raise SystemExit("installed guest disk failed validation")
    os.chown(target_disk, service_uid, service_gid)
    os.chmod(target_disk, 0o600)

    secret = secrets.token_urlsafe(32)
    owner = hashlib.sha256(f"cli:{options.operator_uid}".encode()).hexdigest()
    config = service_config(root, agent_uid=options.operator_uid, client_gid=client_gid,
                            owner=owner, secret=secret)
    _write(root / "config.json", (json.dumps(config, indent=2) + "\n").encode(),
           0o640, 0, service_gid)
    with (release / "guest/launch.plist").open("rb") as stream:
        manifest = plistlib.load(stream)
    plists = launch_plists(root)
    plists[LABELS[2]]["ProgramArguments"] = qemu_arguments(root, manifest)
    for label, value in plists.items():
        target = LAUNCHD / (label + ".plist")
        _write(target, plistlib.dumps(value, fmt=plistlib.FMT_XML), 0o644, 0, 0)
        _run("/usr/bin/plutil", "-lint", str(target))

    key, link = profile / "omarchy-view.key", profile / "computer.url"
    profile_values = {
        "TALOS_COMPUTER_BACKEND": "omarchy", "TALOS_COMPUTER_DESKTOP": "1",
        "TALOS_COMPUTER_OWNER_SHA256": owner,
        "TALOS_COMPUTER_ROOT": str(root / "state"),
        "TALOS_COMPUTER_SOCKET": config["control_socket"],
        "TALOS_COMPUTER_VIEW_KEY_FILE": str(key),
        "TALOS_COMPUTER_VIEW_URL": config["view_url"],
    }
    if options.handoff.exists():
        raise SystemExit("refusing an existing profile handoff")
    _write(options.handoff, (json.dumps({
        "profile": str(profile), "key": str(key), "link": str(link),
        "secret": secret, "values": profile_values,
    }) + "\n").encode(), 0o600, options.operator_uid, operator.pw_gid)

    evidence = {
        "release": release_id, "offline": True, "network": "none",
        "host_shares": False, "clipboard": False,
        "kernel": _record(release / "guest/vmlinuz-linux"),
        "initramfs": _record(release / "guest/initramfs-linux.img"),
        "disk_bytes": target_disk.stat().st_size,
    }
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
    parser.add_argument("--omarchy-app", type=Path)
    parser.add_argument("--talos-app", type=Path)
    parser.add_argument("--backend", type=Path)
    parser.add_argument("--disk", type=Path)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--handoff", type=Path, required=True)
    options = parser.parse_args(argv)
    if options.install == options.configure_profile:
        parser.error("choose exactly one of --install and --configure-profile")
    if options.install and any(getattr(options, name) is None for name in
                               ("operator", "operator_uid", "omarchy_app", "talos_app", "backend", "disk")):
        parser.error("--install requires operator, app, backend and disk inputs")
    return options


if __name__ == "__main__":
    arguments = parse()
    install(arguments) if arguments.install else configure_profile(arguments)
