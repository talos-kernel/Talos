"""Read-only desktop views of this Mac conversation's existing archive."""
import os
from pathlib import Path
import sqlite3


def history(profile: Path) -> dict:
    path = profile / "data/transcript.db"
    if not path.is_file():
        return {"turns": [], "total": 0, "available": True}
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            db.execute("PRAGMA query_only=ON")
            identity = f"cli:{os.getuid()}"
            total = db.execute("SELECT COUNT(*) FROM turns WHERE conversation=?", (identity,)).fetchone()[0]
            rows = db.execute(
                "SELECT id,ts,substr(asked,1,8000),substr(answered,1,16000) FROM turns "
                "WHERE conversation=? ORDER BY ts DESC,id DESC LIMIT 100", (identity,)).fetchall()
        from talos.vault import redact_secrets
        turns = [{"id": row[0], "ts": row[1], "asked": redact_secrets(row[2]),
                  "answered": redact_secrets(row[3])} for row in rows]
        return {"turns": turns, "total": total, "available": True}
    except sqlite3.Error:
        return {"turns": [], "total": 0, "available": False}
