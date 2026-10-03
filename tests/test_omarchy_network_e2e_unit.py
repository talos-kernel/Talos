"""Unit checks for the installed Omarchy network proof helpers."""
import importlib.util
from pathlib import Path
import subprocess

import pytest


spec = importlib.util.spec_from_file_location(
    "omarchy_network_e2e", Path(__file__).with_name("omarchy_network_e2e.py"))
network_e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(network_e2e)


def test_marker_count_tolerates_vision_o_zero_ambiguity_only():
    assert network_e2e.marker_occurrences(
        "TALOSNET0K1234\nTALOSNETOK1234", "TALOSNETOK1234") == 2
    assert network_e2e.marker_occurrences("TALOSNETQK1234", "TALOSNETOK1234") == 0


def test_retired_launchd_helper_exact_absence_is_accepted(monkeypatch):
    label = "org.talos.omarchy.network"

    def run(argv, **kwargs):
        assert argv == ["/bin/launchctl", "print", "system/" + label]
        assert kwargs == {"check": False, "capture_output": True, "text": True}
        return subprocess.CompletedProcess(
            argv, 113, "",
            "Bad request.\n"
            f'Could not find service "{label}" in domain for system\n')

    monkeypatch.setattr(network_e2e.subprocess, "run", run)
    assert network_e2e.launchd_label_is_absent(label)


def test_retired_launchd_helper_generic_launchctl_failure_is_rejected(monkeypatch):
    monkeypatch.setattr(
        network_e2e.subprocess, "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, "", "launchctl query failed: I/O error\n"))

    assert not network_e2e.launchd_label_is_absent("org.talos.omarchy.network")


def test_retired_launchd_helper_loaded_is_rejected(monkeypatch):
    monkeypatch.setattr(
        network_e2e.subprocess, "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, "pid = 4321\n", ""))

    assert not network_e2e.launchd_label_is_absent("org.talos.omarchy.network")


def test_tcp_listeners_accepts_successful_empty_result(monkeypatch):
    def run(argv, **kwargs):
        assert argv == ["/usr/sbin/lsof", "-Pan", "-p", "4321"]
        assert kwargs == {"check": True, "capture_output": True, "text": True}
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(network_e2e.subprocess, "run", run)
    assert network_e2e.tcp_listeners(4321) == ""


def test_tcp_listeners_command_failure_fails_e2e(monkeypatch):
    failure = subprocess.CalledProcessError(
        1, ["/usr/sbin/lsof"], stderr="permission denied")

    def run(argv, **kwargs):
        raise failure

    monkeypatch.setattr(network_e2e.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError) as raised:
        network_e2e.tcp_listeners(4321)
    assert raised.value is failure
