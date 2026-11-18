"""
Crash-report store: SQLite on the server's data volume.

Events arrive already scrubbed by the app; the server re-validates shape and
size, groups them into issues by fingerprint, and prunes old events. The
admin endpoint lists issues (newest first) for the operator.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    fingerprint  TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    message      TEXT NOT NULL,
    level        TEXT NOT NULL,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    events       INTEGER NOT NULL DEFAULT 0,
    occurrences  INTEGER NOT NULL DEFAULT 0,
    installs     TEXT NOT NULL DEFAULT '[]',
    releases     TEXT NOT NULL DEFAULT '[]',
    frames_json  TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL REFERENCES issues(fingerprint),
    received_at  TEXT NOT NULL,
    body_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_received ON events (received_at);
CREATE INDEX IF NOT EXISTS ix_events_fp ON events (fingerprint);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _merge_list(raw: str, value: str, limit: int = 20) -> str:
    items = json.loads(raw)
    if value and value not in items:
        items.append(value)
    return json.dumps(items[-limit:])


class TelemetryStore:
    def __init__(self, path: str | Path, *, retention_days: int = 90) -> None:
        self.path = Path(path)
        self.retention_days = retention_days
        self._lock = threading.Lock()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;" + SCHEMA)
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(issues)")}
            if "status" not in cols:                       # Phase 11: triage state for the admin portal
                self._conn.execute("ALTER TABLE issues ADD COLUMN status TEXT NOT NULL DEFAULT 'open'")
                self._conn.execute("ALTER TABLE issues ADD COLUMN resolved_at TEXT")
                self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def add(self, events: list[dict[str, Any]]) -> int:
        """Insert events (idempotent by id); returns how many were new."""
        received = _now()
        new = 0
        with self._lock, self._conn:
            for e in events:
                fp = e["fingerprint"]
                row = self._conn.execute("SELECT installs, releases FROM issues WHERE fingerprint = ?", (fp,)).fetchone()
                if self._conn.execute("SELECT 1 FROM events WHERE id = ?", (e["id"],)).fetchone():
                    continue
                if row is None:
                    self._conn.execute(
                        "INSERT INTO issues (fingerprint, type, message, level, first_seen, last_seen, installs, "
                        "releases, frames_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (fp, e["type"], e["message"], e["level"], e["first_seen"], e["last_seen"],
                         json.dumps([e["install_id"]]), json.dumps([e["release"]]), json.dumps(e["frames"])))
                else:
                    self._conn.execute(
                        "UPDATE issues SET message = ?, level = ?, last_seen = max(last_seen, ?), "
                        "first_seen = min(first_seen, ?), installs = ?, releases = ?, frames_json = ? "
                        "WHERE fingerprint = ?",
                        (e["message"], e["level"], e["last_seen"], e["first_seen"],
                         _merge_list(row["installs"], e["install_id"]), _merge_list(row["releases"], e["release"]),
                         json.dumps(e["frames"]), fp))
                # a resolved issue that happens again is a regression: reopen it
                self._conn.execute("UPDATE issues SET events = events + 1, occurrences = occurrences + ?, "
                                   "status = 'open', resolved_at = NULL WHERE fingerprint = ?", (e["count"], fp))
                self._conn.execute("INSERT INTO events (id, fingerprint, received_at, body_json) VALUES (?, ?, ?, ?)",
                                   (e["id"], fp, received, json.dumps(e, separators=(",", ":"))))
                new += 1
        return new

    def prune(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM events WHERE received_at < ?", (cutoff,))
            self._conn.execute("DELETE FROM issues WHERE last_seen < ? AND fingerprint NOT IN "
                               "(SELECT fingerprint FROM events)", (cutoff,))
            return cur.rowcount

    def issues(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        where, args = ("WHERE status = ?", (status,)) if status else ("", ())
        with self._lock:
            rows = self._conn.execute(f"SELECT * FROM issues {where} ORDER BY last_seen DESC LIMIT ?",
                                      (*args, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["installs"] = len(json.loads(d["installs"]))
            d["releases"] = json.loads(d["releases"])
            d["frames"] = json.loads(d.pop("frames_json"))
            out.append(d)
        return out

    def set_status(self, fingerprint: str, status: str) -> bool:
        if status not in ("open", "resolved"):
            raise ValueError("status must be open or resolved")
        with self._lock, self._conn:
            cur = self._conn.execute("UPDATE issues SET status = ?, resolved_at = ? WHERE fingerprint = ?",
                                     (status, _now() if status == "resolved" else None, fingerprint))
            return cur.rowcount == 1

    def purge(self, older_than_days: int) -> int:
        """Delete events older than N days (and issues left without events); returns events deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM events WHERE received_at < ?", (cutoff,))
            self._conn.execute("DELETE FROM issues WHERE fingerprint NOT IN (SELECT fingerprint FROM events)")
            return cur.rowcount

    def counts(self) -> dict[str, int]:
        day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            c = self._conn
            return {"open_issues": c.execute("SELECT count(*) FROM issues WHERE status = 'open'").fetchone()[0],
                    "resolved_issues": c.execute("SELECT count(*) FROM issues WHERE status = 'resolved'").fetchone()[0],
                    "events_total": c.execute("SELECT count(*) FROM events").fetchone()[0],
                    "events_24h": c.execute("SELECT count(*) FROM events WHERE received_at >= ?",
                                            (day_ago,)).fetchone()[0]}

    def events_for(self, fingerprint: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT body_json FROM events WHERE fingerprint = ? ORDER BY received_at DESC "
                                      "LIMIT ?", (fingerprint, limit)).fetchall()
        return [json.loads(r["body_json"]) for r in rows]
