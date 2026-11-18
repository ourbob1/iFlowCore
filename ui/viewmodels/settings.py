"""Settings helpers that write to the shared data model (tax profile, platforms)."""

from __future__ import annotations

import uuid
from typing import Any

from core.financial.tax import FILING_STATUSES, TaxTableRegistry

STATUS_LABELS = {
    "single": "Single", "married_joint": "Married filing jointly", "married_separate": "Married filing separately",
    "head_of_household": "Head of household", "qualifying_surviving_spouse": "Qualifying surviving spouse",
    "other": "Other",
}


def region_options(registry: TaxTableRegistry | None = None) -> list[str]:
    registry = registry or TaxTableRegistry.load()
    return [j for j in registry.jurisdictions() if j.startswith("US-")]


def current_tax_profile(db: Any, year: int) -> dict[str, Any] | None:
    with db.connection() as conn:
        row = conn.execute(
            "SELECT id, filing_status, region_code FROM tax_profiles WHERE tax_year = ? AND deleted_at IS NULL "
            "ORDER BY is_default DESC, updated_at DESC LIMIT 1", (year,)).fetchone()
    return dict(row) if row else None


def save_tax_profile(db: Any, year: int, *, filing_status: str, region_code: str | None) -> str:
    """Create or update the year's default US tax profile (bound to tax_profiles)."""
    if filing_status not in FILING_STATUSES:
        raise ValueError(f"Unknown filing status '{filing_status}'")
    region = region_code.upper() if region_code else None
    existing = current_tax_profile(db, year)
    with db.transaction() as conn:
        if existing:
            conn.execute("UPDATE tax_profiles SET filing_status = ?, region_code = ?, is_default = 1 WHERE id = ?",
                         (filing_status, region, existing["id"]))
            return existing["id"]
        new_id = str(uuid.uuid4())
        conn.execute("INSERT INTO tax_profiles (id, tax_year, country_code, region_code, filing_status, is_default) "
                     "VALUES (?, ?, 'US', ?, ?, 1)", (new_id, year, region, filing_status))
        return new_id


def add_platform(db: Any, name: str, kind: str = "other") -> str:
    name = name.strip()
    if not name:
        raise ValueError("Platform name is required")
    new_id = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute("INSERT INTO platforms (id, name, kind) VALUES (?, ?, ?)", (new_id, name, kind))
    return new_id


def list_platforms(db: Any) -> list[tuple[str, str]]:
    with db.connection() as conn:
        return [(r["id"], r["name"]) for r in conn.execute(
            "SELECT id, name FROM platforms WHERE deleted_at IS NULL ORDER BY name COLLATE NOCASE")]
