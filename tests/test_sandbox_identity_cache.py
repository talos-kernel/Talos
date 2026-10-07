"""A long-running worker must recover when its temporary identity files disappear."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from talos import sandbox


@pytest.fixture
def identity_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "_IDENTITY_CACHE", None)
    original_mkdtemp = sandbox.tempfile.mkdtemp
    monkeypatch.setattr(
        sandbox.tempfile, "mkdtemp",
        lambda **kwargs: original_mkdtemp(dir=tmp_path, **kwargs),
    )
    monkeypatch.setattr(
        sandbox, "_identity_line",
        lambda path, number: (
            f"operator:x:{number}:{os.getgid()}::/nonexistent:/bin/false\n"
            if path == "/etc/passwd" else f"operator:x:{number}:\n"
        ),
    )
    return sandbox._identity_files


def test_valid_identity_cache_is_reused(identity_cache):
    first = identity_cache()
    assert first is not None
    assert identity_cache() == first


@pytest.mark.parametrize("missing", ["passwd", "group", "directory"])
def test_missing_identity_cache_is_recreated(identity_cache, missing):
    first = identity_cache()
    assert first is not None
    directory = Path(first[0]).parent
    if missing == "directory":
        shutil.rmtree(directory)
    else:
        (directory / missing).unlink()

    second = identity_cache()

    assert second is not None
    assert second != first
    assert all(Path(path).is_file() for path in second)
    assert Path(second[0]).read_text().count("\n") == 1
    assert Path(second[0]).parent.stat().st_mode & 0o777 == 0o700
    assert all(Path(path).stat().st_mode & 0o777 == 0o444 for path in second)


def test_missing_cache_does_not_fall_back_to_real_identity_files(identity_cache, monkeypatch):
    first = identity_cache()
    assert first is not None
    Path(first[0]).unlink()

    def unavailable(**kwargs):
        raise PermissionError("temporary directory unavailable")

    monkeypatch.setattr(sandbox.tempfile, "mkdtemp", unavailable)
    assert identity_cache() is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bubblewrap is Linux-only")
def test_real_confined_start_recovers_after_identity_cleanup(tmp_path, monkeypatch):
    backend = sandbox.BubblewrapSandbox()
    if not backend.available():
        pytest.skip("bubblewrap unavailable")
    monkeypatch.setattr(sandbox, "_IDENTITY_CACHE", None)
    first = sandbox._identity_files()
    if first is None:
        pytest.skip("no local identity entry")
    Path(first[0]).unlink()
    runner = sandbox.SandboxedShell(workspace=tmp_path, backend=backend)
    result = runner.run("id -un")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()
    assert result.backend == "bubblewrap"
    assert sandbox._IDENTITY_CACHE != first
