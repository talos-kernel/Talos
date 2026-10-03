"""The privileged Omarchy installer has a deterministic, outbound-only launch plan."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location(
    "omarchy_install", Path(__file__).parents[1] / "omarchy_install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def directory_service(initial=False):
    user = f"/Users/{installer.SERVICE_USER}"
    group = f"/Groups/{installer.SERVICE_GROUP}"
    records = {}
    if initial:
        records = {
            user: {"RealName": "Talos Omarchy Service", "UniqueID": "504",
                   "PrimaryGroupID": "504", "UserShell": "/usr/bin/false",
                   "NFSHomeDirectory": "/var/empty", "IsHidden": "1", "Password": "*"},
            group: {"RealName": "Talos Omarchy Service", "PrimaryGroupID": "504",
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


def test_qemu_plan_has_outbound_nat_without_host_forward_or_bridge(tmp_path):
    args = installer.qemu_arguments(tmp_path, {
        "kernelCommandLine": "root=/dev/vda rw mitigations=off console=hvc0"})
    assert args[args.index("-netdev") + 1] == "user,id=talos-omarchy-net,ipv6=off"
    network = args[args.index("-device") + 1]
    assert network == ("virtio-net-pci,id=talos-omarchy-nic,"
                       "netdev=talos-omarchy-net,mac=52:54:00:12:34:56,romfile=")
    assert args[args.index("-display") + 1] == "none"
    assert "-nodefaults" in args
    text = " ".join(args)
    for forbidden in ("hostfwd", "tap", "bridge", "9p", "fsdev", "virtserialport",
                      "clipboard", "usb-host", "mitigations=off"):
        assert forbidden not in text
    assert "qmp.sock" in args[args.index("-qmp") + 1]
    assert str(tmp_path / "vm/rootfs.ext4") in text


def test_service_config_and_plists_keep_roles_separate(tmp_path):
    config = installer.service_config(
        tmp_path, agent_uid=501, client_gid=505, owner="a" * 64, secret="s" * 43)
    assert config["view_url"] == "http://127.0.0.1:8830"
    assert config["agent_uid"] == 501 and config["client_gid"] == 505
    assert config["disk"].startswith(str(tmp_path / "vm"))
    plists = installer.launch_plists(tmp_path)
    assert set(plists) == set(installer.LABELS)
    for value in plists.values():
        assert value["UserName"] == "talosomarchyd"
        assert value["GroupName"] == "talosomarchyd"
        assert value["Umask"] == 63
    api = plists["org.talos.omarchy.api"]
    assert api["EnvironmentVariables"]["PYTHONNOUSERSITE"] == "1"
    assert api["ProgramArguments"][-1] == "talos.computer.omarchy_service"
    assert installer.RETIRED_LABELS == ("org.talos.omarchy.network",)


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
        "profile": str(profile.resolve()), "key": str(profile / "omarchy-view.key"),
        "link": str(profile / "computer.url"), "secret": secret, "values": values,
    }))
    handoff.chmod(0o600)
    options = type("Options", (), {"handoff": handoff, "profile": profile})()
    installer.configure_profile(options)
    assert not handoff.exists()
    assert (profile / "omarchy-view.key").read_text().strip() == secret
    assert "#token=" + secret in (profile / "computer.url").read_text()
    assert set(line.partition("=")[0] for line in (profile / "talos.env").read_text().splitlines()) == set(installer.PROFILE_KEYS)
