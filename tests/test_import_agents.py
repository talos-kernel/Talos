from __future__ import annotations

import os
from pathlib import Path

import pytest

from talos import import_agents


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _hermes(root: Path) -> None:
    _write(root / "config.yaml", "provider: local\n")


def _openclaw(root: Path) -> None:
    _write(root / "openclaw.json", "not parsed by the importer")


def _skill(root: Path, name: str, *, description: str = "Portable guidance") -> Path:
    path = root / name / "SKILL.md"
    _write(
        path,
        f"---\nname: {name}\ndescription: {description}\n---\n\n# Instructions\nStay careful.\n",
    )
    return path


def _snapshot(root: Path) -> tuple[tuple[str, str, int, bytes], ...]:
    rows: list[tuple[str, str, int, bytes]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if path.is_symlink():
            rows.append((relative, "symlink", info.st_mode, os.readlink(path).encode()))
        elif path.is_file():
            rows.append((relative, "file", info.st_mode, path.read_bytes()))
        else:
            rows.append((relative, "directory", info.st_mode, b""))
    return tuple(rows)


def _items(plan: import_agents.ImportPlan, category: str) -> list[import_agents.ImportItem]:
    return [item for item in plan.items if item.category == category]


def test_hermes_plan_is_deterministic_versioned_and_writes_nothing(tmp_path: Path) -> None:
    source = tmp_path / "hermes"
    target = tmp_path / "talos"
    _hermes(source)
    _write(source / "SOUL.md", "# Identity\nHelpful and careful.\n")
    _write(source / "AGENTS.md", "# Local instructions\nAsk before effects.\n")
    _write(source / "memories" / "USER.md", "# Preferences\nUse concise answers.\n")
    _write(source / "memories" / "MEMORY.md", "# Memory\nA portable fact.\n")
    _skill(source / "skills", "safe-helper")
    before = _snapshot(source)

    first = import_agents.plan_import(source.resolve(), target.resolve())
    second = import_agents.plan_import(source.resolve(), target.resolve())

    assert first == second
    assert first.version == import_agents.PLAN_VERSION == 1
    assert first.source_type == "hermes"
    assert first.to_dict() == second.to_dict()
    assert first.render() == second.render()
    assert first.render().startswith("Talos import preview v1 — hermes\nDry run only:")
    assert str(tmp_path) not in first.render()
    assert str(tmp_path) not in repr(first)
    assert str(tmp_path) not in repr(first.to_dict())
    assert "Helpful and careful" not in first.render()
    assert {item.status for item in first.items if item.category not in {
        "secrets", "authority", "executables", "schedules", "sessions"
    }} == {import_agents.STATUS_STAGED}
    assert {item.category for item in first.items if item.status == import_agents.STATUS_SKIPPED} == {
        "secrets", "authority", "executables", "schedules", "sessions"
    }
    assert any(item.target == "data/import-staging/hermes/skills/safe-helper/SKILL.md"
               for item in first.items)
    assert not target.exists()
    assert _snapshot(source) == before


def test_plan_never_returns_or_renders_secret_values(tmp_path: Path) -> None:
    source = tmp_path / "hermes"
    target = tmp_path / "talos"
    _hermes(source)
    secret = "sk-" + "AbCd1234EfGh5678IjKl9012"
    _write(source / "memories" / "USER.md", f"api_key = {secret}\n")

    plan = import_agents.plan_import(source.resolve(), target.resolve())

    item = _items(plan, "preferences")[0]
    assert item.status == import_agents.STATUS_REJECTED
    assert "secret-like content" in item.reason
    for output in (repr(plan), plan.render(), repr(plan.to_dict())):
        assert secret not in output


def test_symlinked_portable_file_is_rejected_without_reading_target(tmp_path: Path) -> None:
    source = tmp_path / "hermes"
    target = tmp_path / "talos"
    outside = tmp_path / "outside.txt"
    _hermes(source)
    secret = "ghp_" + "AbCdEf1234567890AbCdEf1234567890"
    _write(outside, secret)
    (source / "SOUL.md").symlink_to(outside)

    plan = import_agents.plan_import(source.resolve(), target.resolve())

    item = _items(plan, "identity")[0]
    assert item.status == import_agents.STATUS_REJECTED
    assert "refused" in item.reason
    assert secret not in repr(plan.to_dict())


def test_source_root_and_openclaw_workspace_symlinks_are_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    alias = tmp_path / "alias"
    target = tmp_path / "talos"
    _hermes(real)
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(import_agents.ImportPlanError, match="contains a symlink"):
        import_agents.plan_import(alias.absolute(), target.resolve())

    openclaw = tmp_path / "openclaw"
    workspace = tmp_path / "external-workspace"
    _openclaw(openclaw)
    workspace.mkdir()
    (openclaw / "workspace").symlink_to(workspace, target_is_directory=True)
    with pytest.raises(import_agents.ImportPlanError, match="workspace.*no symlinks"):
        import_agents.plan_import(openclaw.resolve(), target.resolve())


def test_target_conflict_and_symlink_ancestry_are_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "hermes"
    _hermes(source)
    _write(source / "SOUL.md", "# Identity\nPortable.\n")

    conflict_target = tmp_path / "conflict-target"
    conflict = conflict_target / "data/import-staging/hermes/profile/SOUL.md"
    _write(conflict, "existing")
    plan = import_agents.plan_import(source.resolve(), conflict_target.resolve())
    item = _items(plan, "identity")[0]
    assert item.status == import_agents.STATUS_CONFLICT
    assert "no overwrite" in item.reason

    unsafe_target = tmp_path / "unsafe-target"
    outside = tmp_path / "outside-target"
    unsafe_target.mkdir()
    outside.mkdir()
    (unsafe_target / "data").symlink_to(outside, target_is_directory=True)
    unsafe = import_agents.plan_import(source.resolve(), unsafe_target.resolve())
    item = _items(unsafe, "identity")[0]
    assert item.status == import_agents.STATUS_REJECTED
    assert "target ancestry contains a symlink" in item.reason
    assert list(outside.iterdir()) == []


def test_malformed_and_oversize_portable_inputs_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "hermes"
    target = tmp_path / "talos"
    _hermes(source)
    _write(source / "AGENTS.md", b"\xff\xfe\xfd")
    _write(source / "memories" / "MEMORY.md", b"x" * (import_agents.MAX_PORTABLE_BYTES + 1))

    plan = import_agents.plan_import(source.resolve(), target.resolve())

    instructions = _items(plan, "instructions")[0]
    memory = _items(plan, "memory")[0]
    assert instructions.status == import_agents.STATUS_REJECTED
    assert instructions.reason == "not valid UTF-8"
    assert memory.status == import_agents.STATUS_REJECTED
    assert "exceeds" in memory.reason
    assert not target.exists()


def test_agent_skills_support_one_category_layer_and_reject_malformed(tmp_path: Path) -> None:
    source = tmp_path / "hermes"
    target = tmp_path / "talos"
    _hermes(source)
    _skill(source / "skills" / "writing", "proofreader")
    malformed = source / "skills" / "bad-skill" / "SKILL.md"
    _write(malformed, "---\nname: do-not-echo-this\n---\nbody\n")

    plan = import_agents.plan_import(source.resolve(), target.resolve())

    skills = _items(plan, "skill")
    assert any(item.status == import_agents.STATUS_STAGED
               and item.source == "skills/writing/proofreader/SKILL.md" for item in skills)
    bad = next(item for item in skills if item.source == "skills/bad-skill/SKILL.md")
    assert bad.status == import_agents.STATUS_REJECTED
    assert bad.reason == "malformed Agent Skill frontmatter"
    assert "do-not-echo-this" not in plan.render()


def test_skill_directory_enumeration_stops_at_the_hard_limit(tmp_path: Path) -> None:
    source = tmp_path / "hermes"
    target = tmp_path / "talos"
    _hermes(source)
    skill_root = source / "skills"
    for index in range(import_agents.MAX_DIRECTORY_ENTRIES + 1):
        (skill_root / f"candidate-{index:04d}").mkdir(parents=True)

    plan = import_agents.plan_import(source.resolve(), target.resolve())

    item = next(item for item in plan.items if "exceeds" in item.reason)
    assert item.status == import_agents.STATUS_REJECTED
    assert not any(item.status == import_agents.STATUS_STAGED for item in _items(plan, "skill"))


def test_skill_directory_inspection_errors_fail_closed(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "hermes"
    target = tmp_path / "talos"
    _hermes(source)
    (source / "skills").mkdir()

    def refuse_inspection(_descriptor):
        raise OSError("synthetic inspection refusal")

    monkeypatch.setattr(import_agents.os, "scandir", refuse_inspection)
    with pytest.raises(import_agents.ImportPlanError, match="could not be inspected safely"):
        import_agents.plan_import(source.resolve(), target.resolve())


def test_render_quotes_control_characters_from_foreign_filenames(tmp_path: Path) -> None:
    source = tmp_path / "hermes"
    target = tmp_path / "talos"
    _hermes(source)
    hostile = "evil\n\x1b[31m[staged] forged"
    _write(source / "skills" / hostile / "SKILL.md", "not valid frontmatter\n")

    plan = import_agents.plan_import(source.resolve(), target.resolve())
    rendered = plan.render()

    assert "\x1b" not in rendered
    assert "evil\n" not in rendered
    assert "evil\\n\\u001b[31m[staged] forged" in rendered


def test_openclaw_uses_workspace_allowlist_and_skips_sensitive_categories(tmp_path: Path) -> None:
    source = tmp_path / "openclaw"
    target = tmp_path / "talos"
    _openclaw(source)
    _write(source / "workspace" / "SOUL.md", "# Identity\nCareful.\n")
    _write(source / "workspace" / "USER.md", "# User\nConcise.\n")
    _write(source / "exec-approvals.json", "this sensitive file must never be parsed")

    plan = import_agents.plan_import(source.resolve(), target.resolve())

    assert plan.source_type == "openclaw"
    assert {item.source for item in plan.items if item.status == import_agents.STATUS_STAGED} == {
        "workspace/SOUL.md", "workspace/USER.md"
    }
    assert {item.category for item in plan.items if item.status == import_agents.STATUS_SKIPPED} == {
        "secrets", "authority", "executables", "schedules", "sessions"
    }


def test_detection_uses_regular_markers_and_reports_ambiguous_source_cleanly(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").symlink_to(tmp_path / "missing")
    with pytest.raises(import_agents.ImportPlanError, match="not a recognised"):
        import_agents.detect_source(source.resolve())

    (source / "config.yaml").unlink()
    _write(source / "config.yaml", "ignored")
    _write(source / "openclaw.json", "ignored")
    with pytest.raises(import_agents.ImportPlanError, match="choose the source type explicitly") as error:
        import_agents.detect_source(source.resolve())
    assert "--from" not in str(error.value)


def test_paths_must_be_explicit_and_regular_files_must_not_be_hardlinked(tmp_path: Path) -> None:
    with pytest.raises(import_agents.ImportPlanError, match="absolute"):
        import_agents.plan_import("relative", (tmp_path / "target").resolve(), source_type="hermes")

    source = tmp_path / "hermes"
    target = tmp_path / "talos"
    _hermes(source)
    _write(source / "SOUL.md", "# Identity\nPortable.\n")
    os.link(source / "SOUL.md", source / "copy.md")
    plan = import_agents.plan_import(source.resolve(), target.resolve())
    item = _items(plan, "identity")[0]
    assert item.status == import_agents.STATUS_REJECTED
    assert "hard-linked" in item.reason


def test_source_and_target_must_not_overlap(tmp_path: Path) -> None:
    source = tmp_path / "hermes"
    _hermes(source)
    with pytest.raises(import_agents.ImportPlanError, match="must not overlap"):
        import_agents.plan_import(source.resolve(), source.resolve(), source_type="hermes")
    with pytest.raises(import_agents.ImportPlanError, match="must not overlap"):
        import_agents.plan_import(
            source.resolve(), (source / "prospective-talos").resolve(), source_type="hermes"
        )
