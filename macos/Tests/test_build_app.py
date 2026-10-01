"""Build inputs must be versioned, complete and attributable without local paths."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import build_app


@pytest.fixture
def source(tmp_path, monkeypatch):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'macos').mkdir()
    for name in build_app.HELPERS:
        (tmp_path / 'macos' / name).write_text('# public helper\n')
    (tmp_path / 'macos/app-version.json').write_text('{"version":"0.3.3","build":"6"}')
    subprocess.run(['git', 'add', '.'], cwd=tmp_path, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                    'commit', '-qm', 'fixture'], cwd=tmp_path, check=True)
    monkeypatch.setattr(build_app, 'ROOT', tmp_path)
    return tmp_path


def test_metadata_is_explicit_and_validated(source):
    assert build_app.app_version() == {'version': '0.3.3', 'build': '6'}
    (source / 'macos/app-version.json').write_text('{"version":"latest","build":"0"}')
    with pytest.raises(ValueError):
        build_app.app_version()


def test_clean_source_manifest_has_helpers_and_no_machine_paths(source):
    record = build_app.source_record()
    assert record['dirty'] is False
    assert all('macos/' + name in record['inputs'] for name in build_app.HELPERS)
    assert str(source) not in json.dumps(record)
    assert 'macos/app-version.json' in record['inputs']


def test_dirty_source_is_refused_or_explicitly_labelled(source):
    (source / 'macos/desktop_accounts.py').write_text('# changed\n')
    with pytest.raises(SystemExit, match='Commit'):
        build_app.source_record()
    assert build_app.source_record(allow_dirty=True)['dirty'] is True
    (source / 'macos/desktop_accounts.py').write_text('# public helper\n')
    # SwiftPM can compile an untracked source; a clean provenance claim must refuse it.
    (source / 'macos/extra.swift').write_text('// untracked source\n')
    with pytest.raises(SystemExit, match='Commit'):
        build_app.source_record()


def test_missing_helper_is_not_silently_omitted(source):
    subprocess.run(['git', 'rm', '-q', 'macos/desktop_accounts.py'], cwd=source, check=True)
    with pytest.raises(ValueError, match='missing tracked'):
        build_app.source_record(allow_dirty=True)
