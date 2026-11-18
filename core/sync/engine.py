"""
Sync engine: pushes this device's changes and pulls everyone else's, end-to-end encrypted.

What syncs    every table with the sync columns (version / sync_status / …), except local-only
              ones (trip zones, device settings, caches, sync bookkeeping). Tables go in foreign-key
              order, so parents are pushed before children.
What doesn't  bookkeeping columns (version, sync_status, last_synced_at) and device-local columns:
              a receipt's image path/storage mode. A receipt that arrives from another device is
              stored as `linked` with a "remote:<sha256>" placeholder until its image is available.

Push   pending rows → {table, row, device} JSON → sealed with version = last seen server version + 1
       → accepted rows are marked synced (only if not edited meanwhile). A conflict (another device
       pushed first) is resolved like a pulled record, then pushed again if the local copy won.
Pull   everything after this device's cursor, applied in ONE transaction with deferred foreign keys
       (a child can arrive before its parent), cursor saved in the same transaction.
Merge  a row with no local edits takes the remote copy. When both sides changed it, the newest
       updated_at wins (ties: device id, then content hash, so every device picks the same winner);
       the losing copy goes to sync_conflicts on this device for review. Deletes are soft
       (deleted_at), so they merge like any other edit.

Remote rows are written with an explicit sync_status, which bypasses the auto-touch triggers:
applying a pull never marks rows pending (no echo loops).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from core.sync.crypto import SyncCryptoError, SyncKeys

log = logging.getLogger(__name__)

LOCAL_ONLY_TABLES = frozenset({"trip_zones", "device_settings", "sync_meta", "sync_versions", "sync_conflicts",
                               "sync_aliases", "demand_feed_cache", "vehicle_decode_cache",
                               "vendor_parsers", "vendor_integrations"})
BOOKKEEPING = frozenset({"version", "sync_status", "last_synced_at"})
LOCAL_COLUMNS: dict[str, frozenset[str]] = {"receipts": frozenset({"file_path", "storage_mode"})}
PUSH_BATCH = 100


@dataclass(frozen=True)
class NaturalKey:
    """A unique index on a synced table, restated so a collision can be found before writing.
    `where` matches live local rows equal to the incoming row (params from `columns`);
    `applies` says whether the incoming row is covered by the index at all.
    action "merge": same real-world thing → keep the smaller id, soft-delete the other, alias it.
    action "demote": a one-per-scope flag → the loser keeps existing, the flag is cleared."""

    index: str
    where: str
    columns: tuple[str, ...]
    applies: Callable[[dict[str, Any]], bool]
    action: str = "merge"
    demote: tuple[tuple[str, Any], ...] = ()          # "demote": columns to set on the losing row


def _live(r: dict[str, Any]) -> bool:
    return r.get("deleted_at") is None


NATURAL_KEYS: dict[str, tuple[NaturalKey, ...]] = {
    "platforms": (NaturalKey("ux_platforms_name_live", "name = ? COLLATE NOCASE", ("name",), _live),),
    "import_mappings": (NaturalKey("ux_import_mappings_name_live", "name = ? COLLATE NOCASE", ("name",), _live),),
    "vehicles": (
        NaturalKey("ux_vehicles_vin_live", "vin IS NOT NULL AND vin = ?", ("vin",),
                   lambda r: _live(r) and r.get("vin") is not None),
        NaturalKey("ux_vehicles_nickname_live", "nickname = ? COLLATE NOCASE", ("nickname",), _live),
    ),
    "tax_profiles": (
        NaturalKey("ux_tax_profiles_year_jurisdiction",
                   "tax_year = ? AND country_code = ? AND ifnull(region_code, '') = ifnull(?, '')",
                   ("tax_year", "country_code", "region_code"), _live),
        NaturalKey("ux_tax_profiles_default_per_year", "is_default = 1 AND tax_year = ?", ("tax_year",),
                   lambda r: _live(r) and r.get("is_default") == 1, "demote", (("is_default", 0),)),
    ),
    "transactions": (NaturalKey("ux_transactions_external_ref",
                                "external_ref IS NOT NULL AND platform_id = ? AND external_ref = ?",
                                ("platform_id", "external_ref"),
                                lambda r: _live(r) and r.get("external_ref") is not None
                                and r.get("platform_id") is not None),),
    "mileage_logs": (NaturalKey("ux_mileage_one_active_trip",
                                "trip_state = 'active' AND ifnull(vehicle_id, '') = ifnull(?, '')", ("vehicle_id",),
                                lambda r: _live(r) and r.get("trip_state") == "active"),),
    "maintenance_schedules": (NaturalKey("ux_maintenance_schedules_service",
                                         "vehicle_id = ? AND service = ? COLLATE NOCASE", ("vehicle_id", "service"),
                                         lambda r: _live(r) and r.get("vehicle_id") is not None),),
    "health_premium_entries": (NaturalKey("ux_health_entries_txn_plan",
                                          "transaction_id IS NOT NULL AND transaction_id = ? AND plan_id = ?",
                                          ("transaction_id", "plan_id"),
                                          lambda r: _live(r) and r.get("transaction_id") is not None),),
}


def _remote_defaults(table: str, row: dict[str, Any]) -> dict[str, Any]:
    if table == "receipts":
        return {"file_path": f"remote:{row.get('sha256') or 'unknown'}", "storage_mode": "linked"}
    return {}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class SyncError(Exception):
    pass


class Api(Protocol):
    def api(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]: ...


@dataclass
class SyncReport:
    pushed: int = 0
    pulled: int = 0
    applied: int = 0
    conflicts: int = 0
    unknown: int = 0
    tables: dict[str, int] = field(default_factory=dict)


def synced_tables(conn: Any) -> list[str]:
    """Tables with sync columns, parents before children (foreign-key order)."""
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    tables = [t for t in names if t not in LOCAL_ONLY_TABLES
              and any(r[1] == "sync_status" for r in conn.execute(f'PRAGMA table_info("{t}")'))]
    deps = {t: {r[2] for r in conn.execute(f'PRAGMA foreign_key_list("{t}")') if r[2] in tables and r[2] != t}
            for t in tables}
    ordered: list[str] = []
    while deps:
        ready = sorted(t for t, d in deps.items() if not d - set(ordered))
        if not ready:
            raise SyncError(f"foreign-key cycle among {sorted(deps)}")
        ordered += ready
        for t in ready:
            del deps[t]
    return ordered


class SyncEngine:
    def __init__(self, db: Any, api: Api, keys: SyncKeys, *, device_id: str,
                 clock: Callable[[], str] = _now) -> None:
        self.db, self.api, self.keys, self.device_id, self.clock = db, api, keys, device_id, clock
        with db.connection() as conn:
            self.tables = synced_tables(conn)
            self.columns = {t: [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')] for t in self.tables}
            # table → [(fk column, parent table)] and parent → [(child table, fk column)]
            self.fks = {t: [(r[3], r[2]) for r in conn.execute(f'PRAGMA foreign_key_list("{t}")') if r[2] in self.tables]
                        for t in self.tables}
        self.children: dict[str, list[tuple[str, str]]] = {}
        for child, refs in self.fks.items():
            for col, parent in refs:
                self.children.setdefault(parent, []).append((child, col))
        self.by_collection = {keys.collection_id(t): t for t in self.tables}
        self._cursor_key = f"pull_cursor:{keys.account_id}"
        self._tables_key = f"pull_tables:{keys.account_id}"

    # --- public -----------------------------------------------------------------------------------

    def sync(self) -> SyncReport:
        report = SyncReport()
        self.pull(report)
        for _ in range(3):                     # a lost push race resolves on the next round
            if not self.push(report):
                break
        return report

    def pull(self, report: SyncReport | None = None) -> SyncReport:
        report = report or SyncReport()
        cursor = self._cursor()
        items: list[dict[str, Any]] = []
        while True:
            page = self.api.api("GET", f"/v1/sync/pull?after={cursor}&limit=500")
            items.extend(page["items"])
            cursor = int(page["cursor"])
            if not page.get("more"):
                break
        report.pulled += len(items)
        with self.db.transaction() as conn:
            conn.execute("PRAGMA defer_foreign_keys = ON")
            for item in items:
                self._apply_item(conn, item, report)
            self._set_meta(conn, self._cursor_key, str(cursor))
            self._set_meta(conn, self._tables_key, self._tables_signature())
        return report

    def push(self, report: SyncReport | None = None) -> int:
        """Push pending rows; returns how many came back as conflicts (0 = all accepted)."""
        report = report or SyncReport()
        pending = self._pending()
        conflicts = 0
        for start in range(0, len(pending), PUSH_BATCH):
            batch = pending[start:start + PUSH_BATCH]
            items = []
            for table, row, base in batch:
                cid = self.keys.collection_id(table)
                blob = self.keys.seal(cid, row["id"], base + 1, self._payload(table, row))
                items.append({"collection": cid, "record_id": row["id"], "base_version": base,
                              "ciphertext": base64.b64encode(blob).decode("ascii")})
            resp = self.api.api("POST", "/v1/sync/push", {"items": items})
            with self.db.transaction() as conn:
                conn.execute("PRAGMA defer_foreign_keys = ON")
                for (table, row, _), result in zip(batch, resp["results"]):
                    if result["status"] == "ok":
                        conn.execute(f'UPDATE "{table}" SET sync_status = \'synced\', last_synced_at = ? '
                                     "WHERE id = ? AND version = ? AND sync_status = 'pending'",
                                     (self.clock(), row["id"], row["version"]))
                        self._set_version(conn, table, row["id"], int(result["version"]))
                        report.pushed += 1
                        report.tables[table] = report.tables.get(table, 0) + 1
                    else:
                        conflicts += 1
                        if result.get("ciphertext"):
                            self._apply_item(conn, {**result, "collection": self.keys.collection_id(table)}, report)
        return conflicts

    def pending_count(self) -> int:
        with self.db.connection() as conn:
            return sum(conn.execute(f"SELECT count(*) FROM \"{t}\" WHERE sync_status = 'pending'").fetchone()[0]
                       for t in self.tables)

    # --- internals --------------------------------------------------------------------------------

    def _tables_signature(self) -> str:
        return hashlib.sha256(",".join(self.tables).encode()).hexdigest()[:16]

    def _cursor(self) -> int:
        with self.db.connection() as conn:
            row = conn.execute("SELECT value FROM sync_meta WHERE key = ?", (self._cursor_key,)).fetchone()
            tables = conn.execute("SELECT value FROM sync_meta WHERE key = ?", (self._tables_key,)).fetchone()
        if row is None or tables is None or tables[0] != self._tables_signature():
            return 0            # first sync, or this app version syncs new tables: read everything again
        return int(row[0])

    @staticmethod
    def _set_meta(conn: Any, key: str, value: str) -> None:
        conn.execute("INSERT INTO sync_meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET "
                     "value = excluded.value, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')", (key, value))

    @staticmethod
    def _set_version(conn: Any, table: str, record_id: str, version: int) -> None:
        conn.execute("INSERT INTO sync_versions (table_name, record_id, server_version) VALUES (?, ?, ?) "
                     "ON CONFLICT (table_name, record_id) DO UPDATE SET server_version = "
                     "max(server_version, excluded.server_version)", (table, record_id, version))

    @staticmethod
    def _known_version(conn: Any, table: str, record_id: str) -> int:
        row = conn.execute("SELECT server_version FROM sync_versions WHERE table_name = ? AND record_id = ?",
                           (table, record_id)).fetchone()
        return int(row[0]) if row else 0

    def _pending(self) -> list[tuple[str, dict[str, Any], int]]:
        out = []
        with self.db.connection() as conn:
            for table in self.tables:
                for row in conn.execute(f"SELECT * FROM \"{table}\" WHERE sync_status = 'pending' ORDER BY created_at"):
                    row = dict(row)
                    out.append((table, row, self._known_version(conn, table, row["id"])))
        return out

    def _payload(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        skip = BOOKKEEPING | LOCAL_COLUMNS.get(table, frozenset())
        return {"t": table, "dev": self.device_id, "row": {k: v for k, v in row.items() if k not in skip}}

    def _apply_item(self, conn: Any, item: dict[str, Any], report: SyncReport) -> None:
        table = self.by_collection.get(item["collection"])
        if table is None:
            report.unknown += 1           # from a newer app version; re-read when this app learns the table
            return
        version = int(item["version"])
        if self._known_version(conn, table, item["record_id"]) >= version:
            return                        # our own push echoing back, or already applied
        try:
            payload = self.keys.open(item["collection"], item["record_id"], version,
                                     base64.b64decode(item["ciphertext"]))
        except (SyncCryptoError, ValueError) as exc:
            raise SyncError(f"A synced record failed decryption ({exc}); sync stopped to protect your data") from exc
        if payload.get("t") != table or payload.get("row", {}).get("id") != item["record_id"]:
            raise SyncError("A synced record doesn't match its label; sync stopped to protect your data")
        self._merge(conn, table, payload, report)
        self._set_version(conn, table, item["record_id"], version)
        report.applied += 1

    def _merge(self, conn: Any, table: str, payload: dict[str, Any], report: SyncReport) -> None:
        remote = dict(payload["row"])
        repointed = False
        for col, parent in self.fks[table]:              # children of a merged-away duplicate follow the survivor
            if remote.get(col) is not None:
                alias = conn.execute("SELECT new_id FROM sync_aliases WHERE table_name = ? AND old_id = ?",
                                     (parent, remote[col])).fetchone()
                if alias:
                    remote[col], repointed = alias[0], True
        payload = {**payload, "row": remote}
        if self._resolve_duplicates(conn, table, payload, report):
            return
        self._write(conn, table, payload, report)
        if repointed:                                    # tell the other devices about the new parent
            conn.execute(f'UPDATE "{table}" SET sync_status = \'pending\' WHERE id = ?', (remote["id"],))

    def _resolve_duplicates(self, conn: Any, table: str, payload: dict[str, Any], report: SyncReport) -> bool:
        """Handle live local rows that share a natural key with the incoming row. Returns True when
        the incoming row lost and has been stored as a tombstone (nothing more to write)."""
        remote = payload["row"]
        for key in NATURAL_KEYS.get(table, ()):
            if not key.applies(remote):
                continue
            rows = conn.execute(f'SELECT * FROM "{table}" WHERE deleted_at IS NULL AND id <> ? AND {key.where}',
                                (remote["id"], *[remote.get(c) for c in key.columns])).fetchall()
            for other in map(dict, rows):
                local_payload = self._payload(table, other)
                if key.action == "demote":                 # smaller id keeps the flag, on every device
                    if remote["id"] < other["id"]:
                        sets = ", ".join(f'"{c}" = ?' for c, _ in key.demote)
                        conn.execute(f'UPDATE "{table}" SET {sets} WHERE id = ?',
                                     (*[v for _, v in key.demote], other["id"]))
                    else:
                        remote.update(dict(key.demote))
                        payload["row"] = remote
                    continue
                self._record_conflict(conn, table, remote["id"], "remote" if remote["id"] < other["id"] else "local",
                                      local_payload, payload)
                report.conflicts += 1
                if remote["id"] < other["id"]:           # the incoming row survives; retire ours
                    self._retire(conn, table, other["id"], remote["id"])
                else:                                    # ours survives; store theirs as a tombstone
                    tomb = {**remote, "deleted_at": remote.get("updated_at") or self.clock()}
                    self._write(conn, table, {**payload, "row": tomb}, report)
                    conn.execute(f'UPDATE "{table}" SET sync_status = \'pending\' WHERE id = ?', (remote["id"],))
                    self._alias(conn, table, remote["id"], other["id"])
                    return True
        return False

    def _retire(self, conn: Any, table: str, old_id: str, new_id: str) -> None:
        """Soft-delete a local duplicate and move its children to the survivor (both sync as edits)."""
        self._alias(conn, table, old_id, new_id)
        conn.execute(f'UPDATE "{table}" SET deleted_at = ? WHERE id = ?', (self.clock(), old_id))
        for child, col in self.children.get(table, []):
            conn.execute(f'UPDATE "{child}" SET "{col}" = ? WHERE "{col}" = ?', (new_id, old_id))

    @staticmethod
    def _alias(conn: Any, table: str, old_id: str, new_id: str) -> None:
        conn.execute("INSERT INTO sync_aliases (table_name, old_id, new_id) VALUES (?, ?, ?) "
                     "ON CONFLICT (table_name, old_id) DO UPDATE SET new_id = excluded.new_id", (table, old_id, new_id))
        conn.execute("UPDATE sync_aliases SET new_id = ? WHERE table_name = ? AND new_id = ?", (new_id, table, old_id))

    def _write(self, conn: Any, table: str, payload: dict[str, Any], report: SyncReport) -> None:
        remote = payload["row"]
        cols = self.columns[table]
        keep = [c for c in cols if c in remote and c not in BOOKKEEPING and c not in LOCAL_COLUMNS.get(table, ())
                and c != "id"]
        local = conn.execute(f'SELECT * FROM "{table}" WHERE id = ?', (remote["id"],)).fetchone()
        now = self.clock()
        if local is None:
            row = {**{c: remote[c] for c in keep}, **_remote_defaults(table, remote), "id": remote["id"],
                   "sync_status": "synced", "last_synced_at": now}
            names = ", ".join(f'"{c}"' for c in row)
            conn.execute(f'INSERT INTO "{table}" ({names}) VALUES ({", ".join("?" * len(row))})', tuple(row.values()))
            return
        local = dict(local)
        if local["sync_status"] == "pending":
            local_payload = self._payload(table, local)
            if self._winner(local_payload, payload) is local_payload:
                self._record_conflict(conn, table, remote["id"], "local", local_payload, payload)
                report.conflicts += 1
                return                    # stays pending; next push overwrites the server copy
            self._record_conflict(conn, table, remote["id"], "remote", local_payload, payload)
            report.conflicts += 1
        # Two updates, each changing sync_status, so the auto-touch trigger never fires.
        # created_at is immutable (enforced by a trigger) and identical for a record everywhere.
        changed = [c for c in keep if c != "created_at"]
        sets = ", ".join(f'"{c}" = ?' for c in changed)
        conn.execute(f'UPDATE "{table}" SET {sets}, sync_status = CASE WHEN sync_status = \'conflict\' '
                     "THEN 'pending' ELSE 'conflict' END, last_synced_at = ? WHERE id = ?",
                     (*[remote[c] for c in changed], now, remote["id"]))
        conn.execute(f'UPDATE "{table}" SET sync_status = \'synced\' WHERE id = ?', (remote["id"],))

    @staticmethod
    def _winner(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
        def rank(p: dict[str, Any]) -> tuple[str, str, str]:
            body = json.dumps(p["row"], sort_keys=True, separators=(",", ":"))
            return (str(p["row"].get("updated_at") or ""), str(p.get("dev") or ""),
                    hashlib.sha256(body.encode()).hexdigest())
        return a if rank(a) > rank(b) else b

    @staticmethod
    def _record_conflict(conn: Any, table: str, record_id: str, kept: str, local: dict[str, Any],
                         remote: dict[str, Any]) -> None:
        conn.execute("INSERT INTO sync_conflicts (id, table_name, record_id, kept, local_json, remote_json) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (str(uuid.uuid4()), table, record_id, kept, json.dumps(local["row"], sort_keys=True),
                      json.dumps(remote["row"], sort_keys=True)))
