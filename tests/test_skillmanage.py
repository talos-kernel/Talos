"""skill_view / skill_manage — Pflege der verwalteten Skills-Ablage, direkt am Modul.

Geprueft wird gegen das echte Dateisystem: Hashes statt Vertrauen, keine Wirkung bei
einer Weigerung, kein Weg aus der Wurzel, Versionen ausserhalb des aktiven Skills.
Das Urteil (NEEDS_HUMAN/DENY), Executor-Rollback und Red-Team-Faelle gehoeren zur
Verdrahtung und werden dort getestet.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from talos import skillmanage as sm
from talos.skills import discover_skills
from talos.skillwrite import SkillWriteError, make_skill_write_runner

BODY = "## Steps\n\n1. Read the input.\n2. Do the thing.\n3. Report briefly.\n"
ABSENT = sm.ABSENT


def _req(**args: object) -> SimpleNamespace:
    return SimpleNamespace(args=args)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "skills"
    path.mkdir(mode=0o700)
    return Path(os.path.realpath(path))


@pytest.fixture
def view(root: Path):
    runner = sm.make_skill_view_runner(root)
    return lambda **args: json.loads(runner(_req(**args)))


@pytest.fixture
def manage(root: Path):
    runner = sm.make_skill_manage_runner(root)
    return lambda **args: json.loads(runner(_req(**args)))


def _sha(data: str | bytes) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _tree(root: Path) -> dict[str, tuple[str, bytes | None, int]]:
    """Alles unter der Wurzel, inklusive versteckter Eintraege — fuer Keine-Wirkung-Tests."""
    state: dict[str, tuple[str, bytes | None, int]] = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(current) / name
            info = path.lstat()
            kind = "link" if stat.S_ISLNK(info.st_mode) else "dir" if stat.S_ISDIR(info.st_mode) else "file"
            data = path.read_bytes() if kind == "file" and stat.S_ISREG(info.st_mode) else None
            state[str(path.relative_to(root))] = (kind, data, stat.S_IMODE(info.st_mode))
    return state


def _create(manage, name: str = "my-skill", body: str = BODY) -> dict:
    return manage(action="create", name=name, description="Does one useful thing.",
                  body=body, expected_sha256=ABSENT)


def _refused(runner_root: Path, **args: object) -> sm.SkillManageError:
    before = _tree(runner_root)
    with pytest.raises(sm.SkillManageError) as caught:
        sm.make_skill_manage_runner(runner_root)(_req(**args))
    assert caught.value.effect_started is False
    assert _tree(runner_root) == before, "a refusal must leave the root untouched"
    return caught.value


# --- Der ganze Weg ------------------------------------------------------------------

def test_lifecycle_create_view_patch_support_delete_restore(root: Path, view, manage) -> None:
    created = _create(manage)
    first = view(name="my-skill")
    assert first["status"] == "active"
    assert first["expected_sha256"] == created["expected_sha256"] == _sha((root / "my-skill" / "SKILL.md").read_bytes())
    assert "Untrusted" in first["notice"] and "no permission" in first["notice"]
    assert "Do the thing." in first["content"]

    patched = manage(action="patch", name="my-skill", old_string="Do the thing.",
                     new_string="Do the better thing.", expected_sha256=first["expected_sha256"])
    assert patched["archived_revision"] == first["expected_sha256"]
    assert "Do the better thing." in (root / "my-skill" / "SKILL.md").read_text()

    # Der alte Hash ist jetzt veraltet: abgelehnt, ohne Wirkung.
    error = _refused(root, action="patch", name="my-skill", old_string="better",
                     new_string="best", expected_sha256=first["expected_sha256"])
    assert "stale" in str(error)

    written = manage(action="write_file", name="my-skill", path="references/api.md",
                     content="# API notes\n", expected_sha256=ABSENT)
    support = root / "my-skill" / "references" / "api.md"
    assert support.read_text() == "# API notes\n"
    listed = view(name="my-skill")["files"]
    assert listed == [{"path": "references/api.md", "expected_sha256": written["expected_sha256"], "bytes": 12}]

    manage(action="remove_file", name="my-skill", path="references/api.md",
           expected_sha256=written["expected_sha256"])
    assert not support.exists()
    support_view = view(name="my-skill", path="references/api.md")
    assert support_view["expected_sha256"] == ABSENT
    assert [r["revision"] for r in support_view["revisions"]] == [written["expected_sha256"]]
    manage(action="restore", name="my-skill", path="references/api.md",
           revision=written["expected_sha256"], expected_sha256=ABSENT)
    assert support.read_text() == "# API notes\n"

    active = view(name="my-skill")["expected_sha256"]
    deleted = manage(action="delete", name="my-skill", expected_sha256=active)
    assert deleted["expected_sha256"] == ABSENT and "inactive" in deleted["note"]
    assert discover_skills(root).get("my-skill") is None
    assert support.exists(), "deactivation keeps support files"
    inactive = view(name="my-skill")
    assert inactive["status"] == "inactive" and inactive["expected_sha256"] == ABSENT
    revisions = [r["revision"] for r in inactive["revisions"]]
    assert set(revisions) == {first["expected_sha256"], active}
    assert view()["skills"] == [{"name": "my-skill", "status": "inactive"}]

    manage(action="restore", name="my-skill", revision=active, expected_sha256=ABSENT)
    catalog = discover_skills(root)
    assert catalog.get("my-skill") is not None and not catalog.rejected
    assert "Do the better thing." in catalog.body("my-skill")


def test_history_is_hidden_from_discovery_and_private(root: Path, view, manage) -> None:
    created = _create(manage)
    manage(action="edit", name="my-skill", expected_sha256=created["expected_sha256"],
           content="---\nname: my-skill\ndescription: Better.\n---\n\nNew body.\n")
    archive = root / ".history" / "my-skill" / f"SKILL.md@{created['expected_sha256']}"
    assert _sha(archive.read_bytes()) == created["expected_sha256"]
    catalog = discover_skills(root)
    assert [skill.name for skill in catalog.skills] == ["my-skill"] and not catalog.rejected
    for path in (root / ".history", root / ".history" / "my-skill", root / "my-skill"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in (archive, root / "my-skill" / "SKILL.md"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    manage(action="write_file", name="my-skill", path="scripts/run.sh",
           content="#!/bin/sh\necho hi\n", expected_sha256=ABSENT)
    script = root / "my-skill" / "scripts" / "run.sh"
    assert stat.S_IMODE(script.stat().st_mode) == 0o600, "scripts are data, never executable"
    assert stat.S_IMODE(script.parent.stat().st_mode) == 0o700
    assert not list(root.rglob(".*.tmp")), "no temporary file survives"


def test_view_has_no_side_effects(root: Path, view, manage, tmp_path: Path) -> None:
    missing = tmp_path / "nowhere" / "skills"
    assert json.loads(sm.make_skill_view_runner(missing)(_req()))["skills"] == []
    assert json.loads(sm.make_skill_view_runner(missing)(_req(name="x")))["expected_sha256"] == ABSENT
    assert not missing.parent.exists()
    _create(manage)
    before = _tree(root)
    view()
    view(name="my-skill")
    view(name="other")
    view(name="my-skill", path="references/none.md")
    assert _tree(root) == before
    assert not (root / ".history").exists()


def test_view_lists_bounded_entries_and_truncates_large_content(root: Path, view, manage, monkeypatch) -> None:
    for index in range(3):
        _create(manage, name=f"skill-{index}")
    monkeypatch.setattr(sm, "MAX_LISTED_SKILLS", 2)
    listing = view()
    assert [entry["name"] for entry in listing["skills"]] == ["skill-0", "skill-1"]
    assert listing["omitted"] == 1
    assert listing["skills"][0]["description"] == "Does one useful thing."
    # Ein vom Betreiber geschriebener grosser Skill: angezeigt wird beschnitten, der
    # Hash gilt der ganzen Datei.
    big = "---\nname: big\ndescription: Big.\n---\n\n" + "x" * 30_000 + "\n"
    (root / "big").mkdir(mode=0o700)
    (root / "big" / "SKILL.md").write_text(big)
    (root / "big" / "SKILL.md").chmod(0o600)
    shown = view(name="big")
    assert shown["truncated"] is True and len(shown["content"]) == sm.MAX_VIEW_CHARS
    assert shown["expected_sha256"] == _sha(big)


# --- Bestand und Altweg -------------------------------------------------------------

def test_legacy_skill_is_manageable_and_skill_write_stays_write_once(root: Path, view, manage) -> None:
    legacy = make_skill_write_runner(root)
    legacy(_req(name="old-skill", description="Legacy.", body=BODY))
    current = view(name="old-skill")
    manage(action="patch", name="old-skill", old_string="Report briefly.",
           new_string="Report in one line.", expected_sha256=current["expected_sha256"])
    assert "one line" in discover_skills(root).body("old-skill")
    with pytest.raises(SkillWriteError, match="already exists"):
        legacy(_req(name="old-skill", description="Again.", body=BODY))


def test_create_never_overwrites(root: Path, manage) -> None:
    _create(manage)
    error = _refused(root, action="create", name="my-skill", description="x",
                     body=BODY, expected_sha256=ABSENT)
    assert "stale" in str(error)
    _refused(root, action="create", name="new-one", description="x", body=BODY,
             expected_sha256=_sha("x"))


# --- Weigerungen ohne Wirkung -------------------------------------------------------

@pytest.mark.parametrize(
    "args",
    [
        {"action": "explode", "name": "my-skill", "expected_sha256": ABSENT},
        {"action": "edit", "name": "My_Skill", "content": "x", "expected_sha256": ABSENT},
        {"action": "edit", "name": "a" * 65, "content": "x", "expected_sha256": ABSENT},
        {"action": "edit", "name": "my-skill", "content": "x"},
        {"action": "edit", "name": "my-skill", "content": "x", "expected_sha256": "ABC"},
        {"action": "delete", "name": "my-skill", "expected_sha256": ABSENT},
        {"action": "remove_file", "name": "my-skill", "path": "references/a.md", "expected_sha256": ABSENT},
        {"action": "write_file", "name": "my-skill", "content": "x", "expected_sha256": ABSENT},
        {"action": "edit", "name": "my-skill", "path": "references/a.md", "content": "x",
         "expected_sha256": ABSENT},
        {"action": "restore", "name": "my-skill", "expected_sha256": ABSENT},
        {"action": "restore", "name": "my-skill", "revision": "0" * 64, "expected_sha256": "0" * 64},
    ],
)
def test_malformed_requests_are_refused_before_any_effect(root: Path, manage, args: dict) -> None:
    _create(manage)
    _refused(root, **args)


@pytest.mark.parametrize(
    "path",
    [
        "../escape.md", "references/../../escape.md", "/etc/passwd", "references/.hidden",
        "other/x.md", "references\\x.md", "references/a/b/c/d.md", "SKILL.md/x",
        "references/", "references//x.md", "references/x@" + "0" * 64, "references/x\x00.md",
        "references", "skill.md", "references/" + "a" * 65,
    ],
)
def test_traversal_and_odd_paths_are_refused(root: Path, manage, path: str) -> None:
    _create(manage)
    _refused(root, action="write_file", name="my-skill", path=path, content="x",
             expected_sha256=ABSENT)
    assert sm.manage_targets({"action": "write_file", "name": "my-skill", "path": path,
                              "expected_sha256": ABSENT}) == (str(sm._managed_root()),)


@pytest.mark.parametrize(
    "content, reason",
    [
        ("---\nname: my-skill\ndescription: d\nallowed-tools: run_shell\n---\n\nbody\n", "allowed-tools"),
        ("---\nname: my-skill\ndescription: d\n---\n\nUse ALLOWED-TOOLS here\n", "allowed-tools"),
        ("---\nname: my-skill\ndescription: d\n---\n\n" + "token: " + "AbC123xyz987QQ\n", "credential"),
        ("---\nname: my-skill\ndescription: d\n---\n\nkey " + "ghp_" + "abcdefghijklmnopqrstuv\n", "credential"),
        ("---\nname: other-name\ndescription: d\n---\n\nbody\n", "would not load"),
        ("no frontmatter at all\n", "would not load"),
        ("---\nname: my-skill\n---\n\nbody\n", "would not load"),
        ("---\nname: my-skill\ndescription: d\n---\n\n   \n", "body must not be empty"),
        ("---\nname: my-skill\ndescription: d\n---\n\nnul\x00byte\n", "NUL"),
        ("---\nname: my-skill\ndescription: d\n---\n\n" + "x" * 25_000, "longer than"),
        ("---\nname: my-skill\ndescription: d\n---\n\nbad \ud800 surrogate\n", "UTF-8"),
    ],
)
def test_edits_that_would_widen_rights_leak_or_break_loading_are_refused(
    root: Path, view, manage, content: str, reason: str
) -> None:
    _create(manage)
    current = view(name="my-skill")["expected_sha256"]
    error = _refused(root, action="edit", name="my-skill", content=content, expected_sha256=current)
    assert reason in str(error)


def test_support_files_are_bounded_text_without_secrets(root: Path, manage, monkeypatch) -> None:
    _create(manage)
    for content, reason in [
        ("x" * (sm.MAX_SUPPORT_BYTES + 1), "larger than"),
        ("aws " + "AKIA" + "ABCDEFGHIJKLMNOP\n", "credential"),
        ("allowed-tools: everything\n", "allowed-tools"),
        (b"\x00binary".decode(), "NUL"),
        (123, "must be text"),
    ]:
        error = _refused(root, action="write_file", name="my-skill", path="assets/a.txt",
                         content=content, expected_sha256=ABSENT)
        assert reason in str(error)
    monkeypatch.setattr(sm, "MAX_SUPPORT_FILES", 2)
    for name in ("one.md", "two.md"):
        manage(action="write_file", name="my-skill", path=f"references/{name}",
               content="ok\n", expected_sha256=ABSENT)
    error = _refused(root, action="write_file", name="my-skill", path="templates/three.md",
                     content="ok\n", expected_sha256=ABSENT)
    assert "support files" in str(error)


def test_patch_must_match_exactly_once_unless_replace_all(root: Path, view, manage) -> None:
    _create(manage, body="step\nstep\nend\n")
    current = view(name="my-skill")["expected_sha256"]
    assert "not found" in str(_refused(root, action="patch", name="my-skill", old_string="nope",
                                       new_string="x", expected_sha256=current))
    assert "2 times" in str(_refused(root, action="patch", name="my-skill", old_string="step",
                                     new_string="x", expected_sha256=current))
    _refused(root, action="patch", name="my-skill", old_string="", new_string="x", expected_sha256=current)
    _refused(root, action="patch", name="my-skill", old_string="step", new_string="x",
             replace_all="yes", expected_sha256=current)
    # Ein Patch darf den Namen im Frontmatter nicht aendern.
    assert "would not load" in str(_refused(root, action="patch", name="my-skill",
                                            old_string="name: my-skill", new_string="name: evil",
                                            expected_sha256=current))
    assert "already has this content" in str(_refused(root, action="patch", name="my-skill",
                                                      old_string="end", new_string="end",
                                                      expected_sha256=current))
    manage(action="patch", name="my-skill", old_string="step", new_string="go",
           replace_all=True, expected_sha256=current)
    assert discover_skills(root).body("my-skill") == "go\ngo\nend"


# --- Dateisystem-Angriffe -----------------------------------------------------------

def test_symlinked_skill_dir_support_dir_and_files_are_refused(root: Path, view, manage, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: linked\ndescription: d\n---\n\nbody\n")
    (root / "linked").symlink_to(outside)
    _refused(root, action="edit", name="linked", expected_sha256=_sha((outside / "SKILL.md").read_bytes()),
             content="---\nname: linked\ndescription: d\n---\n\nnew\n")
    with pytest.raises(sm.SkillManageError, match="symlink"):
        view(name="linked")

    _create(manage)
    (root / "my-skill" / "references").symlink_to(outside)
    _refused(root, action="write_file", name="my-skill", path="references/x.md", content="x",
             expected_sha256=ABSENT)
    assert not (outside / "x.md").exists()

    (root / "my-skill" / "assets").mkdir(mode=0o700)
    (root / "my-skill" / "assets" / "a.md").symlink_to(outside / "SKILL.md")
    _refused(root, action="write_file", name="my-skill", path="assets/a.md", content="x",
             expected_sha256=ABSENT)
    _refused(root, action="remove_file", name="my-skill", path="assets/a.md",
             expected_sha256=_sha((outside / "SKILL.md").read_bytes()))
    assert (outside / "SKILL.md").exists()

    skill_md = root / "my-skill" / "SKILL.md"
    current = _sha(skill_md.read_bytes())
    skill_md.unlink()
    skill_md.symlink_to(outside / "SKILL.md")
    _refused(root, action="delete", name="my-skill", expected_sha256=current)
    with pytest.raises(sm.SkillManageError, match="symlink"):
        view(name="my-skill")


def test_hardlinks_fifos_and_foreign_permissions_are_refused(root: Path, view, manage, tmp_path: Path) -> None:
    _create(manage)
    skill_md = root / "my-skill" / "SKILL.md"
    current = _sha(skill_md.read_bytes())
    os.link(skill_md, tmp_path / "alias")
    error = _refused(root, action="delete", name="my-skill", expected_sha256=current)
    assert "hard links" in str(error)
    (tmp_path / "alias").unlink()

    (root / "my-skill" / "assets").mkdir(mode=0o700)
    os.mkfifo(root / "my-skill" / "assets" / "pipe")
    assert "regular file" in str(_refused(root, action="write_file", name="my-skill",
                                          path="assets/pipe", content="x", expected_sha256=ABSENT))
    (root / "my-skill" / "assets" / "pipe").unlink()

    os.chmod(skill_md, 0o666)
    assert "group or others" in str(_refused(root, action="delete", name="my-skill",
                                             expected_sha256=current))
    os.chmod(skill_md, 0o600)
    os.chmod(root / "my-skill", 0o777)
    assert "group or others" in str(_refused(root, action="delete", name="my-skill",
                                             expected_sha256=current))
    os.chmod(root / "my-skill", 0o700)
    os.chmod(root, 0o770)
    try:
        assert "group or others" in str(_refused(root, action="delete", name="my-skill",
                                                 expected_sha256=current))
    finally:
        os.chmod(root, 0o700)


def test_group_writable_ancestor_is_refused(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o777)
    try:
        with pytest.raises(sm.SkillManageError, match="ancestor") as caught:
            sm.make_skill_manage_runner(shared / "skills")(_req(
                action="create", name="x", description="d", body=BODY, expected_sha256=ABSENT))
        assert caught.value.effect_started is False
        assert not (shared / "skills").exists(), "the root is not created under a shared ancestor"
    finally:
        os.chmod(shared, 0o700)


def test_tampered_history_is_refused(root: Path, view, manage) -> None:
    created = _create(manage)
    edited = manage(action="edit", name="my-skill", expected_sha256=created["expected_sha256"],
                    content="---\nname: my-skill\ndescription: d\n---\n\nsecond\n")
    archive = root / ".history" / "my-skill" / f"SKILL.md@{created['expected_sha256']}"
    archive.write_text("---\nname: my-skill\ndescription: d\n---\n\nevil swap\n")
    with pytest.raises(sm.SkillManageError, match="tampered"):
        view(name="my-skill", revision=created["expected_sha256"])
    error = _refused(root, action="restore", name="my-skill", revision=created["expected_sha256"],
                     expected_sha256=edited["expected_sha256"])
    assert "tampered" in str(error)
    # Eine vorgelegte, falsche Archivdatei fuer den AKTUELLEN Stand: abgelehnt, bevor
    # etwas veroeffentlicht wird.
    planted = root / ".history" / "my-skill" / f"SKILL.md@{edited['expected_sha256']}"
    planted.write_text("not what it claims\n")
    os.chmod(planted, 0o600)
    error = _refused(root, action="patch", name="my-skill", old_string="second", new_string="third",
                     expected_sha256=edited["expected_sha256"])
    assert "tampered" in str(error)


def test_restore_requires_current_state_and_known_revision(root: Path, view, manage) -> None:
    created = _create(manage)
    edited = manage(action="edit", name="my-skill", expected_sha256=created["expected_sha256"],
                    content="---\nname: my-skill\ndescription: d\n---\n\nsecond\n")
    # Neuerer Inhalt wird nicht still ueberschrieben: expected muss stimmen.
    _refused(root, action="restore", name="my-skill", revision=created["expected_sha256"],
             expected_sha256=ABSENT)
    _refused(root, action="restore", name="my-skill", revision="1" * 64,
             expected_sha256=edited["expected_sha256"])
    restored = manage(action="restore", name="my-skill", revision=created["expected_sha256"],
                      expected_sha256=edited["expected_sha256"])
    assert restored["expected_sha256"] == created["expected_sha256"]
    assert restored["archived_revision"] == edited["expected_sha256"]
    revisions = view(name="my-skill")["revisions"]
    assert {r["revision"] for r in revisions} == {created["expected_sha256"], edited["expected_sha256"]}


def test_history_is_bounded_by_refusal_not_pruning(root: Path, view, manage, monkeypatch) -> None:
    def text(word: str) -> str:
        return f"---\nname: my-skill\ndescription: d\n---\n\n{word}\n"

    first = _create(manage)["expected_sha256"]
    second = manage(action="edit", name="my-skill", expected_sha256=first, content=text("second"))["expected_sha256"]
    manage(action="restore", name="my-skill", revision=first, expected_sha256=second)
    monkeypatch.setattr(sm, "MAX_HISTORY_FILES", 2)
    # The current state (first) is already archived: deduplicated, no new slot needed.
    third = manage(action="edit", name="my-skill", expected_sha256=first, content=text("third"))
    assert third["archived_revision"] == first
    error = _refused(root, action="edit", name="my-skill", expected_sha256=third["expected_sha256"],
                     content=text("fourth"))
    assert "prunes" in str(error)
    assert len(list((root / ".history" / "my-skill").iterdir())) == 2


# --- Wettlaeufe: explizite Grenzen, kein vorgetaeuschtes CAS ------------------------

def test_external_change_after_view_is_a_stale_refusal(root: Path, view, manage) -> None:
    _create(manage)
    seen = view(name="my-skill")["expected_sha256"]
    skill_md = root / "my-skill" / "SKILL.md"
    skill_md.write_text(skill_md.read_text().replace("Do the thing.", "Operator edit."))
    _refused(root, action="patch", name="my-skill", old_string="Report", new_string="Say",
             expected_sha256=seen)
    assert "Operator edit." in skill_md.read_text()


def test_change_between_archive_and_publication_rolls_back_own_archive(root: Path, view, manage, monkeypatch) -> None:
    _create(manage)
    seen = view(name="my-skill")["expected_sha256"]
    skill_md = root / "my-skill" / "SKILL.md"
    original_publish = sm._publish

    def racing_publish(dir_fd, base, data, expected, label):
        if label != "history entry":
            # Ein fremder Schreiber (kein skill_manage) zwischen Archiv und Veroeffentlichung.
            skill_md.write_text(skill_md.read_text().replace("Do the thing.", "Concurrent edit."))
        return original_publish(dir_fd, base, data, expected, label)

    monkeypatch.setattr(sm, "_publish", racing_publish)
    with pytest.raises(sm.SkillManageError, match="concurrently") as caught:
        sm.make_skill_manage_runner(root)(_req(action="patch", name="my-skill", old_string="Report",
                                               new_string="Say", expected_sha256=seen))
    assert caught.value.effect_started is False
    assert "Concurrent edit." in skill_md.read_text(), "the concurrent change survives"
    assert not (root / ".history" / "my-skill" / f"SKILL.md@{seen}").exists(), "own archive withdrawn"
    assert not list(root.rglob(".*.tmp"))


def test_failure_after_publication_reports_effect_started(root: Path, view, manage, monkeypatch) -> None:
    _create(manage)
    seen = view(name="my-skill")["expected_sha256"]
    calls = {"n": 0}

    def failing_sync(dir_fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 2:   # 1 = archive, 2 = the published SKILL.md
            raise OSError("disk went away")
        os.fsync(dir_fd)

    monkeypatch.setattr(sm, "_fsync_dir", failing_sync)
    with pytest.raises(sm.SkillManageError) as caught:
        sm.make_skill_manage_runner(root)(_req(action="patch", name="my-skill", old_string="Report",
                                               new_string="Say", expected_sha256=seen))
    assert caught.value.effect_started is True, "the executor must restore its snapshot"


def test_concurrent_manage_calls_are_serialized_by_flock(root: Path, manage, monkeypatch) -> None:
    import fcntl

    monkeypatch.setattr(sm, "LOCK_TIMEOUT_S", 0.1)
    holder = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)
        error = _refused(root, action="create", name="my-skill", description="d", body=BODY,
                         expected_sha256=ABSENT)
        assert "in progress" in str(error)
    finally:
        os.close(holder)
    _create(manage)


# --- Ziele fuer den Kernel ----------------------------------------------------------

def test_targets_are_kernel_derived_files_under_the_managed_root(root: Path, monkeypatch) -> None:
    monkeypatch.setenv("TALOS_SKILLS_DIRS", str(root))
    sha_a, sha_b = "a" * 64, "b" * 64
    skill = str(root / "s" / "SKILL.md")
    assert sm.view_targets({}) == (str(root),)
    assert sm.view_targets({"name": "../x"}) == (str(root),)
    assert sm.view_targets({"name": "s"}) == (skill,)
    assert sm.view_targets({"name": "s", "revision": sha_a}) == (skill, str(root / ".history" / "s" / f"SKILL.md@{sha_a}"))
    assert sm.view_targets({"path": "references/x.md"}) == (str(root),)
    assert sm.manage_targets({"action": "create", "name": "s", "expected_sha256": ABSENT}) == (skill,)
    assert sm.manage_targets({"action": "edit", "name": "s", "expected_sha256": sha_a}) == (
        skill, str(root / ".history" / "s" / f"SKILL.md@{sha_a}"))
    assert sm.manage_targets({"action": "restore", "name": "s", "path": "references/x.md",
                              "revision": sha_b, "expected_sha256": sha_a}) == (
        str(root / "s" / "references" / "x.md"),
        str(root / ".history" / "s" / "references" / f"x.md@{sha_a}"),
        str(root / ".history" / "s" / "references" / f"x.md@{sha_b}"),
    )
    for invalid in ({}, None, "x", {"action": "edit", "name": "s"},
                    {"action": "edit", "name": "s", "expected_sha256": sha_a, "path": "/etc/passwd"}):
        assert sm.manage_targets(invalid) == (str(root),)
    assert not (root / "s").exists() and not (root / ".history").exists()


def test_production_runners_resolve_the_policy_root(root: Path, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TALOS_SKILLS_DIRS", str(root))
    args = dict(action="create", name="prod", description="d", body=BODY, expected_sha256=ABSENT)
    bound = [os.path.realpath(target) for target in sm.manage_targets(args)]
    # Ohne die Bindung des Executors, mit erfundener oder mit veralteter Bindung: nichts.
    for forged in (None, [], [str(tmp_path / "elsewhere" / "prod" / "SKILL.md")], "x"):
        extra = {} if forged is None else {sm.BOUND_TARGETS_KEY: forged}
        before = _tree(root)
        with pytest.raises(sm.SkillManageError, match="bound|differ") as caught:
            sm.skill_manage(_req(**args, **extra))
        assert caught.value.effect_started is False and _tree(root) == before
    # Die Wurzel wechselt nach dem Urteil: die abgeleiteten Ziele passen nicht mehr.
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("TALOS_SKILLS_DIRS", str(elsewhere))
    with pytest.raises(sm.SkillManageError, match="differ"):
        sm.skill_manage(_req(**args, **{sm.BOUND_TARGETS_KEY: bound}))
    assert not elsewhere.exists()
    monkeypatch.setenv("TALOS_SKILLS_DIRS", str(root))
    created = json.loads(sm.skill_manage(_req(**args, **{sm.BOUND_TARGETS_KEY: bound})))
    assert (root / "prod" / "SKILL.md").exists()
    named = {"name": "prod", sm.BOUND_TARGETS_KEY: [str(root / "prod" / "SKILL.md")]}
    assert json.loads(sm.skill_view(_req(**named)))["expected_sha256"] == created["expected_sha256"]
    assert sm.manage_targets({"action": "delete", "name": "prod", "expected_sha256": created["expected_sha256"]})[0] == str(
        root / "prod" / "SKILL.md")


def test_production_view_reads_only_the_bound_root(root: Path, manage, monkeypatch, tmp_path: Path) -> None:
    """Listing and named reads: bound to the judged root, checked before the first read."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    hidden = sm.make_skill_manage_runner(elsewhere)
    hidden(_req(action="create", name="prod", description="Unjudged.", body="UNJUDGED BODY\n",
                expected_sha256=ABSENT))
    _create(manage, name="prod")
    first = json.loads(sm.make_skill_view_runner(root)(_req(name="prod")))["expected_sha256"]
    manage(action="edit", name="prod", expected_sha256=first,
           content="---\nname: prod\ndescription: d\n---\n\nsecond\n")
    monkeypatch.setenv("TALOS_SKILLS_DIRS", str(root))

    requests = [
        {},                                       # catalog listing: the root directory
        {"name": "prod"},                         # named read
        {"name": "prod", "revision": first},      # revision read: active file + archive
    ]
    bound = [[os.path.realpath(t) for t in sm.view_targets(args)] for args in requests]
    assert bound[0] == [str(root)] and len(bound[2]) == 2

    opened: list[object] = []
    real_session = sm._session
    monkeypatch.setattr(sm, "_session", lambda *a, **k: opened.append(a) or real_session(*a, **k))
    for args, targets in zip(requests, bound):
        # Unbound, forged, or truncated: refused before anything under a root is opened.
        for forged in (None, [], [str(elsewhere)], targets[:-1] + [str(elsewhere / "x")], "x"):
            extra = {} if forged is None else {sm.BOUND_TARGETS_KEY: forged}
            with pytest.raises(sm.SkillManageError, match="bound|differ"):
                sm.skill_view(_req(**args, **extra))
        assert opened == []
        # The root changes after the capability was minted: the bound list no longer
        # matches what the runner derives from its single root resolution.
        monkeypatch.setenv("TALOS_SKILLS_DIRS", str(elsewhere))
        with pytest.raises(sm.SkillManageError, match="differ"):
            sm.skill_view(_req(**args, **{sm.BOUND_TARGETS_KEY: targets}))
        assert opened == []
        monkeypatch.setenv("TALOS_SKILLS_DIRS", str(root))
        shown = sm.skill_view(_req(**args, **{sm.BOUND_TARGETS_KEY: targets}))
        assert "UNJUDGED" not in shown and "Unjudged." not in shown
        opened.clear()

    listing = json.loads(sm.skill_view(_req(**{sm.BOUND_TARGETS_KEY: bound[0]})))
    assert listing["skills"] == [{"name": "prod", "status": "active", "description": "d"}]
    revision = json.loads(sm.skill_view(_req(**requests[2], **{sm.BOUND_TARGETS_KEY: bound[2]})))
    assert revision["revision"]["id"] == first and "Do the thing." in revision["revision"]["content"]
