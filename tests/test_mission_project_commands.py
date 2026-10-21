from __future__ import annotations

from pathlib import Path

from talos.approval import ApprovalStore
from talos.channel import Principal
from talos.commands import CommandCenter
from talos.eventlog import EventLog
from talos.manifest import Effect, ToolManifest, ToolSpec
from talos.missions import MissionCatalog
from talos.policy import PolicyKernel
from talos.projects import ProjectStore


OWNER = Principal("telegram", "100000001")
CHAT = "telegram:4242"


class _Reasoner:
    def cancel(self) -> bool:
        return False


class _Worker:
    def pending(self) -> int:
        return 0

    def busy(self) -> bool:
        return False

    def drain(self) -> int:
        return 0


def _center(tmp_path: Path) -> CommandCenter:
    manifest = ToolManifest().with_tool(ToolSpec("read_file", Effect.READ, reversible=True))
    policy = PolicyKernel(manifest, frozenset({OWNER}))
    return CommandCenter(
        log=EventLog(tmp_path / "events.db"),
        approvals=ApprovalStore(),
        policy=policy,
        started_at=0,
        bot_username="talos",
        reasoner=_Reasoner(),
        worker=_Worker(),
        repo_dir=tmp_path,
        missions=MissionCatalog(Path(__file__).resolve().parents[1] / "missions"),
        projects=ProjectStore(tmp_path / "projects.json"),
    )


def test_project_commands_add_select_update_and_report_status(tmp_path: Path) -> None:
    root = tmp_path / "Project One"
    root.mkdir()
    center = _center(tmp_path)

    added = center.dispatch("project", f'add alpha "{root}" Initial review',
                            principal=OWNER, conversation=CHAT)
    updated = center.dispatch("project", "context Open item: verify the dossier",
                              principal=OWNER, conversation=CHAT)
    status = center.dispatch("project", "status", principal=OWNER, conversation=CHAT)

    assert "Added and selected project alpha" in (added.reply or "")
    assert "Updated context" in (updated.reply or "")
    assert "alpha" in (status.reply or "")
    assert str(root.resolve()) in (status.reply or "")
    assert "verify the dossier" in (status.reply or "")


def test_mission_run_becomes_foreground_turn_with_selected_project(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    center = _center(tmp_path)
    center.dispatch("project", f"add alpha {root}", principal=OWNER, conversation=CHAT)

    result = center.dispatch(
        "mission",
        'run folder-dossier source=. output="reports/client dossier.md"',
        principal=OWNER,
        conversation=CHAT,
    )

    assert result.reply is None
    assert result.forward_as is not None
    assert "Verified folder dossier" in result.forward_as
    assert str(root / "reports" / "client dossier.md") in result.forward_as
    assert "grants no permission" in result.forward_as


def test_mission_commands_explain_missing_project_and_bad_arguments(tmp_path: Path) -> None:
    center = _center(tmp_path)

    missing = center.dispatch("mission", "run folder-dossier", principal=OWNER, conversation=CHAT)
    bad = center.dispatch("mission", "run folder-dossier not-an-assignment",
                          principal=OWNER, conversation=CHAT)
    listed = center.dispatch("missions", "", principal=OWNER, conversation=CHAT)

    assert "Select a project first" in (missing.reply or "")
    assert "Select a project first" in (bad.reply or "")
    assert "folder-dossier" in (listed.reply or "")
    assert "repo-guardian" in (listed.reply or "")


def test_continue_is_explicit_foreground_turn_and_remove_clears_selection(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    center = _center(tmp_path)
    center.dispatch("project", f"add alpha {root} Open item: finish tests",
                    principal=OWNER, conversation=CHAT)

    continuation = center.dispatch("continue", "Run the next safe check",
                                   principal=OWNER, conversation=CHAT)
    removed = center.dispatch("project", "remove alpha", principal=OWNER, conversation=CHAT)
    status = center.dispatch("project", "status", principal=OWNER, conversation=CHAT)

    assert continuation.forward_as is not None
    assert "Run the next safe check" in continuation.forward_as
    assert "Do not replay" in continuation.forward_as
    assert "Removed project alpha" in (removed.reply or "")
    assert "No selected project" in (status.reply or "")
