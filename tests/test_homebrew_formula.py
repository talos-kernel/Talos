"""Release metadata cannot become Ruby; installs stay hash locked and per user."""
from pathlib import Path
import runpy
import subprocess
import shutil

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/render-homebrew.py"


def renderer():
    return runpy.run_path(str(SCRIPT))["render"]


def test_formula_is_offline_and_managed():
    text = renderer()(version="0.20.0-beta.5", source_url="https://example.org/talos.tar.gz",
                      source_sha256="a" * 64, wheels_url="https://example.org/wheels.tar.gz",
                      wheels_sha256="b" * 64)
    for required in ("--require-hashes", "--no-index", "--no-deps", "--isolated",
                     'depends_on arch: :arm64', 'depends_on macos: :sonoma',
                     '.talos-install-manager', '"homebrew"', 'write_env_script',
                     'PYTHONDONTWRITEBYTECODE', 'brew upgrade talos'):
        assert required in text
    assert "var/" not in text and "etc/" not in text
    assert "homebrew-core" not in text
    assert '(testpath/"elsewhere").to_s.shellescape' in text


@pytest.mark.skipif(shutil.which("ruby") is None, reason="Ruby unavailable; syntax verified by native Homebrew gate")
def test_formula_ruby_syntax(tmp_path):
    text = renderer()(version="0.20.0-beta.5", source_url="https://example.org/talos.tar.gz",
                      source_sha256="a" * 64, wheels_url="https://example.org/wheels.tar.gz",
                      wheels_sha256="b" * 64)
    path = tmp_path / "talos.rb"
    path.write_text(text)
    assert subprocess.run(["ruby", "-c", str(path)], capture_output=True).returncode == 0


@pytest.mark.parametrize("key,value", [
    ("source_url", 'https://example.org/#{system("id")}'),
    ("source_url", "file:///private/local.tar.gz"),
    ("wheels_url", "https://example.org/a\nend"),
    ("source_sha256", "a" * 63),
    ("wheels_sha256", '"; system("id")'),
    ("version", '0.1.0"; system("id")'),
])
def test_release_metadata_rejects_ruby_and_nonpublic_inputs(key, value):
    kwargs = dict(version="0.20.0-beta.5", source_url="https://example.org/source.tar.gz",
                  source_sha256="a" * 64, wheels_url="https://example.org/wheels.tar.gz",
                  wheels_sha256="b" * 64)
    kwargs[key] = value
    with pytest.raises(ValueError):
        renderer()(**kwargs)
