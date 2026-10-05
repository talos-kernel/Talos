"""The privileged QMP VM installer has a deterministic, outbound-only launch plan."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location(
    "qmp_install", Path(__file__).parents[1] / "qmp_vm_install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def vm_bundle(tmp_path, **changes):
    app = tmp_path / "Verification VM.app"
    resources = app / "Contents/Resources"
    runtime = resources / "runtime"
    guest = resources / "guest"
    qemu = runtime / "bin/qemu-system-x86_64"
    kernel = guest / "boot/vmlinuz"
    initrd = guest / "boot/initrd.img"
    qemu.parent.mkdir(parents=True)
    kernel.parent.mkdir(parents=True)
    qemu.write_bytes(b"arm64-host-qemu-x86-target")
    qemu.chmod(0o755)
    kernel.write_bytes(b"x86-kernel")
    initrd.write_bytes(b"x86-initrd")
    manifest = {
        "schema": "talos.qmp-vm/v1",
        "guestArchitecture": "x86_64",
        "guestDistribution": "debian",
        "guestDesktop": "hyprland",
        "geometry": [1440, 900],
        "qemuBinary": "bin/qemu-system-x86_64",
        "qemuSha256": sha256(qemu),
        "qemuMachine": "pc-q35-9.2",
        "qemuCpu": "max",
        "kernel": "boot/vmlinuz",
        "kernelSha256": sha256(kernel),
        "initrd": "boot/initrd.img",
        "initrdSha256": sha256(initrd),
        "kernelCommandLine": "root=/dev/vda rw console=hvc0",
        "diskFormat": "raw",
        "diskBytes": 8,
        "diskSha256": hashlib.sha256(b"diskdata").hexdigest(),
        "pciDevices": [[0x1B36, 0x0008], [0x1AF4, 0x1000]],
    }
    manifest.update(changes)
    (guest / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return app, resources, manifest


def directory_service(initial=False):
    user = f"/Users/{installer.SERVICE_USER}"
    group = f"/Groups/{installer.SERVICE_GROUP}"
    records = {}
    if initial:
        records = {
            user: {"RealName": "Talos QMP VM Service", "UniqueID": "504",
                   "PrimaryGroupID": "504", "UserShell": "/usr/bin/false",
                   "NFSHomeDirectory": "/var/empty", "IsHidden": "1", "Password": "*"},
            group: {"RealName": "Talos QMP VM Service", "PrimaryGroupID": "504",
                    "Password": "*"},
        }

    def run(*argv, capture=False, check=True):
        assert argv[:2] == ("/usr/bin/dscl", ".")
        operation, path = argv[2:4]
        if operation == "-read":
            if len(argv) == 4:
                return SimpleNamespace(returncode=0 if path in records else 1, stdout="")
            if path not in records or argv[4] not in records[path]:
                if check:
                    raise RuntimeError("missing directory attribute")
                return SimpleNamespace(returncode=1, stdout="")
            label = "dsAttrTypeNative:IsHidden" if argv[4] == "IsHidden" else argv[4]
            return SimpleNamespace(returncode=0,
                                   stdout=f"{label}: {records[path][argv[4]]}\n")
        if operation == "-list":
            assert path in {"/Users", "/Groups"}
            return SimpleNamespace(returncode=0, stdout="operator 501\nwheel 0\n")
        if operation == "-create":
            records.setdefault(path, {})
            if len(argv) > 4:
                records[path][argv[4]] = argv[5]
            return SimpleNamespace(returncode=0, stdout="")
        if operation == "-delete":
            records.pop(path, None)
            return SimpleNamespace(returncode=0, stdout="")
        raise AssertionError(argv)

    return run, records


def test_qemu_plan_is_exact_x86_tcg_with_outbound_nat(tmp_path):
    _, resources, _ = vm_bundle(tmp_path, diskFormat="qcow2")
    manifest = installer.load_bundle_manifest(resources)
    args = installer.qemu_arguments(tmp_path, manifest)
    release = tmp_path / "runtime/current"
    assert args[0] == str(release / "vm-runtime/bin/qemu-system-x86_64")
    assert args[args.index("-machine") + 1] == "pc-q35-9.2"
    assert args[args.index("-accel") + 1] == "tcg,thread=multi"
    assert args[args.index("-cpu") + 1] == "max"
    assert args[args.index("-netdev") + 1] == "user,id=talos-qmp-net,ipv6=off"
    network = args[args.index("-device") + 1]
    assert network == ("virtio-net-pci,id=talos-qmp-nic,"
                       "netdev=talos-qmp-net,mac=52:54:00:12:34:56,romfile=")
    assert args[args.index("-display") + 1] == "none"
    assert "-nodefaults" in args and "-no-user-config" in args
    assert args[args.index("-kernel") + 1] == str(release / "guest/boot/vmlinuz")
    assert args[args.index("-initrd") + 1] == str(release / "guest/boot/initrd.img")
    assert args[args.index("-append") + 1] == manifest["kernelCommandLine"]
    assert "format=qcow2" in args[args.index("-drive") + 1]
    assert "xres=1440,yres=900" in " ".join(args)
    text = " ".join(args)
    for forbidden in ("hostfwd", "tap", "bridge", "9p", "fsdev", "virtserialport",
                      "clipboard", "usb-host", "hvf", "gic-version", "host,pmu",
                      "qemu_virgl"):
        assert forbidden not in text
    assert "qmp.sock" in args[args.index("-qmp") + 1]
    assert str(tmp_path / "vm/rootfs.ext4") in text


def test_service_config_and_plists_keep_roles_separate(tmp_path):
    _, resources, _ = vm_bundle(tmp_path)
    manifest = installer.load_bundle_manifest(resources)
    config = installer.service_config(
        tmp_path, agent_uid=501, client_gid=505, owner="a" * 64,
        secret="s" * 43, manifest=manifest)
    assert config["view_url"] == "http://127.0.0.1:8830"
    assert config["agent_uid"] == 501 and config["client_gid"] == 505
    assert config["disk"].startswith(str(tmp_path / "vm"))
    assert {key: config[key] for key in (
        "architecture", "distribution", "desktop", "geometry", "pci_devices"
    )} == {
        "architecture": "x86_64", "distribution": "debian",
        "desktop": "hyprland", "geometry": [1440, 900],
        "pci_devices": manifest["pciDevices"],
    }
    plists = installer.launch_plists(tmp_path)
    assert set(plists) == set(installer.LABELS)
    for value in plists.values():
        assert value["UserName"] == "talosqmpd"
        assert value["GroupName"] == "talosqmpd"
        assert value["Umask"] == 63
    api = plists["org.talos.qmp.api"]
    assert api["EnvironmentVariables"]["PYTHONNOUSERSITE"] == "1"
    assert api["ProgramArguments"][-1] == "talos.computer.qmp_service"
    assert installer.RETIRED_LABELS == ("org.talos.qmp.network",)


def test_manifest_requires_exact_fields(tmp_path):
    _, resources, manifest = vm_bundle(tmp_path)
    manifest["unknown"] = True
    (resources / "guest/manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SystemExit, match="unknown or missing"):
        installer.load_bundle_manifest(resources)
    manifest.pop("unknown")
    manifest.pop("guestDesktop")
    (resources / "guest/manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SystemExit, match="unknown or missing"):
        installer.load_bundle_manifest(resources)


def test_manifest_rejects_duplicate_json_keys(tmp_path):
    _, resources, manifest = vm_bundle(tmp_path)
    body = json.dumps(manifest)
    duplicate = body[:-1] + ',"schema":"talos.qmp-vm/v1"}'
    (resources / "guest/manifest.json").write_text(duplicate)
    with pytest.raises(SystemExit, match="duplicate"):
        installer.load_bundle_manifest(resources)


@pytest.mark.parametrize(("field", "value"), [
    ("schema", "talos.qmp-vm/v2"),
    ("guestArchitecture", "aarch64"),
    ("guestDistribution", "ubuntu"),
    ("guestDesktop", "gnome"),
    ("geometry", [1920, 1080]),
    ("qemuMachine", "q35"),
    ("qemuMachine", "virt"),
    ("qemuCpu", "host"),
    ("kernelCommandLine", ""),
    ("kernelCommandLine", "root=/dev/vda\nconsole=hvc0"),
    ("diskFormat", "vmdk"),
    ("diskBytes", 0),
    ("diskBytes", True),
    ("pciDevices", []),
    ("pciDevices", [[0x1AF4, True]]),
    ("pciDevices", [[0x10000, 1]]),
    ("pciDevices", [[1, 2]] * 33),
])
def test_manifest_rejects_wrong_contract_values(tmp_path, field, value):
    _, resources, _ = vm_bundle(tmp_path, **{field: value})
    with pytest.raises(SystemExit, match="manifest"):
        installer.load_bundle_manifest(resources)


@pytest.mark.parametrize(("field", "value"), [
    ("qemuBinary", "../qemu-system-x86_64"),
    ("qemuBinary", "/tmp/qemu-system-x86_64"),
    ("qemuBinary", "bin/qemu-system-aarch64"),
    ("kernel", "../vmlinuz"),
    ("initrd", "boot/../../initrd.img"),
])
def test_manifest_rejects_traversal_and_wrong_target_path(tmp_path, field, value):
    _, resources, _ = vm_bundle(tmp_path, **{field: value})
    with pytest.raises(SystemExit, match="path|x86_64"):
        installer.load_bundle_manifest(resources)


def test_manifest_rejects_declared_symlink(tmp_path):
    _, resources, manifest = vm_bundle(tmp_path)
    kernel = resources / "guest/boot/vmlinuz"
    outside = tmp_path / "outside-kernel"
    outside.write_bytes(b"x86-kernel")
    kernel.unlink()
    kernel.symlink_to(outside)
    manifest["kernelSha256"] = sha256(outside)
    (resources / "guest/manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SystemExit, match="symbolic link"):
        installer.load_bundle_manifest(resources)


@pytest.mark.parametrize("field", ["qemuSha256", "kernelSha256", "initrdSha256"])
def test_manifest_rejects_declared_file_hash_mismatch(tmp_path, field):
    _, resources, _ = vm_bundle(tmp_path, **{field: "0" * 64})
    with pytest.raises(SystemExit, match="hash"):
        installer.load_bundle_manifest(resources)


def test_bundle_copy_preserves_runtime_support_and_reverifies_files(tmp_path):
    _, resources, _ = vm_bundle(tmp_path)
    support = resources / "runtime/lib/libsupport.dylib"
    support.parent.mkdir()
    support.write_bytes(b"support")
    (support.parent / "libcurrent.dylib").symlink_to("libsupport.dylib")
    manifest = installer.load_bundle_manifest(resources)
    release = tmp_path / "release"
    installer.copy_bundle_resources(resources, release, manifest)
    copied_link = release / "vm-runtime/lib/libcurrent.dylib"
    assert copied_link.is_symlink()
    assert copied_link.readlink() == Path("libsupport.dylib")
    assert installer.verify_declared_files(
        release / "vm-runtime", release / "guest", manifest)


def test_same_size_wrong_installed_disk_is_refused(tmp_path):
    source = tmp_path / "source.img"
    installed = tmp_path / "installed.img"
    source.write_bytes(b"diskdata")
    installed.write_bytes(b"wrong!!!")
    manifest = {"diskBytes": 8, "diskSha256": sha256(source)}
    with pytest.raises(SystemExit, match="installed guest disk hash"):
        installer.install_disk(source, installed, manifest, uid=501, gid=505)
    assert installed.read_bytes() == b"wrong!!!"


def test_disk_copy_verifies_source_and_installed_hash(tmp_path, monkeypatch):
    source = tmp_path / "source.img"
    installed = tmp_path / "installed.img"
    source.write_bytes(b"diskdata")
    manifest = {"diskBytes": 8, "diskSha256": sha256(source)}

    def run(*argv, **kwargs):
        assert argv[:2] == ("/bin/cp", "-c")
        shutil.copyfile(argv[2], argv[3])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(installer, "_run", run)
    monkeypatch.setattr(installer.os, "chown", lambda *args: None)
    monkeypatch.setattr(installer.os, "chmod", lambda *args: None)
    installer.install_disk(source, installed, manifest, uid=501, gid=505)
    assert installed.read_bytes() == source.read_bytes()

    installed.unlink()
    source.write_bytes(b"wrong!!!")
    with pytest.raises(SystemExit, match="source guest disk hash"):
        installer.install_disk(source, installed, manifest, uid=501, gid=505)
    assert not installed.exists()


def test_install_evidence_binds_complete_contract(tmp_path):
    _, resources, _ = vm_bundle(tmp_path)
    manifest = installer.load_bundle_manifest(resources)
    release = tmp_path / "release"
    installer.copy_bundle_resources(resources, release, manifest)
    disk = tmp_path / "rootfs.ext4"
    disk.write_bytes(b"diskdata")
    evidence = installer.install_evidence("release-id", release, disk, manifest)
    assert evidence["manifest"]["sha256"] == sha256(release / "guest/manifest.json")
    assert evidence["qemu"]["sha256"] == manifest["qemuSha256"]
    assert evidence["kernel"]["sha256"] == manifest["kernelSha256"]
    assert evidence["initrd"]["sha256"] == manifest["initrdSha256"]
    assert evidence["disk"] == {
        "path": str(disk), "sha256": manifest["diskSha256"],
        "bytes": 8, "format": "raw",
    }
    assert evidence["machine"] == "pc-q35-9.2"
    assert evidence["accelerator"] == "tcg,thread=multi"
    assert evidence["cpu"] == "max"
    assert evidence["architecture"] == "x86_64"
    assert evidence["distribution"] == "debian"
    assert evidence["desktop"] == "hyprland"
    assert evidence["geometry"] == [1440, 900]


def test_parser_uses_vm_bundle_not_qmp_app(tmp_path):
    common = [
        "--install", "--operator", "operator", "--operator-uid", "501",
        "--talos-app", "Talos.app", "--backend", "backend", "--disk", "disk.img",
        "--profile", str(tmp_path / "profile"), "--handoff", str(tmp_path / "handoff"),
    ]
    options = installer.parse([*common, "--vm-bundle", "Verifier.app"])
    assert options.vm_bundle == Path("Verifier.app")
    with pytest.raises(SystemExit):
        installer.parse([*common, "--qmp-app", "legacy.app"])


def test_bundle_signature_is_checked_before_identity_or_state(tmp_path, monkeypatch):
    app, _, _ = vm_bundle(tmp_path)
    talos_app = tmp_path / "Talos.app"
    (talos_app / "Contents/Resources").mkdir(parents=True)
    backend = tmp_path / "backend"
    backend.mkdir()
    disk = tmp_path / "disk.img"
    disk.write_bytes(b"diskdata")
    home = tmp_path / "home"
    profile = home / "Library/Application Support/Talos"
    install_root = tmp_path / "install-root"
    options = SimpleNamespace(
        operator="operator", operator_uid=501, vm_bundle=app, talos_app=talos_app,
        backend=backend, disk=disk, profile=profile, handoff=tmp_path / "handoff.json",
    )
    identity_calls = []

    def run(*argv, **kwargs):
        assert argv == ("/usr/bin/codesign", "--verify", "--deep", "--strict", str(app))
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.pwd, "getpwuid", lambda uid: SimpleNamespace(
        pw_name="operator", pw_dir=str(home), pw_gid=20))
    monkeypatch.setattr(installer, "ROOT", install_root)
    monkeypatch.setattr(installer, "_run", run)
    monkeypatch.setattr(installer, "_ensure_service_identity",
                        lambda: identity_calls.append(True))
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(options)
    assert identity_calls == []
    assert not install_root.exists()


def test_retired_helper_not_loaded_is_verified_before_plist_removal(tmp_path, monkeypatch):
    label = installer.RETIRED_LABELS[0]
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    plist = launchd / f"{label}.plist"
    plist.write_text("retired")
    calls = []

    def run(*argv, capture=False, check=True):
        calls.append((argv, capture, check))
        if argv[1] == "bootout":
            return SimpleNamespace(returncode=3, stdout="",
                                   stderr="Boot-out failed: 3: No such process\n")
        assert argv == ("/bin/launchctl", "print", "system/" + label)
        return SimpleNamespace(
            returncode=113, stdout="",
            stderr=("Bad request.\n"
                    f'Could not find service "{label}" in domain for system\n'))

    monkeypatch.setattr(installer, "LAUNCHD", launchd)
    monkeypatch.setattr(installer, "_run", run)
    installer._remove_retired_launchd_services()

    assert not plist.exists()
    assert calls == [
        (("/bin/launchctl", "bootout", "system/" + label), True, False),
        (("/bin/launchctl", "print", "system/" + label), True, False),
    ]


def test_retired_helper_genuine_bootout_failure_keeps_plist(tmp_path, monkeypatch):
    label = installer.RETIRED_LABELS[0]
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    plist = launchd / f"{label}.plist"
    plist.write_text("retired")
    calls = []

    def run(*argv, capture=False, check=True):
        calls.append((argv, capture, check))
        if argv[1] == "bootout":
            return SimpleNamespace(returncode=5, stdout="",
                                   stderr="Boot-out failed: 5: Input/output error\n")
        assert argv == ("/bin/launchctl", "print", "system/" + label)
        return SimpleNamespace(returncode=0, stdout="loaded service state\n", stderr="")

    monkeypatch.setattr(installer, "LAUNCHD", launchd)
    monkeypatch.setattr(installer, "_run", run)
    with pytest.raises(SystemExit, match="failed to unload retired launchd service"):
        installer._remove_retired_launchd_services()

    assert plist.read_text() == "retired"
    assert calls == [
        (("/bin/launchctl", "bootout", "system/" + label), True, False),
        (("/bin/launchctl", "print", "system/" + label), True, False),
    ]


def test_retired_helper_successful_bootout_still_loaded_keeps_plist(tmp_path, monkeypatch):
    label = installer.RETIRED_LABELS[0]
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    plist = launchd / f"{label}.plist"
    plist.write_text("retired")
    calls = []

    def run(*argv, capture=False, check=True):
        calls.append((argv, capture, check))
        if argv[1] == "bootout":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        assert argv == ("/bin/launchctl", "print", "system/" + label)
        return SimpleNamespace(returncode=0, stdout="loaded service state\n", stderr="")

    monkeypatch.setattr(installer, "LAUNCHD", launchd)
    monkeypatch.setattr(installer, "_run", run)
    with pytest.raises(SystemExit, match="still reports the label loaded"):
        installer._remove_retired_launchd_services()

    assert plist.read_text() == "retired"
    assert calls == [
        (("/bin/launchctl", "bootout", "system/" + label), True, False),
        (("/bin/launchctl", "print", "system/" + label), True, False),
    ]


def test_retired_helper_successful_bootout_and_absence_removes_plist(tmp_path, monkeypatch):
    label = installer.RETIRED_LABELS[0]
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    plist = launchd / f"{label}.plist"
    plist.write_text("retired")
    calls = []

    def run(*argv, capture=False, check=True):
        calls.append((argv, capture, check))
        if argv[1] == "bootout":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        assert argv == ("/bin/launchctl", "print", "system/" + label)
        return SimpleNamespace(
            returncode=113, stdout="",
            stderr=("Bad request.\n"
                    f'Could not find service "{label}" in domain for system\n'))

    monkeypatch.setattr(installer, "LAUNCHD", launchd)
    monkeypatch.setattr(installer, "_run", run)
    installer._remove_retired_launchd_services()

    assert not plist.exists()
    assert calls == [
        (("/bin/launchctl", "bootout", "system/" + label), True, False),
        (("/bin/launchctl", "print", "system/" + label), True, False),
    ]


def test_service_identity_is_created_hidden_without_login(monkeypatch):
    run, records = directory_service()
    monkeypatch.setattr(installer, "_run", run)
    assert installer._ensure_service_identity() == (400, 400)
    user = records[f"/Users/{installer.SERVICE_USER}"]
    assert user["PrimaryGroupID"] == "400"
    assert user["UserShell"] == "/usr/bin/false"
    assert user["NFSHomeDirectory"] == "/var/empty"
    assert user["IsHidden"] == "1"


def test_existing_service_identity_must_remain_locked(monkeypatch):
    run, records = directory_service(initial=True)
    monkeypatch.setattr(installer, "_run", run)
    assert installer._ensure_service_identity() == (504, 504)
    records[f"/Users/{installer.SERVICE_USER}"]["UserShell"] = "/bin/zsh"
    with pytest.raises(SystemExit, match="not locked down"):
        installer._ensure_service_identity()


def test_profile_keys_are_an_exact_capability_allowlist():
    assert set(installer.PROFILE_KEYS) == {
        "TALOS_COMPUTER_BACKEND", "TALOS_COMPUTER_DESKTOP",
        "TALOS_COMPUTER_OWNER_SHA256", "TALOS_COMPUTER_ROOT",
        "TALOS_COMPUTER_SOCKET", "TALOS_COMPUTER_VIEW_KEY_FILE",
        "TALOS_COMPUTER_VIEW_URL",
    }


def test_profile_handoff_is_applied_only_by_the_operator(tmp_path, monkeypatch):
    profile = tmp_path / "Talos"
    profile.mkdir(mode=0o700)
    handoff = tmp_path / "handoff.json"
    secret = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
    values = {key: "value" for key in installer.PROFILE_KEYS}
    values["TALOS_COMPUTER_VIEW_URL"] = "http://127.0.0.1:8830"
    handoff.write_text(__import__("json").dumps({
        "profile": str(profile.resolve()), "key": str(profile / "qmp-view.key"),
        "link": str(profile / "computer.url"), "secret": secret, "values": values,
    }))
    handoff.chmod(0o600)
    options = type("Options", (), {"handoff": handoff, "profile": profile})()
    installer.configure_profile(options)
    assert not handoff.exists()
    assert (profile / "qmp-view.key").read_text().strip() == secret
    assert "#token=" + secret in (profile / "computer.url").read_text()
    assert set(line.partition("=")[0] for line in (profile / "talos.env").read_text().splitlines()) == set(installer.PROFILE_KEYS)
