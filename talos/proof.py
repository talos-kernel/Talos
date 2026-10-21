"""Compact outcome receipts derived only from Talos' append-only event log.

This is intentionally not a transcript export.  It names tools, status counts and
artifacts backed by successful write snapshots, but never copies prompts, tool arguments,
result bodies or model answers into a second data store.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping


DEFAULT_RUNS = 25
SUCCESS = frozenset({"done", "ok", "success", "completed"})
FAILURE = frozenset({"error", "failed", "denied", "refused", "cancelled", "canceled"})


def _entry(value: object, key: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _payload(value: object) -> dict:
    payload = _entry(value, "payload", {})
    return dict(payload) if isinstance(payload, Mapping) else {}


def _safe_artifact(value: object, home: Path) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    try:
        path = Path(text)
        relative = path.relative_to(home)
        return str(Path("~") / relative)
    except (TypeError, ValueError):
        return text[:500]


def summarize_events(events: Iterable[object], *, home: Path | None = None) -> dict:
    """Build one conservative receipt. No event prose crosses this boundary."""
    rows = list(events)
    if not rows:
        return {}
    operator_home = Path.home() if home is None else Path(home)
    run_id = str(_entry(rows[0], "run_id", ""))
    started: float | None = None
    done: float | None = None
    status = "unknown"
    evidence_ids: list[int] = []
    stats: dict[str, dict[str, object]] = {}
    pending_snapshots: dict[str, list[list[str]]] = defaultdict(list)
    artifacts: list[str] = []

    def tool_stats(name: str) -> dict[str, object]:
        if name not in stats:
            stats[name] = {"tool": name, "calls": 0, "successful": 0,
                           "failed": 0, "last_status": ""}
        return stats[name]

    for row in rows:
        typ = str(_entry(row, "type", ""))
        ts = float(_entry(row, "ts", 0.0) or 0.0)
        payload = _payload(row)
        row_id = _entry(row, "id", _entry(row, "rowid", 0))
        if isinstance(row_id, int) and typ in {"exec.intent", "snapshot.taken", "exec.result", "reason.done"}:
            evidence_ids.append(row_id)
        if typ == "reason.started" and started is None:
            started = ts
        elif typ == "reason.done":
            done = ts
            status = str(payload.get("status") or "done")[:40]
        elif typ == "snapshot.taken":
            tool = str(payload.get("tool") or "unknown")[:80]
            entries = payload.get("entries")
            if isinstance(entries, list):
                pending_snapshots[tool].append([
                    str(item[0]) for item in entries
                    if isinstance(item, (list, tuple)) and item
                ])
        elif typ == "exec.result":
            tool = str(payload.get("tool") or "unknown")[:80]
            result_status = str(payload.get("status") or "unknown").lower()[:40]
            item = tool_stats(tool)
            item["calls"] = int(item["calls"]) + 1
            item["last_status"] = result_status
            snapshots = pending_snapshots[tool].pop(0) if pending_snapshots[tool] else []
            if result_status in SUCCESS:
                item["successful"] = int(item["successful"]) + 1
                artifacts.extend(_safe_artifact(path, operator_home) for path in snapshots)
            elif result_status in FAILURE:
                item["failed"] = int(item["failed"]) + 1

    timestamps = [float(_entry(row, "ts", 0.0) or 0.0) for row in rows]
    if started is None and timestamps:
        started = min(timestamps)
    if done is None and any(str(_entry(row, "type", "")) == "done" for row in rows):
        done = max(timestamps)
        status = "done"
    tools = [stats[name] for name in sorted(stats)]
    unique_artifacts = list(dict.fromkeys(path for path in artifacts if path))
    return {
        "run_id": run_id,
        "status": status,
        "started_ts": started,
        "done_ts": done,
        "duration_s": round(done - started, 3) if started is not None and done is not None else None,
        "tool_calls": sum(int(item["calls"]) for item in tools),
        "successful_calls": sum(int(item["successful"]) for item in tools),
        "failed_calls": sum(int(item["failed"]) for item in tools),
        "tools": tools,
        "artifacts": unique_artifacts[:25],
        "evidence_event_ids": evidence_ids[:100],
    }


def collect(db_path: Path, *, run_id: str = "", limit: int = DEFAULT_RUNS) -> tuple[dict, ...]:
    """Read receipts through an enforced read-only SQLite connection."""
    path = Path(db_path)
    if not path.is_file():
        return ()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    try:
        selected = [run_id.strip()] if run_id.strip() else [
            str(row[0]) for row in conn.execute(
                "SELECT run_id FROM events GROUP BY run_id "
                "HAVING SUM(CASE WHEN type IN ('exec.result', 'reason.done') THEN 1 ELSE 0 END) > 0 "
                "ORDER BY MAX(id) DESC LIMIT ?", (max(1, min(int(limit), 100)),)
            )
        ]
        summaries: list[dict] = []
        for selected_run in selected:
            rows = []
            for eid, ts, rid, typ, raw in conn.execute(
                "SELECT id, ts, run_id, type, payload_json FROM events "
                "WHERE run_id = ? ORDER BY id", (selected_run,)
            ):
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    payload = {}
                rows.append({"id": int(eid), "ts": float(ts), "run_id": str(rid),
                             "type": str(typ), "payload": payload if isinstance(payload, dict) else {}})
            if rows:
                summaries.append(summarize_events(rows))
        return tuple(summaries)
    finally:
        conn.close()


def _when(ts: object) -> str:
    if ts is None:
        return "unfinished"
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def render_markdown(summaries: Iterable[Mapping[str, object]]) -> str:
    rows = list(summaries)
    lines = [
        "# Talos outcome proof",
        "",
        "> Event-log evidence only. Prompts, arguments, result bodies and model answers are excluded.",
        "",
    ]
    if not rows:
        lines.append("No matching run evidence found.")
        return "\n".join(lines) + "\n"
    for item in rows:
        lines.extend([
            f"## Run `{item.get('run_id', '')}`",
            "",
            f"- Status: `{item.get('status', 'unknown')}`",
            f"- Finished: {_when(item.get('done_ts'))}",
            f"- Tool calls: {item.get('tool_calls', 0)} ({item.get('successful_calls', 0)} successful, {item.get('failed_calls', 0)} failed)",
        ])
        tools = item.get("tools") or []
        if isinstance(tools, list) and tools:
            lines.append("- Tools: " + ", ".join(
                f"{tool.get('tool', '?')} {tool.get('successful', 0)}/{tool.get('calls', 0)}"
                for tool in tools if isinstance(tool, Mapping)
            ))
        artifacts = item.get("artifacts") or []
        if isinstance(artifacts, list) and artifacts:
            lines.append("- Verified artifact targets:")
            lines.extend(f"  - `{path}`" for path in artifacts)
        else:
            lines.append("- Verified artifact targets: none recorded")
        lines.append("")
    return "\n".join(lines)


def run_proof(argv: list[str] | None = None, *, out=None, db=None) -> int:
    args = list(argv or [])
    write = (out or sys.stdout).write
    if "--help" in args or "-h" in args:
        write("  usage: talos proof [--run <id>] [--format markdown|json] [--out <file>]\n")
        return 0

    def value(flag: str, default: str = "") -> str:
        if flag not in args:
            return default
        index = args.index(flag)
        if index + 1 >= len(args):
            raise ValueError(f"missing value for {flag}")
        return args[index + 1]

    try:
        allowed = {"--run", "--format", "--out"}
        consumed: set[int] = set()
        for index, item in enumerate(args):
            if index in consumed:
                continue
            if item not in allowed or index + 1 >= len(args):
                raise ValueError(f"unknown or incomplete option: {item}")
            consumed.add(index + 1)
        format_name = value("--format", "markdown").lower()
        if format_name not in {"markdown", "json"}:
            raise ValueError("--format must be markdown or json")
        from .config import EVENTLOG_DB
        summaries = collect(Path(db) if db is not None else Path(EVENTLOG_DB), run_id=value("--run"))
        text = (json.dumps(summaries, ensure_ascii=False, indent=2) + "\n"
                if format_name == "json" else render_markdown(summaries))
        target = value("--out")
        if target:
            # A receipt export must never truncate operator state through a regular
            # file or hardlink. Exclusive creation also makes the 0600 promise true.
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            write(f"  written to {target} ({len(text)} bytes, mode 600)\n")
        else:
            write(text)
        return 0
    except (OSError, sqlite3.Error, ValueError) as error:
        write(f"  proof failed: {error}\n")
        return 2


__all__ = ["collect", "render_markdown", "run_proof", "summarize_events"]
