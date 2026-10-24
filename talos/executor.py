"""Executor — die gegatete Ausführungs-Pipeline (M2-Spine, fail-closed).

Ablauf pro Tool-Aufruf:
  1. Policy-Kernel entscheidet (deterministisch, default-deny).
  2. Write-ahead Intent-Record ins Event-Log VOR jeder Wirkung (v0.2 §4).
  3. Nur bei ALLOW: Ziele binden (Verifier) + Snapshot nehmen.
  4. Recheck unmittelbar vor dem Lauf — bei Abweichung fail-closed abbrechen.
  5. Capability-Token praegen und Tool damit ausführen; Fehler/Verify-Fail → Restore.
  6. Ergebnis-Event (Receipt) schreiben — mit der Token-ID, die den Lauf belegt.

Der eigentliche Tool-Code ist injiziert (`runner`) — M2 liefert die sichere Maschinerie,
nicht die Tools selbst.

Das Token entsteht **so spaet wie moeglich**: nach Snapshot und Recheck, unmittelbar vor
dem Lauf. Seine Lebensdauer ist damit der kuerzeste Weg im ganzen System — und der
`GrantedRunner` laesst ohne es gar nichts laufen. Der Mint fragt den Kernel dabei ein
zweites Mal selbst; diese Doppelung ist Absicht: die Praegestelle glaubt auch dem
Executor nicht.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from . import verifier
from .autonomy import is_auto_attended
from .capability import CapabilityError, CapabilityMint, Grant
from .eventlog import Event, EventLog
from .manifest import Effect
from .policy import Decision, PolicyKernel, ToolRequest, Verdict, guard_targets
from .snapshot import SnapshotError, SnapshotToken, Snapshotter

GrantedRun = Callable[[ToolRequest, Grant], object]

# Ohne die Argumente steht im Receipt nur "run_shell" — nicht, welches Kommando lief.
# "Wer hat freigegeben" war belegbar, "was wurde ausgefuehrt" nicht. Das schliesst die Luecke.
AUDIT_MAX_CHARS = 400
COMMAND_AUDIT_DIGEST_KEY = "command_sha256"


# Die Quittung einer Handlung, die nur lief, weil the operator sie freigegeben hat.
APPROVED_DETAIL = "ran with your approval"
_BOUND_FILE_KEY = "__talos_bound_file_v1"


def _bound_runner_request(
    req: ToolRequest, bindings: tuple[verifier.Binding, ...]
) -> ToolRequest:
    """Freeze file runners to the verifier-bound object, not a mutable symlink."""
    if req.tool in {"skill_manage", "skill_view"}:
        args = dict(req.args)
        # Overwrite any model-supplied value. The grant binds this exact list;
        # the runner checks its derived destinations again before any effect.
        targets = [binding.realpath for binding in bindings]
        if req.tool == "skill_view" and not bindings:
            # Catalog reads target a directory, deliberately omitted by the file
            # snapshotter. Still bind that root into the capability before minting.
            from .skillmanage import view_targets
            targets = [os.path.realpath(path) for path in view_targets(req.args)]
        args["__talos_bound_skill_targets_v1"] = targets
        return ToolRequest(req.tool, req.identity, args, req.targets)
    if req.tool not in {"read_file", "write_file"} or len(bindings) != 1:
        return req
    binding = bindings[0]
    args = dict(req.args)
    args["path"] = binding.realpath
    args[_BOUND_FILE_KEY] = {
        "realpath": binding.realpath,
        "sha256": binding.sha256,
        "parent_dev": binding.parent_dev,
        "parent_ino": binding.parent_ino,
    }
    return ToolRequest(req.tool, req.identity, args, req.targets)


def done_detail(decision: Decision) -> str:
    """Der Beleg eines gelaufenen Werkzeugs — nie der Grund, der davor galt.

    `decision.reason` beschreibt den Zustand VOR dem Lauf. Bei NEEDS_HUMAN lautet er
    „will be executed later — needs your approval". Als Quittung einer erledigten
    Handlung ist dieser Satz nicht nur schief, er ist falsch — und er bleibt nicht im
    Log: ueber `tool_history_entry` geht er in den naechsten Prompt. Dort liest das
    Modell Prosa, nicht `status`, und erzaehlte the operator anschliessend, es warte noch auf
    eine Freigabe, die er eine Sekunde vorher erteilt hatte.
    """
    if is_auto_attended(decision):
        return "ran under your attended auto-approval setting"
    return APPROVED_DETAIL if decision.verdict is Verdict.NEEDS_HUMAN else decision.reason


# Arg-Schluessel, deren WERTE Zugangsdaten tragen koennen. Ins Log kommen nur die
# Struktur (Namen, Anzahl, Laenge), nie der Inhalt — ein Bearer-Token im
# `headers`-Objekt eines Requests waere sonst fuer immer im append-only Log.
_SENSITIVE_MAP_KEYS = frozenset({"headers", "authorization", "auth"})
# `body` ist doppeldeutig: bei `http_request` kann es ein Login-Payload sein
# (redigieren), bei `skill_write` ist es der Skill-Text selbst — der GEHOERT ins
# Log, weil genau er die haerteste Persistenz des Hauses auditiert.
_REDACT_BODY_TOOLS = frozenset({"http_request"})


def audit_args(args: dict, tool: str = "") -> dict:
    """Argumente fuers Event-Log: vollstaendig in den Schluesseln, beschnitten im Wert."""
    out: dict[str, str] = {}
    for key, value in args.items():
        folded = str(key).casefold()
        if folded in _SENSITIVE_MAP_KEYS and isinstance(value, dict):
            namen = ", ".join(str(k) for k in value) or "(leer)"
            out[str(key)] = f"[{len(value)} Eintraege, Werte redigiert: {namen}]"
            continue
        if folded == "body" and tool in _REDACT_BODY_TOOLS and isinstance(value, str) and value:
            out[str(key)] = f"[{len(value)} Zeichen, redigiert]"
            continue
        text = str(value)
        if len(text) > AUDIT_MAX_CHARS:
            text = text[:AUDIT_MAX_CHARS] + f"…(+{len(text) - AUDIT_MAX_CHARS} Zeichen)"
        out[str(key)] = text
    # A long shell command is intentionally truncated in the human-readable audit
    # payload, but completion verification must still bind to the exact command. The
    # derived digest is written after user-provided keys so it cannot be spoofed by an
    # argument named ``command_sha256``.
    command = args.get("command")
    if tool == "run_shell" and isinstance(command, str):
        out[COMMAND_AUDIT_DIGEST_KEY] = hashlib.sha256(command.encode("utf-8")).hexdigest()
    return out


class Status(str, Enum):
    DONE = "done"
    DENIED = "denied"
    NEEDS_HUMAN = "needs_human"
    BINDING_CHANGED = "binding_changed"
    VERIFY_FAILED = "verify_failed"
    ERROR = "error"


@dataclass(frozen=True)
class Outcome:
    status: Status
    detail: str
    result: object = None


@dataclass(frozen=True)
class Executor:
    policy: PolicyKernel
    log: EventLog
    snapshotter: Snapshotter
    runner: GrantedRun
    mint: CapabilityMint

    def run(
        self, req: ToolRequest, run_id: str, *, expected: object = None, human_approved: bool = False
    ) -> Outcome:
        decision = self.policy.decide(req)
        self._record(run_id, "exec.intent", req, decision)  # write-ahead VOR Wirkung
        self._record_auto_attended(run_id, req, decision)

        # DENY ist absolut — auch eine menschliche Freigabe hebt ihn NICHT auf (Bricking-Schutz).
        if decision.verdict is Verdict.DENY:
            return self._final(run_id, req.tool, Status.DENIED, decision.reason)
        # NEEDS_HUMAN läuft nur, wenn the operator diese Anfrage ausdrücklich freigegeben hat.
        if decision.verdict is Verdict.NEEDS_HUMAN and not human_approved:
            return self._final(run_id, req.tool, Status.NEEDS_HUMAN, decision.reason)

        # Gesichert wird, was der Kernel ableitet — nicht, was das LLM deklariert.
        # Sonst haengt das Undo an einem Feld, das der Reasoner weglassen kann.
        targets = self.policy.guard_targets(req)
        bindings = verifier.bind(targets)
        # A failed read must never rewrite its source (which may be read-only or
        # have changed independently). Binding checks still cover every target.
        spec = self.policy.manifest.get(req.tool)
        # Snapshot the same resolved objects that the verifier bound.  In particular,
        # a broken symlink's real target is "absent"; restoring the lexical link would
        # delete the link and strand the file the runner created behind it.
        snapshot_targets = tuple(binding.realpath for binding in bindings)
        try:
            token = self.snapshotter.take(
                () if spec and spec.effect is Effect.READ else snapshot_targets
            )
        except SnapshotError as error:
            # Snapshot refusal precedes both grant and runner, so restoration would be
            # not only unnecessary but unsafe for the special file that was refused.
            return self._final(run_id, req.tool, Status.ERROR, str(error))

        if not verifier.recheck(bindings):
            # No effect has started. Restoring here would overwrite the very external
            # change that the binding check detected (or follow a newly swapped link).
            return self._final(run_id, req.tool, Status.BINDING_CHANGED, "target changed before the run")

        bound_req = _bound_runner_request(req, bindings)

        # Erst jetzt das Recht praegen — auf diese Anfrage, fuer diesen einen Lauf.
        try:
            grant = self.mint.issue(bound_req, human_approved=human_approved)
        except CapabilityError as error:
            # Minting is still before the runner boundary; there is nothing to undo.
            return self._final(run_id, req.tool, Status.DENIED, str(error))
        self._record_grant(run_id, bound_req, grant)

        try:
            result = self.runner(bound_req, grant)
        except Exception as error:  # jeder Tool-Fehler -> zurückrollen
            # Atomic runners mark failures that happened before publication.  Rolling
            # those back can itself damage an unchanged FIFO, hardlink or read-only
            # file; rollback is only for a failure after an effect may have started.
            if getattr(error, "effect_started", None) is not False:
                try:
                    self.snapshotter.restore(token)
                except Exception as restore_error:
                    return self._final(
                        run_id, req.tool, Status.ERROR,
                        f"{error}; rollback failed: {restore_error}", grant=grant,
                    )
            return self._final(run_id, req.tool, Status.ERROR, str(error), grant=grant)

        if expected is not None and not verifier.verify_result(expected, result):
            try:
                self.snapshotter.restore(token)
            except Exception as restore_error:
                return self._final(
                    run_id, req.tool, Status.ERROR,
                    f"result verification failed; rollback failed: {restore_error}", grant=grant,
                )
            return self._final(run_id, req.tool, Status.VERIFY_FAILED, "Ergebnis != erwartet", grant=grant)

        self._record_snapshot(run_id, req, token)
        return self._final(run_id, req.tool, Status.DONE, done_detail(decision), result, grant=grant)

    def _record_auto_attended(self, run_id: str, req: ToolRequest, decision: Decision) -> None:
        """Beleg der Attended-Auto-Freigabe — leise fuer the operator, nie unsichtbar im Log.

        Steht direkt hinter dem Intent, VOR jeder Wirkung: wer spaeter fragt, warum
        hier kein Prompt kam, findet die Antwort am Lauf, nicht in der Erinnerung.
        """
        if not is_auto_attended(decision):
            return
        self.log.append(
            Event(
                run_id,
                "executor",
                "approval.auto_attended",
                {
                    "tool": req.tool,
                    "targets": list(self.policy.guard_targets(req)),
                    "reason": decision.reason,
                },
            )
        )

    def _record_grant(self, run_id: str, req: ToolRequest, grant: Grant) -> None:
        """Beleg der Praegung. Macht im Log sichtbar, dass ein Lauf ein Recht hatte —
        und welches. Der Abdruck steht gekuerzt drin: er identifiziert die Handlung,
        ohne Argument-Inhalte ein zweites Mal auszubreiten."""
        self.log.append(
            Event(
                run_id,
                "executor",
                "grant.issued",
                {
                    "tool": req.tool,
                    "grant_id": grant.short(),
                    "action_fp": grant.action_fp[:12],
                    "ttl_s": round(grant.expires_at - grant.issued_at, 1),
                    "human_approved": grant.human_approved,
                },
            )
        )

    def _record_snapshot(self, run_id: str, req: ToolRequest, token: SnapshotToken) -> None:
        """Snapshot-Beleg NACH dem erfolgreichen Lauf — der Ankerpunkt für `/undo`.

        Vorher lag der Rückwärtsgang im Prozessspeicher: nach einem Neustart war die
        letzte Änderung nicht mehr zurückrollbar. Jetzt steht er im Log.

        Nur bei WRITE und nur mit Einträgen: `run_shell` hat keine ableitbaren Ziele,
        sein Token ist leer — ein Beleg dafür würde ein Undo vortäuschen, das es nicht gibt.
        """
        spec = self.policy.manifest.get(req.tool)
        if spec is None or spec.effect is not Effect.WRITE or not token.entries:
            return
        self.log.append(
            Event(
                run_id,
                "executor",
                "snapshot.taken",
                {
                    "tool": req.tool,
                    "snapshot_id": token.snapshot_id,
                    "entries": [[original, backup] for original, backup in token.entries],
                },
            )
        )

    def _record(self, run_id: str, etype: str, req: ToolRequest, decision: Decision) -> None:
        self.log.append(
            Event(
                run_id=run_id,
                actor="executor",
                type=etype,
                payload={
                    "tool": req.tool,
                    "args": audit_args(req.args, req.tool),
                    "targets": list(self.policy.guard_targets(req)),
                    "verdict": decision.verdict.value,
                    "reason": decision.reason,
                },
            )
        )

    def _final(
        self,
        run_id: str,
        tool: str,
        status: Status,
        detail: str,
        result: object = None,
        *,
        grant: Grant | None = None,
    ) -> Outcome:
        payload = {"tool": tool, "status": status.value, "detail": detail}
        if grant is not None:
            payload["grant_id"] = grant.short()  # bindet das Ergebnis an sein Recht
        self.log.append(Event(run_id, "executor", "exec.result", payload))
        return Outcome(status, detail, result)
