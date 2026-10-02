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


def test_python_bundle_paths_are_normalized(tmp_path):
    source_root = tmp_path / 'private-python'
    bundle = tmp_path / 'bundle'
    config = bundle / 'lib/python3.13/_sysconfigdata_test.py'
    config.parent.mkdir(parents=True)
    config.write_text("PREFIX = " + repr(str(source_root)) + "\n")
    packages = tmp_path / 'packages'
    (packages / 'bin').mkdir(parents=True)
    (packages / 'bin/tool').write_text('#!' + str(source_root / 'bin/python3') + '\n')
    build_app.scrub_python_paths(bundle, packages, source_root)
    assert not (packages / 'bin').exists()
    assert str(source_root) not in config.read_text()
    assert '/opt/talos/python' in config.read_text()


def test_bundle_hygiene_finds_paths_across_chunks(tmp_path):
    root = tmp_path / 'app'
    root.mkdir()
    private = tmp_path / 'operator-home'
    payload = root / 'payload'
    payload.write_bytes(b'x' * (1024 * 1024 - 3) + str(private).encode())
    with pytest.raises(SystemExit, match='payload'):
        build_app.assert_no_local_paths(root, (private,))
