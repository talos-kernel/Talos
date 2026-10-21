"""Executor: die gegatete Pipeline rollt bei jedem Fehlpfad zurück und loggt Intent + Receipt."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from talos.capability import CapabilityError, CapabilityMint, GrantedRunner
from talos.eventlog import EventLog
from talos.executor import APPROVED_DETAIL, Executor, Status
from talos.manifest import Effect, ToolManifest, ToolSpec
from talos.policy import PolicyKernel, ToolRequest
from talos.snapshot import Snapshotter
from talos.channel import Principal
from talos import tools

OWNER = Principal("telegram", "100000001")


def _manifest() -> ToolManifest:
    return (
        ToolManifest()
        .with_tool(ToolSpec("write_file", Effect.WRITE, reversible=True))
        .with_tool(ToolSpec("send_mail", Effect.EXEC, reversible=False))
    )


def _executor(tmp_path: Path, runner) -> Executor:
    """Die rohen Runner liegen jetzt im GrantedRunner, nicht mehr im Executor.

    Die Test-Runner bleiben bewusst einarmig (`req`) — das Token nimmt ihnen der
    GrantedRunner ab, bevor sie ueberhaupt aufgerufen werden.
    """
    log = EventLog(tmp_path / "ev.db")
    policy = PolicyKernel(_manifest(), frozenset({OWNER}))
    mint = CapabilityMint(policy)
    snap = Snapshotter(tmp_path / ".snap")
    return Executor(
        policy=policy,
        log=log,
        snapshotter=snap,
        runner=GrantedRunner(mint=mint, runners={"write_file": runner, "send_mail": runner}),
        mint=mint,
    )


def test_denied_tool_records_intent_and_result(tmp_path: Path) -> None:
    ex = _executor(tmp_path, lambda req: "should not run")
    out = ex.run(ToolRequest("write_file", OWNER, {"path": "/etc/passwd"}, ()), "run1")
    assert out.status is Status.DENIED
    assert ex.log.count() == 2  # exec.intent + exec.result — kein grant.issued dazwischen


def test_irreversible_needs_human_and_does_not_run(tmp_path: Path) -> None:
    calls = []
    ex = _executor(tmp_path, lambda req: calls.append(1))
    out = ex.run(ToolRequest("send_mail", OWNER, {}), "run2")
    assert out.status is Status.NEEDS_HUMAN
    assert calls == []  # nichts ausgeführt


def test_allowed_write_runs_and_returns_result(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"

    def runner(req):
        target.write_text("geschrieben", encoding="utf-8")
        return "ok"

    ex = _executor(tmp_path, runner)
    out = ex.run(ToolRequest("write_file", OWNER, {"path": str(target)}), "run3")
    assert out.status is Status.DONE
    assert out.result == "ok"
    assert target.read_text(encoding="utf-8") == "geschrieben"


def test_runner_exception_rolls_back(tmp_path: Path) -> None:
    target = tmp_path / "cfg.txt"
    target.write_text("original", encoding="utf-8")

    def runner(req):
        target.write_text("halb geschrieben", encoding="utf-8")
        raise RuntimeError("boom")

    ex = _executor(tmp_path, runner)
    out = ex.run(ToolRequest("write_file", OWNER, {"path": str(target)}), "run4")
    assert out.status is Status.ERROR
    assert target.read_text(encoding="utf-8") == "original"  # zurückgerollt


def test_binding_change_before_runner_never_restores_over_external_state(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "cfg.txt"
    target.write_text("snapshot", encoding="utf-8")
    calls: list[int] = []
    ex = _executor(tmp_path, lambda _req: calls.append(1))

    def changed(_bindings) -> bool:
        target.write_text("external", encoding="utf-8")
        return False

    monkeypatch.setattr("talos.executor.verifier.recheck", changed)
    out = ex.run(ToolRequest("write_file", OWNER, {"path": str(target)}), "run-binding")

    assert out.status is Status.BINDING_CHANGED
    assert calls == []
    assert target.read_text(encoding="utf-8") == "external"


def test_mint_refusal_before_runner_never_restores_over_external_state(tmp_path: Path) -> None:
    target = tmp_path / "cfg.txt"
    target.write_text("snapshot", encoding="utf-8")
    calls: list[int] = []
    policy = PolicyKernel(_manifest(), frozenset({OWNER}))

    class RefusingMint:
        def issue(self, _req, *, human_approved=False):
            target.write_text("external", encoding="utf-8")
            raise CapabilityError("refused before effect")

    ex = Executor(
        policy=policy,
        log=EventLog(tmp_path / "mint-events.db"),
        snapshotter=Snapshotter(tmp_path / ".mint-snap"),
        runner=lambda _req, _grant: calls.append(1),
        mint=RefusingMint(),  # type: ignore[arg-type]
    )
    out = ex.run(ToolRequest("write_file", OWNER, {"path": str(target)}), "run-mint")

    assert out.status is Status.DENIED
    assert calls == []
    assert target.read_text(encoding="utf-8") == "external"


def test_verify_failed_rolls_back(tmp_path: Path) -> None:
    target = tmp_path / "cfg.txt"
    target.write_text("original", encoding="utf-8")

    def runner(req):
        target.write_text("neu", encoding="utf-8")
        return "actual"

    ex = _executor(tmp_path, runner)
    out = ex.run(ToolRequest("write_file", OWNER, {"path": str(target)}), "run5", expected="other")
    assert out.status is Status.VERIFY_FAILED
    assert target.read_text(encoding="utf-8") == "original"


def test_write_file_runner_refusal_does_not_delete_a_fifo(tmp_path: Path) -> None:
    target = tmp_path / "pipe"
    os.mkfifo(target)
    ex = _executor(tmp_path, tools.write_file)

    out = ex.run(
        ToolRequest("write_file", OWNER, {"path": str(target), "content": "x"}),
        "run-fifo",
    )

    assert out.status is Status.ERROR
    assert target.exists()
    assert stat.S_ISFIFO(target.stat().st_mode)


def test_verify_failure_via_broken_symlink_restores_the_real_target(tmp_path: Path) -> None:
    target = tmp_path / "missing-target.txt"
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    ex = _executor(tmp_path, tools.write_file)

    out = ex.run(
        ToolRequest("write_file", OWNER, {"path": str(link), "content": "new"}),
        "run-broken-link",
        expected="not the runner receipt",
    )

    assert out.status is Status.VERIFY_FAILED
    assert link.is_symlink()
    assert not target.exists()


def test_write_file_runner_refusal_does_not_try_to_restore_readonly_file(tmp_path: Path) -> None:
    target = tmp_path / "readonly.txt"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o444)
    ex = _executor(tmp_path, tools.write_file)

    out = ex.run(
        ToolRequest("write_file", OWNER, {"path": str(target), "content": "new"}),
        "run-readonly",
    )

    assert out.status is Status.ERROR
    assert target.read_text(encoding="utf-8") == "old"
    assert stat.S_IMODE(target.stat().st_mode) == 0o444


def test_file_runner_uses_verifier_bound_target_after_symlink_swap(tmp_path: Path) -> None:
    safe = tmp_path / "safe.txt"
    safe.write_text("safe", encoding="utf-8")
    protected_dir = tmp_path / ".secrets"
    protected_dir.mkdir()
    secret = protected_dir / "token.txt"
    secret.write_text("SECRET", encoding="utf-8")
    link = tmp_path / "selected.txt"
    link.symlink_to(safe)
    policy = PolicyKernel(_manifest(), frozenset({OWNER}))
    real_mint = CapabilityMint(policy)

    class SwappingMint:
        def issue(self, req, *, human_approved=False):
            grant = real_mint.issue(req, human_approved=human_approved)
            link.unlink()
            link.symlink_to(secret)
            return grant

        def redeem(self, grant, req):
            return real_mint.redeem(grant, req)

    mint = SwappingMint()
    ex = Executor(
        policy=policy,
        log=EventLog(tmp_path / "swap-events.db"),
        snapshotter=Snapshotter(tmp_path / ".swap-snap"),
        runner=GrantedRunner(mint=mint, runners={"write_file": tools.write_file}),  # type: ignore[arg-type]
        mint=mint,  # type: ignore[arg-type]
    )

    out = ex.run(
        ToolRequest("write_file", OWNER, {"path": str(link), "content": "PWNED"}),
        "run-swap",
    )

    assert out.status is Status.DONE
    assert safe.read_text(encoding="utf-8") == "PWNED"
    assert secret.read_text(encoding="utf-8") == "SECRET"
    assert link.resolve() == secret


def test_file_runner_refuses_parent_directory_swap_after_binding(tmp_path: Path) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    target = approved / "note.txt"
    target.write_text("old", encoding="utf-8")
    moved = tmp_path / "moved"
    policy = PolicyKernel(_manifest(), frozenset({OWNER}))
    real_mint = CapabilityMint(policy)

    class SwappingMint:
        def issue(self, req, *, human_approved=False):
            grant = real_mint.issue(req, human_approved=human_approved)
            approved.rename(moved)
            approved.mkdir()
            (approved / "note.txt").write_text("old", encoding="utf-8")
            return grant

        def redeem(self, grant, req):
            return real_mint.redeem(grant, req)

    mint = SwappingMint()
    ex = Executor(
        policy=policy,
        log=EventLog(tmp_path / "parent-swap-events.db"),
        snapshotter=Snapshotter(tmp_path / ".parent-swap-snap"),
        runner=GrantedRunner(mint=mint, runners={"write_file": tools.write_file}),  # type: ignore[arg-type]
        mint=mint,  # type: ignore[arg-type]
    )

    out = ex.run(
        ToolRequest("write_file", OWNER, {"path": str(target), "content": "new"}),
        "run-parent-swap",
    )

    assert out.status is Status.ERROR
    assert "parent changed" in out.detail
    assert (approved / "note.txt").read_text(encoding="utf-8") == "old"
    assert (moved / "note.txt").read_text(encoding="utf-8") == "old"


def test_file_runner_refuses_an_unbound_missing_parent(tmp_path: Path) -> None:
    parent = tmp_path / "not-created"
    target = parent / "note.txt"
    ex = _executor(tmp_path, tools.write_file)

    out = ex.run(
        ToolRequest("write_file", OWNER, {"path": str(target), "content": "new"}),
        "run-missing-parent",
    )

    assert out.status is Status.ERROR
    assert "parent did not exist at verifier binding" in out.detail
    assert not parent.exists()


def test_approved_run_receipt_does_not_still_ask_for_approval(tmp_path: Path) -> None:
    """Ein gelaufenes Werkzeug quittiert den Lauf, nicht die Bedingung davor.

    Der Beleg geht ueber `tool_history_entry` in den naechsten Prompt. Stand dort
    weiterhin „needs your approval", erklaerte das Modell dem Betreiber danach, es warte
    noch — Sekunden nachdem er freigegeben hatte und die Datei laengst geschrieben war.
    Das Modell liest die Prosa, nicht den Status; deshalb muss die Prosa stimmen.
    """
    calls: list[int] = []
    ex = _executor(tmp_path, lambda req: calls.append(1) or "sent")
    out = ex.run(ToolRequest("send_mail", OWNER, {}), "run7", human_approved=True)
    assert out.status is Status.DONE
    assert calls == [1]
    assert out.detail == APPROVED_DETAIL
    assert "needs your approval" not in out.detail
    assert "will be executed later" not in out.detail

    receipt = [e for e in ex.log.recent(20) if e["type"] == "exec.result"][0]
    assert "needs your approval" not in receipt["payload"]["detail"]


def test_allowed_run_keeps_the_kernel_reason_as_receipt(tmp_path: Path) -> None:
    """Ohne Freigabe-Umweg bleibt der Kernel-Grund die Quittung — nur NEEDS_HUMAN wird ersetzt."""
    target = tmp_path / "out.txt"
    ex = _executor(tmp_path, lambda req: target.write_text("x", encoding="utf-8") or "ok")
    out = ex.run(ToolRequest("write_file", OWNER, {"path": str(target)}), "run8")
    assert out.status is Status.DONE
    assert out.detail == ex.policy.decide(
        ToolRequest("write_file", OWNER, {"path": str(target)})
    ).reason


def test_receipt_names_the_grant_that_authorised_the_run(tmp_path: Path) -> None:
    """Jeder Lauf hinterlaesst sein Recht im Log — und der Beleg nennt dessen ID.

    Ohne das steht im Log zwar „ausgefuehrt", aber nicht „aufgrund welcher Erlaubnis".
    """
    target = tmp_path / "out.txt"

    def runner(req):
        target.write_text("x", encoding="utf-8")
        return "ok"

    ex = _executor(tmp_path, runner)
    out = ex.run(ToolRequest("write_file", OWNER, {"path": str(target)}), "run6")
    assert out.status is Status.DONE

    events = ex.log.recent(20)
    issued = [e for e in events if e["type"] == "grant.issued"]
    result = [e for e in events if e["type"] == "exec.result"]
    assert len(issued) == 1
    assert issued[0]["payload"]["ttl_s"] > 0
    assert issued[0]["payload"]["human_approved"] is False
    assert result[0]["payload"]["grant_id"] == issued[0]["payload"]["grant_id"]
