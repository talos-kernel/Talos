from talos import __version__
from talos.runtime_facts import release_context
from talos.instructions import assemble_system_prompt


def test_running_version_is_present_without_changelog(tmp_path):
    text = release_context(tmp_path)
    assert f"this process is Talos {__version__}" in text
    assert "changelog unavailable" in text
    assert "not a live check" in text


def test_shipped_notes_exclude_unreleased_and_are_bounded(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(
        f"## [Unreleased]\nnot-shipped\n## [{__version__}]\nreal-change\n" + "x" * 6000 + "\n## [0.20.0-beta.8]\nolder changes")
    text = release_context(tmp_path)
    assert "not-shipped" not in text
    assert "real-change" in text
    assert "0.20.0-beta.8" in text
    assert "read that file" in text
    assert len(text) < 5000


def test_shared_reasoner_prompt_contains_actual_software_version():
    text = assemble_system_prompt(tool_protocol="protocol", plan_protocol="plan")
    assert f"this process is Talos {__version__}" in text
    assert "alias does not prove" in text
