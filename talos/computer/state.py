"""Durable receipts; an uncertain write is never replayed automatically."""
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, operation_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, request TEXT NOT NULL,
                    state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    result TEXT NOT NULL DEFAULT '{}', UNIQUE(owner, operation_key));
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS routines (
                    name TEXT PRIMARY KEY, owner TEXT NOT NULL, source_job TEXT NOT NULL,
                    created REAL NOT NULL, request TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES ('control','paused')")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state='interrupted', updated=? WHERE state IN ('queued','running')",
                       (time.time(),))
            db.execute("UPDATE settings SET value='paused' WHERE key='control'")

    def control(self, value=None):
        with self.connect() as db:
            if value is not None:
                if value not in {"agent", "human", "paused", "stopped"}:
                    raise ValueError("invalid control state")
                db.execute("UPDATE settings SET value=? WHERE key='control'", (value,))
            return db.execute("SELECT value FROM settings WHERE key='control'").fetchone()[0]

    def begin(self, owner, args):
        encoded = json.dumps(args, sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM jobs WHERE owner=? AND operation_key=?",
                                  (owner, args["key"])).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("operation key already belongs to another request")
                return self.decode(existing), False
            if db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running')").fetchone():
                raise ValueError("the computer is busy; inspect the existing job")
            job_id = "pc-" + uuid.uuid4().hex[:24]
            now = time.time()
            db.execute("INSERT INTO jobs (id,owner,operation_key,fingerprint,request,state,created,updated,result) VALUES (?,?,?,?,?,?,?,?,?)",
                       (job_id, owner, args["key"], fingerprint, encoded, "queued", now, now, "{}"))
            # The explicit list above deliberately avoids schema-dependent INSERTs.
        return self.get(owner, job_id), True

    @staticmethod
    def decode(row):
        value = dict(row)
        request = json.loads(value.pop("request"))
        value.pop("fingerprint", None)
        value.pop("owner", None)
        value.update(title=request.get("title", "Computer"), project=request.get("project", ""),
                     operation=request.get("op", ""), checks_requested=len(request.get("checks", [])))
        value["result"] = json.loads(value["result"])
        return value

    def get(self, owner, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND owner=?", (job_id, owner)).fetchone()
        if row is None:
            raise ValueError("job not found")
        return self.decode(row)

    def jobs(self, owner):
        with self.connect() as db:
            return [self.decode(r) for r in db.execute(
                "SELECT * FROM jobs WHERE owner=? ORDER BY created DESC LIMIT 30", (owner,))]

    def finish(self, job_id, state, result=None):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state=?,updated=?,result=? WHERE id=?",
                       (state, time.time(), json.dumps(result or {}, ensure_ascii=False), job_id))

    def save_routine(self, owner, job_id, name):
        from .contract import slug
        slug(name, "routine name")
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND owner=?", (job_id, owner)).fetchone()
            if row is None or row["state"] != "verified":
                raise ValueError("only a job with passed file checks can become a tested routine")
            args = json.loads(row["request"])
            if args["op"] != "exec":
                raise ValueError("a routine must be a command with reproducible file checks")
            db.execute("INSERT INTO routines VALUES (?,?,?,?,?)",
                       (name, owner, job_id, time.time(), row["request"]))

    def routines(self, owner):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT name,source_job,created FROM routines WHERE owner=? ORDER BY created DESC", (owner,))]

    def routine(self, owner, name):
        from .contract import slug
        slug(name, "routine name")
        with self.connect() as db:
            row = db.execute("SELECT request,source_job FROM routines WHERE owner=? AND name=?",
                             (owner, name)).fetchone()
        if row is None:
            raise ValueError("routine not found")
        request = json.loads(row["request"])
        request.pop("key", None)
        return {"name": name, "source_job": row["source_job"], "template": request,
                "next_step": "inspect, choose a fresh operation key and submit through the kernel"}
