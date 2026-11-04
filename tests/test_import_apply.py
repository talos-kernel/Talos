from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

from talos import import_agents
from talos.import_apply import (
    ImportApplyError,
    activate_imported_skill,
    apply_import,
    read_manifest,
    rollback_import,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _source(root: Path) -> None:
    _write(root / "config.yaml", "provider: local\n")
    _write(root / "SOUL.md", "# Previous agent\n\nCareful.\n")
    _write(root / "memories" / "USER.md", "# Preferences\n\nConcise.\n")
    _write(
        root / "skills" / "portable-helper" / "SKILL.md",
        "---\nname: portable-helper\ndescription: Portable careful guidance\n---\n\n"
        "# Instructions\n\nCheck the result.\n",
    )


def _target(root: Path) -> None:
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)


def test_apply_copies_reviewed_files_inactive_and_writes_redacted_manifest(tmp_path: Path) -> None:
    source = tmp_path / "hermes"
    target = tmp_path / "talos"
    _source(source)
    _target(target)
    before = tuple((path.relative_to(source).as_posix(), path.read_bytes())
                   for path in sorted(source.rglob("*")) if path.is_file())
    plan = import_agents.plan_import(source.resolve(), target.resolve(), source_type="hermes")

    result = apply_import(
        source.resolve(), target.resolve(), source_type="hermes",
        confirmation=plan.fingerprint(),
    )

    assert result["ok"] is True and result["state"] == "inactive" and result["copied"] == 3
    stage = target / "data/import-staging/hermes"
    assert (stage / "profile/SOUL.md").read_text() == "# Previous agent\n\nCareful.\n"
    assert (stage / "skills/portable-helper/SKILL.md").is_file()
    manifest = read_manifest(target, "hermes")
    assert manifest["fingerprint"] == plan.fingerprint() and manifest["state"] == "inactive"
    assert set(manifest["follow_up"]) == {"secrets", "authority", "executables", "schedules", "sessions"}
    assert manifest["follow_up"]["secrets"]["state"] == "reauth-required"
    assert manifest["follow_up"]["authority"]["state"] == "approval-required"
    assert manifest["follow_up"]["executables"]["state"] == "disabled-review"
    assert str(tmp_path) not in json.dumps(manifest)
    assert stat.S_IMODE((stage / "profile/SOUL.md").stat().st_mode) == 0o600
    assert stat.S_IMODE(stage.stat().st_mode) == 0o700
    after = tuple((path.relative_to(source).as_posix(), path.read_bytes())
                  for path in sorted(source.rglob("*")) if path.is_file())
    assert before == after


def test_apply_requires_current_fingerprint_and_never_overwrites_staging(tmp_path: Path) -> None:
    source = tmp_path / "hermes"
    target = tmp_path / "talos"
    _source(source)
    _target(target)
    plan = import_agents.plan_import(source.resolve(), target.resolve(), source_type="hermes")
    _write(source / "SOUL.md", "# Changed after review\n")

    with pytest.raises(ImportApplyError, match="confirmation does not match"):
        apply_import(source, target, source_type="hermes", confirmation=plan.fingerprint())
    assert not (target / "data/import-staging/hermes").exists()

    current = import_agents.plan_import(source.resolve(), target.resolve(), source_type="hermes")
    apply_import(source, target, source_type="hermes", confirmation=current.fingerprint())
    staged = (target / "data/import-staging/hermes/profile/SOUL.md").read_bytes()
    with pytest.raises(ImportApplyError, match="confirmation does not match|already exists"):
        apply_import(source, target, source_type="hermes", confirmation=current.fingerprint())
    assert (target / "data/import-staging/hermes/profile/SOUL.md").read_bytes() == staged


def test_apply_refuses_any_rejected_portable_item(tmp_path: Path) -> None:
    source = tmp_path / "hermes"
    target = tmp_path / "talos"
    _source(source)
    _target(target)
    _write(source / "memories/USER.md", "api_key=sk-" + "AbCd1234EfGh5678IjKl9012\n")
    plan = import_agents.plan_import(source, target, source_type="hermes")

    with pytest.raises(ImportApplyError, match="rejected or conflicting"):
        apply_import(source, target, source_type="hermes", confirmation=plan.fingerprint())
    assert not (target / "data/import-staging/hermes").exists()


def test_skill_activation_reuses_managed_writer_and_rollback_archives_staging(tmp_path: Path) -> None:
    source = tmp_path / "hermes"
    target = tmp_path / "talos"
    _source(source)
    _target(target)
    plan = import_agents.plan_import(source, target, source_type="hermes")
    apply_import(source, target, source_type="hermes", confirmation=plan.fingerprint())

    activated = activate_imported_skill(
        target, source_type="hermes", name="portable-helper", confirmation=plan.fingerprint()
    )
    active = target / "skills/portable-helper/SKILL.md"
    assert activated["active"] is True and active.is_file()
    assert "Portable careful guidance" in active.read_text()
    assert stat.S_IMODE(active.stat().st_mode) == 0o600
    with pytest.raises(ImportApplyError, match="activation refused"):
        activate_imported_skill(
            target, source_type="hermes", name="portable-helper", confirmation=plan.fingerprint()
        )

    rolled = rollback_import(target, source_type="hermes", confirmation=plan.fingerprint())
    assert rolled["state"] == "rolled-back"
    assert not (target / "data/import-staging/hermes").exists()
    assert (target / rolled["archive"] / "import-manifest.json").is_file()
    assert active.is_file(), "rollback of staging must not silently deactivate an approved skill"


def test_apply_refuses_shared_or_symlinked_target(tmp_path: Path) -> None:
    source = tmp_path / "hermes"
    _source(source)
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    os.chmod(shared, 0o777)
    plan = import_agents.plan_import(source, shared, source_type="hermes")
    with pytest.raises(ImportApplyError, match="not group/world writable"):
        apply_import(source, shared, source_type="hermes", confirmation=plan.fingerprint())


def test_activation_refuses_manifest_tampering_after_confirmation(tmp_path: Path) -> None:
    source = tmp_path / "hermes"
    target = tmp_path / "talos"
    _source(source)
    _target(target)
    plan = import_agents.plan_import(source, target, source_type="hermes")
    apply_import(source, target, source_type="hermes", confirmation=plan.fingerprint())
    manifest_path = target / "data/import-staging/hermes/import-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["items"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ImportApplyError, match="confirmed plan"):
        activate_imported_skill(
            target, source_type="hermes", name="portable-helper",
            confirmation=plan.fingerprint(),
        )
