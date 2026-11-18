"""
Parser Studio backend (Phase 11): statement-format schemas, drafts → published versions → a
signed bundle that apps install (core/financial/parsers/dynamic.py verifies and re-validates it).

The service is deployed on its own, so the structural checks and regex-safety rules are restated
here; tests/test_admin_console.py checks them (and the bundle format) against the app's. Signing
uses the vendor key (RS_AI_LICENSE_SIGNING_KEY) with a distinct "IFPB1" prefix.

No statement data ever reaches this server: the Studio's Test Parse runs on the admin's machine.
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

BUNDLE_PREFIX = "IFPB1"
BUNDLE_FORMAT = "iflow-parser-bundle"
MAX_SCHEMA_BYTES = 32 * 1024
MAX_PATTERN = 300
ROLES = ("gross", "tips", "bonus", "fees", "tolls", "occurred_at", "paid_out", "reference", "description")
MONEY_ROLES = ("gross", "tips", "bonus", "fees", "tolls")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*[+*{]")

SCHEMA = """
CREATE TABLE IF NOT EXISTS parser_schemas (
    id            TEXT NOT NULL,
    version       INTEGER NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('draft', 'published')),
    schema_json   TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    updated_by    TEXT NOT NULL,
    published_at  TEXT,
    published_by  TEXT,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS integration_schemas (
    id            TEXT NOT NULL,
    version       INTEGER NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('draft', 'published')),
    schema_json   TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    updated_by    TEXT NOT NULL,
    published_at  TEXT,
    published_by  TEXT,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS parser_distributions (
    issued_at     TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    schemas       INTEGER NOT NULL,
    target        TEXT NOT NULL,
    published_by  TEXT NOT NULL,
    PRIMARY KEY (issued_at, sha256)
);
"""


class ParserSchemaError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_pattern(pattern: Any, what: str) -> None:
    if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN:
        raise ParserSchemaError(f"{what} must be 1–{MAX_PATTERN} characters")
    if _NESTED_QUANTIFIER.search(pattern):
        raise ParserSchemaError(f"{what} has a nested quantifier like (a+)+")
    if re.search(r"\\[1-9]|\(\?P=", pattern):
        raise ParserSchemaError(f"{what} can't use backreferences")
    try:
        if re.compile(pattern).groups == 0:
            raise ParserSchemaError(f"{what} needs a capture group")
    except re.error as exc:
        raise ParserSchemaError(f"{what} isn't a valid regular expression: {exc}") from None


def validate_schema(raw: Any) -> dict[str, Any]:
    """Structural + safety checks before a schema is stored or signed (the app re-validates fully)."""
    if not isinstance(raw, dict):
        raise ParserSchemaError("Schema must be a JSON object")
    if len(json.dumps(raw)) > MAX_SCHEMA_BYTES:
        raise ParserSchemaError("Schema is too large")
    if not _ID.match(str(raw.get("id", ""))):
        raise ParserSchemaError("Schema id: 3–50 lowercase letters, digits and dashes")
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= 1_000_000:
        raise ParserSchemaError("Version must be a whole number ≥ 1")
    for key in ("name", "platform"):
        if not isinstance(raw.get(key), str) or not raw[key].strip() or len(raw[key]) > 80:
            raise ParserSchemaError(f"'{key}' is required (up to 80 characters)")
    rules = raw.get("rules")
    if not isinstance(rules, dict) or "occurred_at" not in rules:
        raise ParserSchemaError("Map the trip / job date column")
    unknown = set(rules) - set(ROLES)
    if unknown:
        raise ParserSchemaError(f"Unknown role(s): {sorted(unknown)}")
    if not any(r in rules for r in MONEY_ROLES if r != "fees"):
        raise ParserSchemaError("Map at least one money column")
    for role, rule in rules.items():
        cols = rule.get("columns") if isinstance(rule, dict) else None
        if not isinstance(cols, list) or not cols or not all(isinstance(c, str) and 0 < len(c) <= 100 for c in cols):
            raise ParserSchemaError(f"{role}: list 1+ column names")
        if rule.get("combine", "first") not in ("first", "sum"):
            raise ParserSchemaError(f"{role}: combine must be 'first' or 'sum'")
        if rule.get("regex"):
            check_pattern(rule["regex"], f"{role} regex")
        bp = rule.get("multiplier_bp", 10_000)
        if isinstance(bp, bool) or not isinstance(bp, int) or not -100_000 <= bp <= 100_000:
            raise ParserSchemaError(f"{role}: multiplier must be whole basis points (-100000…100000)")
        if bp != 10_000 and role not in MONEY_ROLES:
            raise ParserSchemaError(f"{role}: multipliers only apply to money columns")
    return raw


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def now_iso() -> str:
    return _now()


def validate_integration(raw: Any) -> dict[str, Any]:
    """Structural checks for an API integration schema. The Admin Console validates the full model
    (core/integration/schema.py) before saving, and every app re-validates on install; the service is
    deployed on its own, so this only guards shape, size and the https-only rule."""
    if not isinstance(raw, dict):
        raise ParserSchemaError("Schema must be a JSON object")
    if len(json.dumps(raw)) > MAX_SCHEMA_BYTES:
        raise ParserSchemaError("Schema is too large")
    if not _ID.match(str(raw.get("id", ""))):
        raise ParserSchemaError("Integration id: 3–50 lowercase letters, digits and dashes")
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= 1_000_000:
        raise ParserSchemaError("Version must be a whole number ≥ 1")
    for key in ("name", "platform"):
        if not isinstance(raw.get(key), str) or not raw[key].strip() or len(raw[key]) > 80:
            raise ParserSchemaError(f"'{key}' is required (up to 80 characters)")
    if not str(raw.get("base_url", "")).startswith("https://"):
        raise ParserSchemaError("base_url must be https://")
    auth = raw.get("auth") or {}
    if not isinstance(auth, dict):
        raise ParserSchemaError("auth must be an object")
    for key in ("authorize_url", "token_url"):
        if auth.get(key) and not str(auth[key]).startswith("https://"):
            raise ParserSchemaError(f"auth.{key} must be https://")
    if not isinstance(raw.get("requests"), list) or not raw["requests"]:
        raise ParserSchemaError("List at least one request")
    return raw


def sign_bundle(schemas: list[dict[str, Any]], key: Ed25519PrivateKey, issued_at: str | None = None,
                integrations: list[dict[str, Any]] | None = None) -> str:
    body: dict[str, Any] = {"format": BUNDLE_FORMAT, "v": 1, "issued_at": issued_at or _now(), "schemas": schemas}
    if integrations:
        body["integrations"] = integrations
    payload = _b64e(canonical(body))
    return f"{BUNDLE_PREFIX}.{payload}.{_b64e(key.sign(f'{BUNDLE_PREFIX}.{payload}'.encode('ascii')))}"


class ParserStore:
    """Drafts → published versions. Statement formats by default; table="integration_schemas" with
    validate=validate_integration for API integrations (same lifecycle)."""

    def __init__(self, path: str | Path, *, table: str = "parser_schemas", validate: Any = None) -> None:
        if table not in ("parser_schemas", "integration_schemas"):
            raise ValueError(table)
        self.table, self.validate = table, validate or validate_schema
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript("PRAGMA journal_mode=WAL;" + SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT id, version, status, schema_json, updated_at, updated_by, published_at "
                                      f"FROM {self.table} ORDER BY id, version DESC").fetchall()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            item = out.setdefault(r["id"], {"id": r["id"], "draft": None, "published": None})
            s = json.loads(r["schema_json"])
            entry = {"version": r["version"], "name": s["name"], "platform": s["platform"],
                     "updated_at": r["updated_at"], "updated_by": r["updated_by"], "published_at": r["published_at"]}
            if r["status"] == "draft" and item["draft"] is None:
                item["draft"] = entry
            if r["status"] == "published" and item["published"] is None:
                item["published"] = entry
        return list(out.values())

    def get(self, schema_id: str) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(f"SELECT * FROM {self.table} WHERE id = ? ORDER BY version DESC",
                                      (schema_id,)).fetchall()
        if not rows:
            raise KeyError(schema_id)
        return {"id": schema_id, "versions": [{"version": r["version"], "status": r["status"],
                                               "schema": json.loads(r["schema_json"]), "updated_at": r["updated_at"],
                                               "updated_by": r["updated_by"], "published_at": r["published_at"],
                                               "published_by": r["published_by"]} for r in rows]}

    def save_draft(self, schema: dict[str, Any], actor: str) -> dict[str, Any]:
        schema = self.validate(schema)
        with self._lock, self._conn:
            latest = self._conn.execute(f"SELECT max(version) FROM {self.table} WHERE id = ? AND status = 'published'",
                                        (schema["id"],)).fetchone()[0] or 0
            if schema["version"] <= latest:
                raise ParserSchemaError(f"Version must be greater than the published v{latest}")
            self._conn.execute(f"DELETE FROM {self.table} WHERE id = ? AND status = 'draft'", (schema["id"],))
            self._conn.execute(f"INSERT INTO {self.table} (id, version, status, schema_json, updated_at, updated_by) "
                               "VALUES (?, ?, 'draft', ?, ?, ?)",
                               (schema["id"], schema["version"], json.dumps(schema, sort_keys=True), _now(), actor))
        return {"id": schema["id"], "version": schema["version"], "status": "draft"}

    def publish(self, schema_id: str, actor: str) -> dict[str, Any]:
        with self._lock, self._conn:
            row = self._conn.execute(f"SELECT version, schema_json FROM {self.table} WHERE id = ? AND status = 'draft'",
                                     (schema_id,)).fetchone()
            if row is None:
                raise KeyError(schema_id)
            self.validate(json.loads(row["schema_json"]))
            self._conn.execute(f"UPDATE {self.table} SET status = 'published', published_at = ?, published_by = ? "
                               "WHERE id = ? AND version = ?", (_now(), actor, schema_id, row["version"]))
        return {"id": schema_id, "version": row["version"], "status": "published"}

    def published(self) -> list[dict[str, Any]]:
        """Latest published version of every schema (what the bundle contains)."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT p.schema_json FROM {self.table} p WHERE p.status = 'published' AND p.version = "
                f"(SELECT max(version) FROM {self.table} q WHERE q.id = p.id AND q.status = 'published') "
                "ORDER BY p.id").fetchall()
        return [json.loads(r["schema_json"]) for r in rows]

    def record_distribution(self, published: dict[str, Any], actor: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO parser_distributions (issued_at, sha256, schemas, target, "
                               "published_by) VALUES (?, ?, ?, ?, ?)",
                               (published["issued_at"], published["sha256"], published["schemas"],
                                published["target"], actor))

    def last_distribution(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM parser_distributions ORDER BY issued_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None

