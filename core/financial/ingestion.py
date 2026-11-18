"""
Multi-platform income ingestion (CSV + PDF statements).

The design is *schema-driven*: nothing about Uber, Lyft, or TaskRabbit is
hard-coded into the parser. Each source format is described by a
`MappingProfile` — plain data that says which column (or PDF regex group)
feeds which field of our `transactions` table. Profiles serialize to JSON,
are stored in the `import_mappings` table, and can be created or edited by
users from the UI. When a platform renames a column, the fix is a mapping
edit, not a code release.

Pipeline
--------

    file ──► rows (dict[str, str]) ──► MappingProfile.map_row() ──► ParsedRecord[]
     CSV: header row detection, delimiter sniffing, BOM/encoding handling
     PDF: text extraction (pypdf) + per-line regex with named groups

    ParsedRecord[] ──► import_records(db, ...) ──► transactions (deduplicated)

One statement row can become several transactions: a ride with a fare, a
tip, and a quest bonus is stored as three rows (`earning`, `tip`,
`incentive`) so reports and taxes can tell them apart.

Deduplication
-------------
Every record carries an `external_ref`. If the statement has an ID column we
use it; otherwise we derive a stable hash from the row's content (plus an
occurrence counter for genuinely identical rows). Re-importing the same file
is therefore a no-op — enforced by the unique index on
`(platform_id, external_ref)`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.config import ConfigError, normalize_currency
from core.financial.money import MoneyError, apply_bp, convert, parse_amount

log = logging.getLogger(__name__)

PROFILE_SCHEMA_VERSION = 2          # v2: extractors, multipliers_bp, paid_out, detect_headers (v1 files still load)
MAX_PATTERN = 300
MAX_CELL = 1000                      # extractor regexes only ever see this many characters of a cell
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*[+*{]")


def safe_pattern(pattern: str, *, what: str = "pattern") -> re.Pattern[str]:
    """Compile a regex that comes from a profile (possibly downloaded). Refuses constructs that can
    blow up matching time (nested quantifiers like (a+)+, backreferences) and very long patterns.
    Inputs are also truncated to MAX_CELL characters before matching."""
    if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN:
        raise IngestionError(f"{what} must be 1–{MAX_PATTERN} characters")
    if _NESTED_QUANTIFIER.search(pattern):
        raise IngestionError(f"{what} has a nested quantifier like (a+)+; rewrite it without one")
    if re.search(r"\\[1-9]|\(\?P=", pattern):
        raise IngestionError(f"{what} can't use backreferences")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise IngestionError(f"{what} isn't a valid regular expression: {exc}") from exc

# Must mirror the CHECK constraint on transactions.kind in schema.sql.
TRANSACTION_KINDS = ("earning", "tip", "bonus", "incentive", "reimbursement", "adjustment", "refund")
SKIP = "skip"

DEFAULT_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%b %d, %Y %I:%M %p",
    "%b %d, %Y",
    "%B %d, %Y",
)

Source = Literal["csv", "pdf"]


class IngestionError(ValueError):
    """Unrecoverable problem with a file or a mapping profile."""


# ---------------------------------------------------------------------------
# Mapping model
# ---------------------------------------------------------------------------


def _norm_header(name: str) -> str:
    """Normalize a column name for matching: case, whitespace, BOM, quotes."""
    return re.sub(r"\s+", " ", name.replace("﻿", "").strip().strip('"').strip()).casefold()


@dataclass(frozen=True)
class FieldSource:
    """Where a field's value comes from.

    `columns` are header names (CSV) or named regex groups (PDF).
      combine="first": take the first listed column that exists and is
                       non-empty — i.e. the names are *aliases*.
      combine="sum":   add up every listed column that exists — for
                       platforms that split an amount across columns.
    """

    columns: tuple[str, ...]
    combine: Literal["first", "sum"] = "first"

    def __post_init__(self) -> None:
        if not self.columns:
            raise IngestionError("FieldSource needs at least one column")
        if self.combine not in ("first", "sum"):
            raise IngestionError(f"combine must be 'first' or 'sum', got {self.combine!r}")
        object.__setattr__(self, "columns", tuple(self.columns))

    @classmethod
    def coerce(cls, value: Any) -> "FieldSource":
        """Accept 'Col', ['A', 'B'], {'columns': [...], 'combine': 'sum'}."""
        if isinstance(value, FieldSource):
            return value
        if isinstance(value, str):
            return cls((value,))
        if isinstance(value, (list, tuple)):
            return cls(tuple(value))
        if isinstance(value, Mapping):
            return cls(tuple(value["columns"]), value.get("combine", "first"))
        raise IngestionError(f"Cannot interpret field source {value!r}")

    def to_json(self) -> Any:
        if self.combine == "first" and len(self.columns) == 1:
            return self.columns[0]
        if self.combine == "first":
            return list(self.columns)
        return {"columns": list(self.columns), "combine": self.combine}

    def present_in(self, headers: set[str]) -> bool:
        return any(_norm_header(c) in headers for c in self.columns)

    def values(self, row: Mapping[str, str]) -> list[str]:
        """Raw non-empty cell values this source selects from a normalized row."""
        found = [row[_norm_header(c)] for c in self.columns if row.get(_norm_header(c), "").strip()]
        return found[:1] if self.combine == "first" else found


@dataclass(frozen=True)
class MappingProfile:
    """Declarative description of one statement format."""

    name: str
    platform: str
    date: FieldSource
    amounts: Mapping[str, FieldSource]              # transaction kind -> source
    source_format: Source = "csv"
    fee: FieldSource | None = None                  # platform fee / commission
    external_ref: FieldSource | None = None
    description: FieldSource | None = None
    currency: FieldSource | None = None
    default_currency: str = "USD"
    # Row-typed statements: a "Type" column decides the kind of the row.
    kind_column: FieldSource | None = None
    kind_values: Mapping[str, str] = field(default_factory=dict)   # raw value -> kind | "skip"
    date_formats: tuple[str, ...] = DEFAULT_DATE_FORMATS
    timezone: str | None = None                     # IANA name; None = caller must supply
    decimal_separator: str = "."
    round_sub_minor: bool = False                   # accept "12.345" and round half-up
    delimiter: str | None = None                    # None = sniff
    header_search_rows: int = 25
    line_pattern: str | None = None                 # PDF: regex with named groups
    # v2 (dynamic / vendor schemas):
    extractors: Mapping[str, str] = field(default_factory=dict)      # column -> regex; group "value" or 1
    multipliers_bp: Mapping[str, int] = field(default_factory=dict)  # kind or "fee" -> basis points (10000 = ×1)
    paid_out: FieldSource | None = None                              # payout / deposit date column
    detect_headers: tuple[str, ...] = ()                             # all present ⇒ this profile fits the file

    def __post_init__(self) -> None:
        object.__setattr__(self, "date", FieldSource.coerce(self.date))
        object.__setattr__(self, "amounts", {k: FieldSource.coerce(v) for k, v in dict(self.amounts).items()})
        for attr in ("fee", "external_ref", "description", "currency", "kind_column", "paid_out"):
            value = getattr(self, attr)
            if value is not None:
                object.__setattr__(self, attr, FieldSource.coerce(value))
        object.__setattr__(self, "kind_values", {str(k).strip().casefold(): v for k, v in dict(self.kind_values).items()})
        object.__setattr__(self, "date_formats", tuple(self.date_formats))
        object.__setattr__(self, "extractors", {_norm_header(k): v for k, v in dict(self.extractors).items()})
        object.__setattr__(self, "multipliers_bp", dict(self.multipliers_bp))
        object.__setattr__(self, "detect_headers", tuple(self.detect_headers))
        self.validate()

    @property
    def uses_v2(self) -> bool:
        return bool(self.extractors or self.multipliers_bp or self.paid_out or self.detect_headers)

    # -- validation ---------------------------------------------------------

    def validate(self) -> None:
        if not self.name.strip() or not self.platform.strip():
            raise IngestionError("Profile needs a name and a platform")
        if not self.amounts:
            raise IngestionError(f"Profile '{self.name}' maps no amount columns")
        bad = [k for k in self.amounts if k not in TRANSACTION_KINDS]
        if bad:
            raise IngestionError(f"Unknown transaction kind(s) {bad}; allowed: {TRANSACTION_KINDS}")
        bad_values = [v for v in self.kind_values.values() if v not in (*TRANSACTION_KINDS, SKIP)]
        if bad_values:
            raise IngestionError(f"kind_values contains invalid kinds {bad_values}")
        if self.kind_values and self.kind_column is None:
            raise IngestionError("kind_values given without kind_column")
        if self.kind_column is not None and len(self.amounts) != 1:
            raise IngestionError("Row-typed profiles (kind_column) must map exactly one amount source")
        try:
            normalize_currency(self.default_currency)
        except ConfigError as exc:
            raise IngestionError(str(exc)) from exc
        if self.timezone is not None:
            _zone(self.timezone)
        if self.decimal_separator not in (".", ","):
            raise IngestionError("decimal_separator must be '.' or ','")
        for column, pattern in self.extractors.items():
            compiled = safe_pattern(pattern, what=f"Extractor for '{column}'")
            if compiled.groups == 0:
                raise IngestionError(f"Extractor for '{column}' needs a capture group (use (?P<value>...))")
        for key, bp in self.multipliers_bp.items():
            if key not in (*TRANSACTION_KINDS, "fee"):
                raise IngestionError(f"multipliers_bp key {key!r} must be an income kind or 'fee'")
            if isinstance(bp, bool) or not isinstance(bp, int) or not -100_000 <= bp <= 100_000:
                raise IngestionError("multipliers_bp values are whole basis points between -100000 and 100000")
        if self.source_format not in ("csv", "pdf"):
            raise IngestionError("source_format must be 'csv' or 'pdf'")
        if self.source_format == "pdf":
            if not self.line_pattern:
                raise IngestionError("PDF profiles need a line_pattern regex")
            try:
                groups = set(re.compile(self.line_pattern).groupindex)
            except re.error as exc:
                raise IngestionError(f"Invalid line_pattern: {exc}") from exc
            if self.uses_v2:                                 # downloaded/dynamic profiles get the safety rules
                safe_pattern(self.line_pattern, what="line_pattern")
            referenced = {_norm_header(c) for c in self.date.columns}
            if not referenced & {_norm_header(g) for g in groups}:
                raise IngestionError("line_pattern must define the date group referenced by the profile")

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        def opt(fs: FieldSource | None) -> Any:
            return fs.to_json() if fs else None

        extra = {"extractors": dict(self.extractors), "multipliers_bp": dict(self.multipliers_bp),
                 "paid_out": opt(self.paid_out), "detect_headers": list(self.detect_headers)} if self.uses_v2 else {}
        return {
            # v1 when no v2 feature is used, so older app versions keep reading these profiles; a v2
            # profile is refused by an older app instead of being misread without its multipliers.
            "schema_version": 2 if self.uses_v2 else 1,
            **extra,
            "name": self.name,
            "platform": self.platform,
            "source_format": self.source_format,
            "date": self.date.to_json(),
            "amounts": {k: v.to_json() for k, v in self.amounts.items()},
            "fee": opt(self.fee),
            "external_ref": opt(self.external_ref),
            "description": opt(self.description),
            "currency": opt(self.currency),
            "default_currency": self.default_currency,
            "kind_column": opt(self.kind_column),
            "kind_values": dict(self.kind_values),
            "date_formats": list(self.date_formats),
            "timezone": self.timezone,
            "decimal_separator": self.decimal_separator,
            "round_sub_minor": self.round_sub_minor,
            "delimiter": self.delimiter,
            "header_search_rows": self.header_search_rows,
            "line_pattern": self.line_pattern,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MappingProfile":
        data = dict(data)
        version = data.pop("schema_version", PROFILE_SCHEMA_VERSION)
        if version > PROFILE_SCHEMA_VERSION:
            raise IngestionError(f"Mapping profile schema v{version} is newer than supported")
        known = set(cls.__dataclass_fields__)
        unknown = set(data) - known
        if unknown:
            log.warning("Ignoring unknown mapping keys: %s", sorted(unknown))
        kwargs = {k: v for k, v in data.items() if k in known and v is not None}
        if "date_formats" in kwargs:
            kwargs["date_formats"] = tuple(kwargs["date_formats"])
        if "detect_headers" in kwargs:
            kwargs["detect_headers"] = tuple(kwargs["detect_headers"])
        try:
            return cls(**kwargs)
        except TypeError as exc:
            raise IngestionError(f"Invalid mapping profile: {exc}") from exc

    @classmethod
    def from_json(cls, text: str) -> "MappingProfile":
        return cls.from_dict(json.loads(text))

    def with_updates(self, **changes: Any) -> "MappingProfile":
        return replace(self, **changes)


# Metadata the UI uses to render a column-mapping form.
MAPPABLE_FIELDS: dict[str, dict[str, Any]] = {
    "date": {"required": True, "help": "When the job/payment happened"},
    "amounts": {"required": True, "help": "One source per income kind", "kinds": TRANSACTION_KINDS},
    "fee": {"required": False, "help": "Platform commission/service fee (sign ignored)"},
    "external_ref": {"required": False, "help": "Trip/order/invoice ID — enables exact de-duplication"},
    "description": {"required": False, "help": "Free text shown in the ledger"},
    "currency": {"required": False, "help": "Per-row ISO 4217 code; else default_currency"},
    "kind_column": {"required": False, "help": "Column holding the row type (e.g. Tip, Payout)"},
    "paid_out": {"required": False, "help": "Payout / deposit date"},
}


# ---------------------------------------------------------------------------
# Parsed output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedRecord:
    occurred_at: str            # ISO-8601 UTC, ms precision, 'Z'
    kind: str
    gross_amount_minor: int
    platform_fee_minor: int
    currency: str
    external_ref: str
    description: str | None
    source_line: int
    paid_out_at: str | None = None


@dataclass(frozen=True)
class RowError:
    line: int
    message: str
    raw: Mapping[str, str] | str


@dataclass
class ParseResult:
    profile: str
    records: list[ParsedRecord] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rows_seen: int = 0
    rows_skipped: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def totals_by_kind(self) -> dict[tuple[str, str], int]:
        """(currency, kind) -> summed gross minor units. Handy for previews/tests."""
        out: dict[tuple[str, str], int] = {}
        for r in self.records:
            out[(r.currency, r.kind)] = out.get((r.currency, r.kind), 0) + r.gross_amount_minor
        return out


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise IngestionError(f"Unknown timezone '{name}'") from exc


def _to_utc_iso(dt: datetime) -> str:
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def parse_datetime(text: str, formats: Sequence[str], zone: ZoneInfo) -> str:
    """Parse a statement timestamp; naive values are interpreted in `zone`."""
    value = text.strip()
    parsed: datetime | None = None
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise IngestionError(f"Unrecognized date {text!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return _to_utc_iso(parsed)


class _RowMapper:
    """Applies a profile to normalized rows. Tracks hash occurrences per file."""

    def __init__(self, profile: MappingProfile, tz: ZoneInfo) -> None:
        self.p = profile
        self.tz = tz
        self._seen_hashes: dict[str, int] = {}
        self._rounding = ROUND_HALF_UP if profile.round_sub_minor else None
        self._extract = {col: safe_pattern(pat) for col, pat in profile.extractors.items()}

    def _values(self, source: FieldSource, row: Mapping[str, str]) -> list[str]:
        """Cell values for a source, with the profile's regex extractors applied per column."""
        if not self._extract:
            return source.values(row)
        out = []
        for column in source.columns:
            key = _norm_header(column)
            raw = row.get(key, "")
            if key in self._extract and raw.strip():
                m = self._extract[key].search(raw[:MAX_CELL])
                raw = "" if m is None else (m.group("value") if "value" in m.re.groupindex else m.group(1)) or ""
            if raw.strip():
                out.append(raw)
                if source.combine == "first":
                    break
        return out

    def _amount(self, source: FieldSource | None, row: Mapping[str, str], currency: str) -> int:
        if source is None:
            return 0
        total = 0
        for raw in self._values(source, row):
            total += parse_amount(raw, currency, decimal_separator=self.p.decimal_separator, rounding=self._rounding)
        return total

    def _scaled(self, key: str, amount: int) -> int:
        bp = self.p.multipliers_bp.get(key)
        return amount if bp is None else apply_bp(amount, bp)

    def _text(self, source: FieldSource | None, row: Mapping[str, str]) -> str | None:
        if source is None:
            return None
        vals = self._values(source, row)
        return " ".join(v.strip() for v in vals) or None if vals else None

    def _fallback_ref(self, row: Mapping[str, str]) -> str:
        content = json.dumps(
            [self.p.platform.casefold(), sorted((k, v.strip()) for k, v in row.items())], ensure_ascii=False
        )
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:24]
        n = self._seen_hashes.get(digest, 0) + 1
        self._seen_hashes[digest] = n
        return f"h:{digest}" if n == 1 else f"h:{digest}~{n}"

    def map_row(self, row: Mapping[str, str], line: int) -> list[ParsedRecord] | None:
        """Return records for the row, or None if the row is intentionally skipped."""
        p = self.p

        row_kind: str | None = None
        if p.kind_column is not None:
            raw_kind = (self._text(p.kind_column, row) or "").strip()
            mapped = p.kind_values.get(raw_kind.casefold())
            if mapped is None:
                raise IngestionError(
                    f"Unmapped row type {raw_kind!r}; add it to kind_values (use 'skip' to ignore)"
                )
            if mapped == SKIP:
                return None
            row_kind = mapped

        date_text = self._text(p.date, row)
        if not date_text:
            raise IngestionError("Missing date")
        occurred_at = parse_datetime(date_text, p.date_formats, self.tz)

        currency_text = self._text(p.currency, row)
        try:
            currency = normalize_currency(currency_text or p.default_currency)
        except ConfigError as exc:
            raise IngestionError(str(exc)) from exc

        components: list[tuple[str, int]] = []
        for kind, source in p.amounts.items():
            amount = self._scaled(kind, self._amount(source, row, currency))
            if amount:
                components.append((row_kind or kind, amount))

        fee = abs(self._scaled("fee", self._amount(p.fee, row, currency)))
        paid_text = self._text(p.paid_out, row)
        paid_out_at = parse_datetime(paid_text, p.date_formats, self.tz) if paid_text else None
        if not components and not fee:
            return []            # empty row (e.g. $0.00 line) — nothing to store
        if not components:
            components.append(("adjustment", 0))

        # The fee belongs to the primary earning component when there is one.
        fee_index = next((i for i, (k, _) in enumerate(components) if k == "earning"), 0)

        base_ref = self._text(p.external_ref, row) or self._fallback_ref(row)
        # Suffix refs with the kind when a row can split into several records,
        # so each component de-duplicates independently.
        multi = len(p.amounts) > 1
        description = self._text(p.description, row)

        records = []
        for i, (kind, amount) in enumerate(components):
            ref = f"{base_ref}#{kind}" if multi else base_ref
            records.append(
                ParsedRecord(
                    occurred_at=occurred_at,
                    kind=kind,
                    gross_amount_minor=amount,
                    platform_fee_minor=fee if i == fee_index else 0,
                    currency=currency,
                    external_ref=ref,
                    description=description,
                    source_line=line,
                    paid_out_at=paid_out_at,
                )
            )
        return records


def _resolve_tz(profile: MappingProfile, tz_override: str | None) -> ZoneInfo:
    name = tz_override or profile.timezone
    if not name:
        raise IngestionError(
            f"Profile '{profile.name}' has no timezone; pass timezone= (statement times are local)"
        )
    return _zone(name)


def map_rows(
    rows: Iterable[tuple[int, Mapping[str, str]]],
    profile: MappingProfile,
    *,
    timezone_name: str | None = None,
    result: ParseResult | None = None,
) -> ParseResult:
    """Apply a profile to (line_number, row) pairs. Rows must be header-normalized."""
    result = result or ParseResult(profile=profile.name)
    mapper = _RowMapper(profile, _resolve_tz(profile, timezone_name))
    for line, row in rows:
        result.rows_seen += 1
        try:
            mapped = mapper.map_row(row, line)
        except (IngestionError, MoneyError) as exc:
            result.errors.append(RowError(line, str(exc), dict(row)))
            continue
        if mapped is None:
            result.rows_skipped += 1
        else:
            result.records.extend(mapped)
    return result


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

CsvInput = str | bytes | Path | io.TextIOBase


def _read_text(source: CsvInput) -> str:
    if isinstance(source, Path):
        source = source.read_bytes()
    if isinstance(source, bytes):
        for encoding in ("utf-8-sig", "cp1252"):
            try:
                return source.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise IngestionError("File is not UTF-8 or Windows-1252 text")  # pragma: no cover
    if isinstance(source, str):
        return source.lstrip("﻿")
    return source.read().lstrip("﻿")


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def parse_csv(source: CsvInput, profile: MappingProfile, *, timezone: str | None = None) -> ParseResult:
    """Parse a CSV statement. `source` may be a Path, bytes, str content, or text stream.

    A `str` is treated as file *content*; pass a `Path` for a file on disk.
    """
    if profile.source_format != "csv":
        raise IngestionError(f"Profile '{profile.name}' is not a CSV profile")
    text = _read_text(source)
    delimiter = profile.delimiter or _sniff_delimiter(text[:8192])
    all_rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))

    # Find the header: first row (within the search window) containing the
    # date column and at least one amount column. Skips statement preambles.
    header_index = None
    for i, row in enumerate(all_rows[: profile.header_search_rows]):
        headers = {_norm_header(c) for c in row}
        if profile.date.present_in(headers) and any(s.present_in(headers) for s in profile.amounts.values()):
            header_index = i
            break
    if header_index is None:
        raise IngestionError(
            f"No header row matching profile '{profile.name}' in the first "
            f"{profile.header_search_rows} rows (need date + an amount column)"
        )

    header = [_norm_header(c) for c in all_rows[header_index]]
    header_set = set(header)
    result = ParseResult(profile=profile.name)
    for label, source in _optional_sources(profile):
        if not source.present_in(header_set):
            result.warnings.append(f"Column(s) for '{label}' not found {list(source.columns)}; treated as empty")

    def rows() -> Iterable[tuple[int, dict[str, str]]]:
        for offset, raw in enumerate(all_rows[header_index + 1 :], start=header_index + 2):
            if not any(cell.strip() for cell in raw):
                continue
            yield offset, {header[j]: raw[j] if j < len(raw) else "" for j in range(len(header))}

    return map_rows(rows(), profile, timezone_name=timezone, result=result)


def _optional_sources(profile: MappingProfile) -> list[tuple[str, FieldSource]]:
    out: list[tuple[str, FieldSource]] = [(f"amount:{k}", v) for k, v in profile.amounts.items()]
    for label in ("fee", "external_ref", "description", "currency", "kind_column"):
        source = getattr(profile, label)
        if source is not None:
            out.append((label, source))
    return out


def read_csv_headers(source: CsvInput, max_rows: int = 25) -> list[list[str]]:
    """First rows of a CSV, for the mapping UI to show the user."""
    text = _read_text(source)
    reader = csv.reader(io.StringIO(text), delimiter=_sniff_delimiter(text[:8192]))
    return [row for _, row in zip(range(max_rows), reader)]


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def extract_pdf_text(path: Path | str) -> str:
    """Extract text from a text-based PDF. Scanned/image PDFs need OCR (later phase)."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - pypdf is in requirements
        raise IngestionError("PDF support requires the 'pypdf' package") from exc
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        raise IngestionError("PDF is password-protected; decrypt it before importing")
    pages = [(page.extract_text() or "") for page in reader.pages]
    text = "\n".join(pages)
    if not text.strip():
        raise IngestionError("PDF has no extractable text (scanned image?)")
    return text


def parse_pdf(
    source: Path | str,
    profile: MappingProfile,
    *,
    timezone: str | None = None,
    text_extractor: Callable[[Path | str], str] = extract_pdf_text,
) -> ParseResult:
    """Parse a PDF statement line-by-line using the profile's `line_pattern`.

    Lines that don't match (headers, page footers, totals) are ignored, not
    errors. Matched lines go through the same mapping logic as CSV rows, with
    the regex's named groups acting as column names.
    """
    if profile.source_format != "pdf":
        raise IngestionError(f"Profile '{profile.name}' is not a PDF profile")
    assert profile.line_pattern is not None
    pattern = re.compile(profile.line_pattern)
    text = text_extractor(source)

    def rows() -> Iterable[tuple[int, dict[str, str]]]:
        for number, line in enumerate(text.splitlines(), start=1):
            match = pattern.search(line.strip())
            if match:
                yield number, {_norm_header(k): (v or "") for k, v in match.groupdict().items()}

    return map_rows(rows(), profile, timezone_name=timezone)


def parse_statement(source: Path, profile: MappingProfile, *, timezone: str | None = None) -> ParseResult:
    """Dispatch on the profile's source format."""
    if profile.source_format == "pdf":
        return parse_pdf(source, profile, timezone=timezone)
    return parse_csv(Path(source), profile, timezone=timezone)


# ---------------------------------------------------------------------------
# Mapping suggestions (for unknown CSVs)
# ---------------------------------------------------------------------------

_SUGGEST_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("external_ref", ("uuid", "trip id", "ride id", "order id", "invoice", "transaction id", "reference", "id")),
    ("date", ("date/time", "datetime", "timestamp", "date", "time")),
    ("fee", ("service fee", "commission", "platform fee", "fee")),
    ("amount:tip", ("tip", "gratuity")),
    ("amount:incentive", ("quest", "promotion", "incentive", "surge bonus")),
    ("amount:bonus", ("bonus",)),
    ("amount:reimbursement", ("toll", "reimburse")),
    ("amount:earning", ("fare", "earnings", "payout amount", "amount", "total", "pay")),
    ("description", ("description", "service", "product", "type", "memo")),
    ("currency", ("currency",)),
]


def suggest_profile(headers: Sequence[str], *, name: str, platform: str, timezone: str | None = None) -> MappingProfile:
    """Draft a profile from header names with keyword heuristics.

    The draft is a starting point for the mapping UI; the user confirms or
    corrects each assignment before import.
    """
    assigned: dict[str, list[str]] = {}
    for header in headers:
        norm = _norm_header(header)
        if not norm:
            continue
        for target, keywords in _SUGGEST_RULES:
            if any(re.search(rf"(^|\b){re.escape(k)}(\b|$)", norm) for k in keywords):
                assigned.setdefault(target, []).append(header)
                break

    if "date" not in assigned:
        raise IngestionError("Could not find a date column to suggest a mapping")
    amounts = {t.split(":", 1)[1]: FieldSource(tuple(cols[:1])) for t, cols in assigned.items() if t.startswith("amount:")}
    if not amounts:
        raise IngestionError("Could not find an amount column to suggest a mapping")

    def first(target: str) -> FieldSource | None:
        cols = assigned.get(target)
        return FieldSource(tuple(cols[:1])) if cols else None

    return MappingProfile(
        name=name,
        platform=platform,
        date=FieldSource(tuple(assigned["date"][:1])),
        amounts=amounts,
        fee=first("fee"),
        external_ref=first("external_ref"),
        description=first("description"),
        currency=first("currency"),
        timezone=timezone,
    )


# ---------------------------------------------------------------------------
# Built-in starter profiles
# ---------------------------------------------------------------------------
# Platforms change export layouts often and vary by region. These profiles use
# alias lists to tolerate common variants, but they are *templates*: verify
# against a real export and adjust (in the UI) rather than editing code.

BUILTIN_PROFILES: dict[str, MappingProfile] = {
    p.name: p
    for p in [
        MappingProfile(
            name="uber-driver-csv",
            platform="Uber",
            date=("Date/Time", "Trip Date", "Date", "Request Time"),
            amounts={
                "earning": {"columns": ["Fare", "Base Fare", "Surge", "Wait Time", "Time", "Distance"], "combine": "sum"},
                "tip": ("Tip", "Tips"),
                "incentive": ("Quest", "Promotion", "Incentive", "Boost"),
                "reimbursement": ("Tolls", "Toll", "Airport Fee"),
            },
            fee=("Service Fee", "Uber Fee", "Booking Fee Deduction"),
            external_ref=("Trip UUID", "Trip ID", "Order ID"),
            description=("Service Type", "Product", "Trip Type"),
        ),
        MappingProfile(
            name="lyft-driver-csv",
            platform="Lyft",
            date=("Date", "Ride Date", "Timestamp", "Date/Time"),
            amounts={
                "earning": ("Ride Earnings", "Earnings", "Ride earnings", "Fare"),
                "tip": ("Tips", "Tip"),
                "bonus": ("Bonuses", "Bonus", "Streak Bonus"),
                "reimbursement": ("Tolls", "Toll Reimbursement"),
            },
            fee=("Lyft Fee", "Service Fee", "Platform Fee"),
            external_ref=("Ride ID", "Transaction ID"),
            description=("Ride Type", "Description"),
        ),
        MappingProfile(
            name="taskrabbit-csv",
            platform="TaskRabbit",
            date=("Date", "Invoice Date", "Payment Date"),
            amounts={"earning": ("Amount", "Total", "Net Amount")},
            kind_column=("Type", "Transaction Type"),
            kind_values={
                "task payment": "earning",
                "invoice": "earning",
                "payment": "earning",
                "tip": "tip",
                "reimbursement": "reimbursement",
                "expense reimbursement": "reimbursement",
                "adjustment": "adjustment",
                "refund": "refund",
                "payout": SKIP,
                "transfer": SKIP,
                "withdrawal": SKIP,
            },
            fee=("Service Fee", "TaskRabbit Fee"),
            external_ref=("Invoice ID", "Invoice #", "Transaction ID"),
            description=("Description", "Task", "Category"),
        ),
        MappingProfile(
            name="generic-pdf-statement",
            platform="Generic",
            source_format="pdf",
            # e.g. "09/01/2026  INV-1042  Furniture assembly   $85.00   $12.00"
            line_pattern=(
                r"^(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+(?P<ref>[A-Z0-9][A-Z0-9-]{2,})\s+"
                r"(?P<description>.+?)\s+(?P<amount>-?\(?\$?[\d,]+\.\d{2}\)?)"
                r"(?:\s+(?P<tip>\$?[\d,]+\.\d{2}))?$"
            ),
            date="date",
            amounts={"earning": "amount", "tip": "tip"},
            external_ref="ref",
            description="description",
            date_formats=("%m/%d/%Y",),
        ),
    ]
}


# ---------------------------------------------------------------------------
# Database integration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportResult:
    inserted: int
    duplicates: int
    inserted_ids: tuple[str, ...]
    unconverted: int          # rows with foreign currency and no FX rate (base_amount_minor NULL)


def import_records(
    db: Any,
    platform_id: str,
    records: Iterable[ParsedRecord],
    *,
    base_currency: str,
    fx_rates: Mapping[str, Decimal | str] | None = None,
    device_id: str | None = None,
) -> ImportResult:
    """Insert parsed records into `transactions` atomically, skipping duplicates.

    `fx_rates` maps a foreign currency to units of `base_currency` per one
    unit of it, and pins `base_amount_minor` at import time.
    """
    import uuid

    base_currency = normalize_currency(base_currency)
    fx_rates = {normalize_currency(k): Decimal(str(v)) for k, v in (fx_rates or {}).items()}
    inserted: list[str] = []
    duplicates = unconverted = 0

    sql = """
        INSERT INTO transactions (
            id, platform_id, kind, gross_amount_minor, platform_fee_minor, currency,
            base_amount_minor, occurred_at, paid_out_at, external_ref, description, last_modified_device
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (platform_id, external_ref)
            WHERE external_ref IS NOT NULL AND deleted_at IS NULL
        DO NOTHING
    """
    with db.transaction() as conn:
        for rec in records:
            if rec.currency == base_currency:
                base_amount: int | None = rec.gross_amount_minor
            elif rec.currency in fx_rates:
                base_amount = convert(rec.gross_amount_minor, rec.currency, base_currency, fx_rates[rec.currency])
            else:
                base_amount = None
                unconverted += 1
            new_id = str(uuid.uuid4())
            cur = conn.execute(
                sql,
                (
                    new_id, platform_id, rec.kind, rec.gross_amount_minor, rec.platform_fee_minor,
                    rec.currency, base_amount, rec.occurred_at, rec.paid_out_at, rec.external_ref, rec.description,
                    device_id,
                ),
            )
            if cur.rowcount == 1:
                inserted.append(new_id)
            else:
                duplicates += 1
                if base_amount is None:
                    unconverted -= 1
    return ImportResult(len(inserted), duplicates, tuple(inserted), unconverted)


def save_profile(db: Any, profile: MappingProfile, *, platform_id: str | None = None) -> str:
    """Create or update a user mapping profile (matched by name). Returns its id."""
    import uuid

    payload = profile.to_json()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT id FROM import_mappings WHERE name = ? COLLATE NOCASE AND deleted_at IS NULL",
            (profile.name,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE import_mappings SET mapping_json = ?, source_format = ?, platform_id = ?, "
                "mapping_version = mapping_version + 1 WHERE id = ?",
                (payload, profile.source_format, platform_id, row["id"]),
            )
            return row["id"]
        new_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO import_mappings (id, name, platform_id, source_format, mapping_json) VALUES (?, ?, ?, ?, ?)",
            (new_id, profile.name, platform_id, profile.source_format, payload),
        )
        return new_id


def _vendor_profiles(db: Any) -> dict[str, MappingProfile]:
    """Installed vendor statement formats (Parser Studio), by display name."""
    try:
        from core.financial.parsers.dynamic import installed_schemas

        return {s.display_name: s.profile for s in installed_schemas(db)}
    except Exception as exc:                          # a damaged install must never block imports
        log.warning("Couldn't load installed statement formats: %s", type(exc).__name__)
        return {}


def load_profile(db: Any, name: str) -> MappingProfile:
    """User profiles take precedence over vendor formats, which take precedence over built-ins."""
    with db.connection() as conn:
        row = conn.execute(
            "SELECT mapping_json FROM import_mappings WHERE name = ? COLLATE NOCASE AND deleted_at IS NULL",
            (name,),
        ).fetchone()
    if row:
        return MappingProfile.from_json(row["mapping_json"])
    vendor = _vendor_profiles(db)
    if name in vendor:
        return vendor[name]
    if name in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[name]
    raise IngestionError(f"No mapping profile named '{name}'")


@dataclass(frozen=True)
class Detection:
    name: str                  # profile name, as listed by list_profiles()
    profile: MappingProfile
    origin: str                # "vendor" (a published iFlow format) | "user" | "builtin"
    score: int


def fit_score(profile: MappingProfile, headers: Sequence[str]) -> int | None:
    """How well a CSV header row fits a profile; None if it doesn't fit at all.

    Profiles with detect_headers (published formats) fit only when every one is present, and then beat
    any column-count guess. Others fit when the date column and at least one amount column are present.
    """
    if profile.source_format != "csv":
        return None
    present = {_norm_header(h) for h in headers if h and h.strip()}
    if profile.detect_headers:
        if all(_norm_header(h) in present for h in profile.detect_headers):
            return 100 + len(profile.detect_headers)
        return None
    if not profile.date.present_in(present) or not any(a.present_in(present) for a in profile.amounts.values()):
        return None
    sources = [profile.date, *(s for _, s in _optional_sources(profile))]     # amounts are in there too
    return sum(1 for s in sources if s.present_in(present))


def detect_profile(db: Any, source: CsvInput) -> Detection | None:
    """The best-fitting profile for a CSV statement, looking at its first rows for the header line.
    Ties go to published iFlow formats, then the user's own profiles, then the built-ins."""
    try:
        rows = read_csv_headers(source, max_rows=25)
    except (OSError, UnicodeDecodeError, csv.Error, IngestionError):
        return None
    candidates: list[tuple[str, MappingProfile, str]] = []
    candidates += [(n, p, "vendor") for n, p in _vendor_profiles(db).items()]
    with db.connection() as conn:
        for r in conn.execute("SELECT name, mapping_json FROM import_mappings WHERE deleted_at IS NULL"):
            try:
                candidates.append((r["name"], MappingProfile.from_json(r["mapping_json"]), "user"))
            except (IngestionError, ValueError, TypeError):
                continue
    candidates += [(n, p, "builtin") for n, p in BUILTIN_PROFILES.items()]
    rank = {"vendor": 2, "user": 1, "builtin": 0}
    best: Detection | None = None
    for name, profile, origin in candidates:
        scores = [s for row in rows if (s := fit_score(profile, row)) is not None]
        if not scores:
            continue
        found = Detection(name, profile, origin, max(scores))
        if best is None or (found.score, rank[origin]) > (best.score, rank[best.origin]):
            best = found
    return best


def list_profiles(db: Any) -> list[str]:
    with db.connection() as conn:
        user = [r[0] for r in conn.execute("SELECT name FROM import_mappings WHERE deleted_at IS NULL ORDER BY name")]
    return sorted(set(user) | set(BUILTIN_PROFILES) | set(_vendor_profiles(db)), key=str.casefold)
