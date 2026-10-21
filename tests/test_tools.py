"""Konkrete Tool-Runner (ohne Gating — das prüft der Executor separat)."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from talos import tools
from talos.policy import ToolRequest

OWNER = 100000001


def test_read_file_returns_content(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("hallo", encoding="utf-8")
    assert tools.read_file(ToolRequest("read_file", OWNER, {"path": str(f)})) == "hallo"


def test_write_file_creates_and_writes(tmp_path: Path) -> None:
    f = tmp_path / "sub" / "b.txt"
    out = tools.write_file(ToolRequest("write_file", OWNER, {"path": str(f), "content": "xyz"}))
    assert f.read_text(encoding="utf-8") == "xyz"
    assert "3 Zeichen" in out


def test_file_tools_use_the_kernel_expanded_target(tmp_path: Path, monkeypatch) -> None:
    """Urteil, Snapshot und Runner muessen dieselbe Datei meinen."""
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(cwd)

    out = tools.write_file(
        ToolRequest("write_file", OWNER, {"path": "~/note.txt", "content": "sicher"})
    )

    target = home / "note.txt"
    assert target.read_text(encoding="utf-8") == "sicher"
    assert not (cwd / "~" / "note.txt").exists()
    assert tools.read_file(ToolRequest("read_file", OWNER, {"path": "$HOME/note.txt"})) == "sicher"
    assert str(target) in out


def test_write_file_keeps_symlink_and_existing_mode(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("alt", encoding="utf-8")
    target.chmod(0o750)
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    tools.write_file(ToolRequest("write_file", OWNER, {"path": str(link), "content": "neu"}))

    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "neu"
    assert stat.S_IMODE(target.stat().st_mode) == 0o750


def test_write_file_temp_stays_private_until_existing_metadata_is_applied(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "secret.txt"
    target.write_text("alt", encoding="utf-8")
    target.chmod(0o600)
    observed: list[tuple[int, str]] = []
    real_chmod = tools.os.chmod

    def inspect_temp(path, mode, *, dir_fd=None, follow_symlinks=True) -> None:
        if dir_fd is not None and str(path).endswith(".tmp"):
            temp = tmp_path / str(path)
            observed.append((stat.S_IMODE(temp.stat().st_mode), temp.read_text(encoding="utf-8")))
        real_chmod(path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(tools.os, "chmod", inspect_temp)
    tools.write_file(
        ToolRequest("write_file", OWNER, {"path": str(target), "content": "streng geheim"})
    )

    assert observed == [(0o600, "streng geheim")]
    assert target.read_text(encoding="utf-8") == "streng geheim"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_file_gives_changed_content_a_new_mtime(tmp_path: Path) -> None:
    target = tmp_path / "watched.txt"
    target.write_text("old", encoding="utf-8")
    old_time = 946_684_800
    os.utime(target, (old_time, old_time))

    tools.write_file(ToolRequest("write_file", OWNER, {"path": str(target), "content": "new"}))

    assert target.read_text(encoding="utf-8") == "new"
    assert target.stat().st_mtime > old_time


def test_write_file_refuses_metadata_it_cannot_preserve(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "acl.txt"
    target.write_text("alt", encoding="utf-8")
    monkeypatch.setattr(tools, "_has_extended_metadata", lambda _path: True)

    with pytest.raises(ValueError, match="ACL|extended metadata"):
        tools.write_file(ToolRequest("write_file", OWNER, {"path": str(target), "content": "neu"}))

    assert target.read_text(encoding="utf-8") == "alt"
    assert target.is_file()
    assert list(tmp_path.glob(".acl.txt.*.tmp")) == []


def test_write_file_refuses_acl_inherited_by_the_temp_inode(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "plain.txt"
    target.write_text("old", encoding="utf-8")
    probes = 0

    def inherited_on_temp(_fd: int) -> bool:
        nonlocal probes
        probes += 1
        return probes == 2

    monkeypatch.setattr(tools, "_has_extended_metadata", inherited_on_temp)

    with pytest.raises(ValueError, match="inherited ACL|extended metadata"):
        tools.write_file(ToolRequest("write_file", OWNER, {"path": str(target), "content": "new"}))

    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".plain.txt.*.tmp")) == []


def test_linux_metadata_probe_uses_inode_xattrs_not_bsd_ls(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "linux.txt"
    target.write_text("x", encoding="utf-8")
    fd = os.open(target, os.O_RDONLY)
    calls: list[int] = []
    try:
        monkeypatch.setattr(tools.sys, "platform", "linux")
        monkeypatch.setattr(
            tools.os, "listxattr", lambda received: calls.append(received) or [], raising=False
        )
        monkeypatch.setattr(
            tools.subprocess, "run",
            lambda *_args, **_kwargs: pytest.fail("Linux must not invoke BSD ls"),
        )

        assert tools._has_extended_metadata(fd) is False
        assert calls == [fd]
    finally:
        os.close(fd)


def test_write_file_replace_failure_keeps_old_content_and_cleans_temp(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "note.txt"
    target.write_text("vollstaendig alt", encoding="utf-8")

    def fail_replace(*_args, **_kwargs) -> None:
        raise OSError("simulierter rename-fehler")

    monkeypatch.setattr(tools.os, "replace", fail_replace)

    with pytest.raises(OSError, match="rename-fehler"):
        tools.write_file(
            ToolRequest("write_file", OWNER, {"path": str(target), "content": "vollstaendig neu"})
        )

    assert target.read_text(encoding="utf-8") == "vollstaendig alt"
    assert list(tmp_path.glob(".note.txt.*.tmp")) == []
    assert target.is_file()


def test_write_file_refuses_to_split_hardlinks(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    alias = tmp_path / "alias.txt"
    target.write_text("gemeinsam", encoding="utf-8")
    os.link(target, alias)

    with pytest.raises(ValueError, match="hardlink"):
        tools.write_file(ToolRequest("write_file", OWNER, {"path": str(target), "content": "neu"}))

    assert target.read_text(encoding="utf-8") == "gemeinsam"
    assert alias.read_text(encoding="utf-8") == "gemeinsam"
    assert target.stat().st_ino == alias.stat().st_ino


def test_write_file_does_not_gain_access_by_replacing_a_readonly_file(tmp_path: Path) -> None:
    target = tmp_path / "readonly.txt"
    target.write_text("geschuetzt", encoding="utf-8")
    target.chmod(0o444)

    with pytest.raises(PermissionError, match="not writable"):
        tools.write_file(ToolRequest("write_file", OWNER, {"path": str(target), "content": "neu"}))

    assert target.read_text(encoding="utf-8") == "geschuetzt"
    assert stat.S_IMODE(target.stat().st_mode) == 0o444
    assert list(tmp_path.glob(".readonly.txt.*.tmp")) == []


def test_write_file_refuses_special_files_before_opening_them(tmp_path: Path) -> None:
    target = tmp_path / "pipe"
    os.mkfifo(target)

    with pytest.raises(ValueError, match="regular file"):
        tools.write_file(ToolRequest("write_file", OWNER, {"path": str(target), "content": "x"}))

    assert target.exists()
    assert stat.S_ISFIFO(target.stat().st_mode)
    assert list(tmp_path.glob(".pipe.*.tmp")) == []


def test_run_shell_returns_rc_and_output() -> None:
    out = tools.run_shell(ToolRequest("run_shell", OWNER, {"command": "echo talos"}))
    assert out.startswith("rc=0")
    assert "talos" in out


def test_missing_required_arg_raises_clear_error() -> None:
    # Ein fehlendes Pflichtargument liefert eine brauchbare Meldung statt eines nackten
    # KeyError, der als kryptisches „error · 'path'" durchschlägt und einen Plan abbricht.
    faelle = (
        ("read_file", {}, "path"),
        ("write_file", {"content": "x"}, "path"),
        ("write_file", {"path": "/tmp/nichts"}, "content"),
        ("run_shell", {}, "command"),
    )
    for tool, args, wort in faelle:
        with pytest.raises(ValueError, match=wort):
            getattr(tools, tool)(ToolRequest(tool, OWNER, args))


def test_default_manifest_declares_three_tools() -> None:
    m = tools.default_manifest()
    assert m.get("read_file") is not None
    assert m.get("write_file") is not None
    assert m.get("run_shell") is not None
