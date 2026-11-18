"""
Dynamic statement parser schemas (Parser Studio, Phase 11).

A *parser schema* is a versioned, vendor-published statement format: a MappingProfile (the same
engine every import uses, core/financial/ingestion.py) plus identity (id, version, platform) and a
header signature for auto-detection. Admins design and test schemas in the Admin Console's Parser
Studio; publishing produces a **signed bundle** that apps download or import.

Roles (what the Studio maps) → engine fields
--------------------------------------------
    gross       Gross fare / base pay        → amounts["earning"]
    tips        Tips                         → amounts["tip"]
    bonus       Quests / bonuses / surge     → amounts["bonus"]
    fees        Platform service fees        → fee (always stored as a positive deduction)
    tolls       Tolls & pass-through         → amounts["reimbursement"] (not income, not a fee)
    occurred_at Trip / job timestamp         → date            (required)
    paid_out    Payout / deposit date        → paid_out
    reference   Trip / order id              → external_ref    (enables exact de-duplication)
    description Free text                    → description

Each role takes one or more columns (aliases, or summed), an optional regex extractor (group
"value" or the first group) and a multiplier in basis points (10000 = ×1, -10000 flips the sign,
e.g. for statements that print fees as negatives or tips in a different unit).

Trust
-----
Bundles are signed with the vendor Ed25519 key (the same key that signs licenses; a distinct
"IFPB1" prefix keeps the two formats from ever being confused). Apps install only bundles that
verify against their trusted vendor keys, never older versions over newer ones, and every
schema is re-validated locally (including the regex safety rules) before it's stored in the
encrypted database. Test Parse runs on the admin's machine: sample statement lines are never
uploaded anywhere.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from core.financial.ingestion import (
    DEFAULT_DATE_FORMATS,
    FieldSource,
    IngestionError,
    MappingProfile,
    _norm_header,
    map_rows,
)

BUNDLE_PREFIX = "IFPB1"
BUNDLE_FORMAT = "iflow-parser-bundle"
SCHEMA_FORMAT = 1
MAX_BUNDLE_CHARS = 512 * 1024
MAX_SAMPLE_CHARS = 64 * 1024
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")

ROLES: dict[str, dict[str, Any]] = {
    "gross": {"label": "Gross fare / base pay", "kind": "earning"},
    "tips": {"label": "Tips", "kind": "tip"},
    "bonus": {"label": "Quests & bonuses", "kind": "bonus"},
    "fees": {"label": "Platform service fees", "field": "fee"},
    "tolls": {"label": "Tolls & pass-through", "kind": "reimbursement"},
    "occurred_at": {"label": "Trip / job date & time", "field": "date", "required": True},
    "paid_out": {"label": "Payout date", "field": "paid_out"},
    "reference": {"label": "Trip / order ID", "field": "external_ref"},
    "description": {"label": "Description", "field": "description"},
}
MONEY_ROLES = ("gross", "tips", "bonus", "fees", "tolls")


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class RoleRule:
    columns: tuple[str, ...]
    combine: str = "first"            # "first" (aliases) | "sum"
    regex: str | None = None
    multiplier_bp: int = 10_000

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"columns": list(self.columns)}
        if self.combine != "first":
            d["combine"] = self.combine
        if self.regex:
            d["regex"] = self.regex
        if self.multiplier_bp != 10_000:
            d["multiplier_bp"] = self.multiplier_bp
        return d

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RoleRule:
        cols = raw.get("columns") or []
        if isinstance(cols, str):
            cols = [cols]
        return cls(tuple(str(c) for c in cols if str(c).strip()), str(raw.get("combine", "first")),
                   (str(raw["regex"]) if raw.get("regex") else None), int(raw.get("multiplier_bp", 10_000)))


@dataclass(frozen=True)
class ParserSchema:
    id: str
    version: int
    name: str
    platform: str
    rules: Mapping[str, RoleRule]
    date_formats: tuple[str, ...] = DEFAULT_DATE_FORMATS
    timezone: str | None = None
    currency: str = "USD"
    decimal_separator: str = "."
    delimiter: str | None = None
    detect_headers: tuple[str, ...] = ()
    notes: str = ""
    profile: MappingProfile = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not _ID.match(self.id or ""):
            raise SchemaError("Schema id: 3–50 lowercase letters, digits and dashes (e.g. uber-weekly-2026)")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or not 1 <= self.version <= 1_000_000:
            raise SchemaError("Version must be a whole number ≥ 1")
        unknown = set(self.rules) - set(ROLES)
        if unknown:
            raise SchemaError(f"Unknown role(s): {sorted(unknown)}")
        if "occurred_at" not in self.rules or not self.rules["occurred_at"].columns:
            raise SchemaError("Map the trip / job date column")
        if not any(r in self.rules and self.rules[r].columns for r in MONEY_ROLES if r != "fees"):
            raise SchemaError("Map at least one money column (gross, tips, quests or tolls)")
        for role, rule in self.rules.items():
            if not rule.columns:
                raise SchemaError(f"{ROLES[role]['label']}: pick at least one column")
            if rule.multiplier_bp != 10_000 and role not in MONEY_ROLES:
                raise SchemaError(f"{ROLES[role]['label']}: multipliers only apply to money columns")
        try:
            object.__setattr__(self, "profile", self._compile())
        except IngestionError as exc:
            raise SchemaError(str(exc)) from None
        object.__setattr__(self, "rules", dict(self.rules))
        object.__setattr__(self, "date_formats", tuple(self.date_formats))
        object.__setattr__(self, "detect_headers", tuple(self.detect_headers))

    @property
    def display_name(self) -> str:
        return f"{self.name} (iFlow format v{self.version})"

    def _compile(self) -> MappingProfile:
        amounts: dict[str, FieldSource] = {}
        extractors: dict[str, str] = {}
        multipliers: dict[str, int] = {}
        extra: dict[str, FieldSource] = {}
        for role, rule in self.rules.items():
            spec = ROLES[role]
            source = FieldSource(rule.columns, rule.combine)       # validates combine
            if rule.regex:
                for column in rule.columns:
                    extractors[column] = rule.regex
            if "kind" in spec:
                amounts[spec["kind"]] = source
                if rule.multiplier_bp != 10_000:
                    multipliers[spec["kind"]] = rule.multiplier_bp
            else:
                extra[spec["field"]] = source
                if role == "fees" and rule.multiplier_bp != 10_000:
                    multipliers["fee"] = rule.multiplier_bp
        return MappingProfile(
            name=self.display_name, platform=self.platform, date=extra.pop("date"), amounts=amounts,
            fee=extra.get("fee"), paid_out=extra.get("paid_out"), external_ref=extra.get("external_ref"),
            description=extra.get("description"), default_currency=self.currency, date_formats=self.date_formats,
            timezone=self.timezone, decimal_separator=self.decimal_separator, delimiter=self.delimiter,
            extractors=extractors, multipliers_bp=multipliers,
            detect_headers=self.detect_headers or tuple(self.rules["occurred_at"].columns[:1]),
        )

    # --- serialization ---

    def to_dict(self) -> dict[str, Any]:
        return {"format": SCHEMA_FORMAT, "id": self.id, "version": self.version, "name": self.name,
                "platform": self.platform, "rules": {k: v.to_dict() for k, v in self.rules.items()},
                "date_formats": list(self.date_formats), "timezone": self.timezone, "currency": self.currency,
                "decimal_separator": self.decimal_separator, "delimiter": self.delimiter,
                "detect_headers": list(self.detect_headers), "notes": self.notes}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ParserSchema:
        if not isinstance(raw, Mapping):
            raise SchemaError("Schema must be a JSON object")
        if raw.get("format", SCHEMA_FORMAT) > SCHEMA_FORMAT:
            raise SchemaError("This schema was made for a newer iFlow Core; update the app")
        try:
            return cls(
                id=str(raw["id"]), version=int(raw.get("version", 1)), name=str(raw["name"]).strip()[:80],
                platform=str(raw["platform"]).strip()[:60],
                rules={str(k): RoleRule.from_dict(v) for k, v in dict(raw.get("rules") or {}).items()},
                date_formats=tuple(raw.get("date_formats") or DEFAULT_DATE_FORMATS),
                timezone=raw.get("timezone") or None, currency=str(raw.get("currency") or "USD"),
                decimal_separator=str(raw.get("decimal_separator") or "."), delimiter=raw.get("delimiter") or None,
                detect_headers=tuple(raw.get("detect_headers") or ()), notes=str(raw.get("notes") or "")[:500])
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, SchemaError):
                raise
            raise SchemaError(f"Schema is incomplete or invalid: {exc}") from None

    def matches(self, headers: Iterable[str]) -> bool:
        present = {_norm_header(h) for h in headers}
        return all(_norm_header(h) in present for h in self.profile.detect_headers)


# --- Test Parse (the Studio's bench) --------------------------------------------------------------


@dataclass(frozen=True)
class PreviewRow:
    line: int
    occurred_at: str | None
    paid_out_at: str | None
    gross_minor: int
    tips_minor: int
    bonus_minor: int
    tolls_minor: int
    fees_minor: int
    currency: str
    reference: str | None

    @property
    def net_minor(self) -> int:
        """What the driver kept: income − fees (tolls are pass-through, so they're excluded)."""
        return self.gross_minor + self.tips_minor + self.bonus_minor - self.fees_minor


@dataclass
class Preview:
    rows: list[PreviewRow] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    skipped: int = 0

    def total(self, attr: str) -> int:
        return sum(getattr(r, attr) for r in self.rows)


def sample_headers(sample: str, delimiter: str | None = None) -> list[str]:
    """Header names from the first line of a pasted CSV sample (for the Studio's column pickers)."""
    text = (sample or "")[:MAX_SAMPLE_CHARS].lstrip("﻿")
    first = text.splitlines()[0] if text.strip() else ""
    if not first:
        return []
    dialect_delim = delimiter or (csv.Sniffer().sniff(first, delimiters=",;\t|").delimiter if first else ",")
    return [h.strip() for h in next(csv.reader([first], delimiter=dialect_delim))]


def test_parse(schema: ParserSchema, sample: str, *, timezone_name: str | None = None) -> Preview:
    """Run a pasted CSV sample (header line + rows) through the schema. Local only; nothing is sent."""
    text = (sample or "")[:MAX_SAMPLE_CHARS].lstrip("﻿")
    preview = Preview(headers=sample_headers(text, schema.delimiter))
    if not preview.headers:
        preview.errors.append((1, "Paste a header line and at least one row"))
        return preview
    delim = schema.delimiter or csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;\t|").delimiter
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    header = [_norm_header(h) for h in next(reader)]
    missing = [c for rule in schema.rules.values() for c in rule.columns if _norm_header(c) not in header]
    if missing:
        preview.errors.append((1, f"Column(s) not in the sample header: {', '.join(sorted(set(missing)))}"))
        return preview
    rows = [(i, dict(zip(header, cells))) for i, cells in enumerate(reader, start=2) if any(c.strip() for c in cells)]
    tz = timezone_name or schema.timezone or "UTC"
    result = map_rows(rows, schema.profile, timezone_name=tz)
    preview.skipped = result.rows_skipped
    preview.errors += [(e.line, e.message) for e in result.errors]
    by_line: dict[int, dict[str, Any]] = {}
    kind_to_role = {"earning": "gross_minor", "tip": "tips_minor", "bonus": "bonus_minor",
                    "reimbursement": "tolls_minor"}
    for rec in result.records:
        row = by_line.setdefault(rec.source_line, {"occurred_at": rec.occurred_at, "paid_out_at": rec.paid_out_at,
                                                   "currency": rec.currency, "reference": rec.external_ref.split("#")[0],
                                                   "gross_minor": 0, "tips_minor": 0, "bonus_minor": 0,
                                                   "tolls_minor": 0, "fees_minor": 0})
        slot = kind_to_role.get(rec.kind, "gross_minor")
        row[slot] += rec.gross_amount_minor
        row["fees_minor"] += rec.platform_fee_minor
    for line in sorted(by_line):
        preview.rows.append(PreviewRow(line=line, **by_line[line]))
    return preview


# --- signed bundles --------------------------------------------------------------------------------------


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_bundle(schemas: Iterable[ParserSchema | Mapping[str, Any]], key: Ed25519PrivateKey,
                *, issued_at: datetime | None = None, integrations: Iterable[Mapping[str, Any]] = ()) -> str:
    items = [s.to_dict() if isinstance(s, ParserSchema) else dict(s) for s in schemas]
    body: dict[str, Any] = {"format": BUNDLE_FORMAT, "v": 1, "schemas": items,
                            "issued_at": (issued_at or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    extra = [i.to_dict() if hasattr(i, "to_dict") else dict(i) for i in integrations]
    if extra:                          # older apps ignore the key, so bundles stay backward compatible
        body["integrations"] = extra
    payload = _b64e(canonical(body))
    return f"{BUNDLE_PREFIX}.{payload}.{_b64e(key.sign(f'{BUNDLE_PREFIX}.{payload}'.encode('ascii')))}"


@dataclass(frozen=True)
class Bundle:
    issued_at: str
    schemas: tuple[ParserSchema, ...]
    integrations: tuple[Any, ...] = ()               # core.integration.schema.IntegrationSchema
    skipped_integrations: tuple[str, ...] = ()       # newer-format or invalid ones (reasons)


def verify_bundle(text: str, public_keys_hex: Iterable[str]) -> Bundle:
    text = "".join((text or "").split())
    if not text or len(text) > MAX_BUNDLE_CHARS:
        raise SchemaError("That isn't an iFlow Core statement-format file")
    parts = text.split(".")
    if len(parts) != 3 or parts[0] != BUNDLE_PREFIX:
        raise SchemaError("That isn't an iFlow Core statement-format file")
    signed = f"{BUNDLE_PREFIX}.{parts[1]}".encode("ascii")
    try:
        signature = _b64d(parts[2])
    except ValueError:
        raise SchemaError("Statement-format file is damaged") from None
    for hex_key in public_keys_hex:
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(hex_key)).verify(signature, signed)
            break
        except (InvalidSignature, ValueError):
            continue
    else:
        raise SchemaError("This statement-format file isn't signed by iFlow Core (or this build doesn't trust "
                          "its key)")
    try:
        body = json.loads(_b64d(parts[1]))
    except (ValueError, UnicodeDecodeError):
        raise SchemaError("Statement-format file is damaged") from None
    if not isinstance(body, dict) or body.get("format") != BUNDLE_FORMAT or body.get("v") != 1:
        raise SchemaError("Unsupported statement-format file; update the app")
    schemas = tuple(ParserSchema.from_dict(s) for s in body.get("schemas", []))      # re-validated locally
    if len({s.id for s in schemas}) != len(schemas):
        raise SchemaError("Statement-format file lists a format twice")
    from core.integration.schema import parse_all

    raw_integrations = body.get("integrations") or []
    if not isinstance(raw_integrations, list):
        raise SchemaError("Statement-format file is damaged")
    integrations, skipped = parse_all(raw_integrations)          # one bad entry never blocks formats
    if len({i.id for i in integrations}) != len(integrations):
        raise SchemaError("Statement-format file lists an integration twice")
    return Bundle(str(body.get("issued_at", "")), schemas, tuple(integrations), tuple(skipped))


# --- installation in the app (encrypted database) -------------------------------------------------


@dataclass(frozen=True)
class InstallReport:
    added: tuple[str, ...]
    updated: tuple[str, ...]
    unchanged: tuple[str, ...]
    refused_downgrades: tuple[str, ...]
    integrations_added: tuple[str, ...] = ()
    integrations_updated: tuple[str, ...] = ()
    integrations_skipped: tuple[str, ...] = ()


def _upsert(conn: Any, table: str, item: Any, issued_at: str, now: str) -> str:
    """'added' | 'updated' | 'unchanged' | 'refused' (never roll a version back: replayed old bundle)."""
    row = conn.execute(f"SELECT version FROM {table} WHERE id = ?", (item.id,)).fetchone()
    payload = json.dumps(item.to_dict(), sort_keys=True)
    if row is None:
        conn.execute(f"INSERT INTO {table} (id, version, name, platform, schema_json, bundle_issued_at, installed_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (item.id, item.version, item.name, item.platform, payload, issued_at, now))
        return "added"
    if item.version > row[0]:
        conn.execute(f"UPDATE {table} SET version = ?, name = ?, platform = ?, schema_json = ?, bundle_issued_at = ?, "
                     "installed_at = ? WHERE id = ?",
                     (item.version, item.name, item.platform, payload, issued_at, now, item.id))
        return "updated"
    return "unchanged" if item.version == row[0] else "refused"


def install_bundle(db: Any, text: str, public_keys_hex: Iterable[str]) -> InstallReport:
    bundle = verify_bundle(text, public_keys_hex)
    added, updated, unchanged, refused = [], [], [], []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    out: dict[str, list[str]] = {"added": added, "updated": updated, "unchanged": unchanged, "refused": refused}
    i_added: list[str] = []
    i_updated: list[str] = []
    with db.transaction() as conn:
        for schema in bundle.schemas:
            out[_upsert(conn, "vendor_parsers", schema, bundle.issued_at, now)].append(schema.id)
        for integration in bundle.integrations:
            status = _upsert(conn, "vendor_integrations", integration, bundle.issued_at, now)
            if status == "added":
                i_added.append(integration.id)
            elif status == "updated":
                i_updated.append(integration.id)
            elif status == "refused":
                refused.append(integration.id)
    return InstallReport(tuple(added), tuple(updated), tuple(unchanged), tuple(refused), tuple(i_added),
                         tuple(i_updated), bundle.skipped_integrations)


def installed_integrations(db: Any) -> list[Any]:
    from core.integration.schema import IntegrationSchema, IntegrationSchemaError

    with db.connection() as conn:
        rows = conn.execute("SELECT schema_json FROM vendor_integrations WHERE enabled = 1 ORDER BY name").fetchall()
    out = []
    for r in rows:
        try:
            out.append(IntegrationSchema.from_dict(json.loads(r[0])))
        except (IntegrationSchemaError, ValueError):
            continue
    return out


def installed_schemas(db: Any) -> list[ParserSchema]:
    with db.connection() as conn:
        rows = conn.execute("SELECT schema_json FROM vendor_parsers WHERE enabled = 1 ORDER BY name").fetchall()
    out = []
    for r in rows:
        try:
            out.append(ParserSchema.from_dict(json.loads(r[0])))
        except (SchemaError, ValueError):
            continue                                  # a stored schema this build can't read is skipped
    return out


def detect(schemas: Iterable[ParserSchema], headers: Iterable[str]) -> ParserSchema | None:
    """Most specific installed schema whose header signature is present."""
    headers = list(headers)
    fits = [s for s in schemas if s.matches(headers)]
    return max(fits, key=lambda s: (len(s.profile.detect_headers), s.version), default=None)
