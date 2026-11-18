"""Finances view-model: recent ledger, statement import preview/commit, set-asides."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from core.financial.exports import collect_ledger
from core.financial.ingestion import (
    ImportResult,
    IngestionError,
    ParseResult,
    detect_profile,
    import_records,
    list_profiles,
    load_profile,
    parse_statement,
)
from ui.format import money


@dataclass(frozen=True)
class LedgerRow:
    day: date
    kind: str
    category: str
    payee: str
    amount_minor: int
    amount: str


@dataclass(frozen=True)
class FinancesData:
    rows: tuple[LedgerRow, ...]
    profiles: tuple[str, ...]
    platforms: tuple[tuple[str, str], ...]          # (id, name)
    set_asides: tuple[tuple[str, str, str], ...]    # (date, kind, amount)
    warnings: tuple[str, ...]


def load_finances(db: Any, *, today: date, currency: str, days: int = 30) -> FinancesData:
    ledger = collect_ledger(db, today - timedelta(days=days), today, currency=currency)
    rows = tuple(
        LedgerRow(e.posted_on, e.kind, e.category.split(":")[-1], e.payee, e.amount_minor, money(e.amount_minor, currency))
        for e in reversed(ledger.entries)
    )
    with db.connection() as conn:
        platforms = tuple((r["id"], r["name"]) for r in conn.execute(
            "SELECT id, name FROM platforms WHERE deleted_at IS NULL AND is_active = 1 ORDER BY name COLLATE NOCASE"))
        asides = tuple(
            (r["occurred_on"], r["kind"], money(r["amount_minor"], r["currency"]))
            for r in conn.execute("SELECT occurred_on, kind, amount_minor, currency FROM tax_set_asides "
                                  "WHERE deleted_at IS NULL AND tax_year = ? ORDER BY occurred_on DESC", (today.year,))
        )
    return FinancesData(rows, tuple(list_profiles(db)), platforms, asides, tuple(ledger.warnings))


@dataclass(frozen=True)
class ImportPreview:
    path: str
    profile: str
    result: ParseResult

    @property
    def summary(self) -> str:
        r = self.result
        return (f"{len(r.records)} records from {r.rows_seen} rows"
                + (f", {r.rows_skipped} skipped" if r.rows_skipped else "")
                + (f", {len(r.errors)} errors" if r.errors else ""))


def preview_import(db: Any, path: str, profile_name: str, *, tz: str) -> ImportPreview:
    profile = load_profile(db, profile_name)
    if not Path(path).is_file():
        raise IngestionError(f"File not found: {path}")
    return ImportPreview(path, profile_name, parse_statement(Path(path), profile, timezone=tz))


@dataclass(frozen=True)
class Recognition:
    profile: str                   # a name from list_profiles()
    platform_id: str | None        # the user's matching platform, if they have one
    label: str                     # what the import card says


def recognize(db: Any, path: str, platforms: tuple[tuple[str, str], ...]) -> Recognition | None:
    """Pick the statement format (and platform) from a CSV's header line. PDFs are chosen by hand."""
    if Path(path).suffix.lower() != ".csv":
        return None
    found = detect_profile(db, Path(path))
    if found is None:
        return None
    platform = found.profile.platform.strip().casefold()
    platform_id = next((pid for pid, name in platforms if name.strip().casefold() == platform), None)
    label = {"vendor": f"Recognized: {found.name}",
             "user": f"Recognized your saved format \u201c{found.name}\u201d",
             "builtin": f"Recognized: {found.profile.platform} statement ({found.name})"}[found.origin]
    return Recognition(found.name, platform_id, label)


def commit_import(db: Any, preview: ImportPreview, *, platform_id: str, base_currency: str) -> ImportResult:
    # Good rows are imported; the preview already showed the user any bad ones.
    return import_records(db, platform_id, preview.result.records, base_currency=base_currency)
