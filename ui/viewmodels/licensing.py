"""
Plans view-model and the app-side feature gates.

Gates live at the *action* boundary (start the sentry, decode a VIN, scan a
receipt, export a Quicken file, use the Guild tools) and raise FeatureLocked;
views catch it and show an upgrade prompt. Core library functions stay
ungated so data can always be read, and plain CSV export always works.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from core.licensing import (
    FEATURES,
    FREE_VEHICLE_LIMIT,
    TIER_INFO,
    Feature,
    FeatureLocked,
    LicenseState,
    Period,
    Plan,
    Status,
    Tier,
    plans,
)
from core.licensing.pricing import annual_savings_bp
from core.licensing.tiers import new_in
from ui.format import money, percent_bp
from ui.state import AppState

# --- plans page ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TierCard:
    tier: Tier
    name: str
    tagline: str
    price: str                  # "Free", "$6.99 / month"
    price_note: str | None      # "or $60 / year (save 28%)"
    features: tuple[str, ...]   # what this tier adds
    includes_below: str | None  # "Everything in Solo Core, plus:"
    current: bool
    plans: tuple[Plan, ...]


@dataclass(frozen=True)
class ComparisonRow:
    label: str
    description: str
    tiers: tuple[bool, bool, bool]   # Solo, Pro, Guild


@dataclass(frozen=True)
class StatusView:
    level: str                  # good | warning | serious | critical
    title: str
    message: str


def status_view(st: LicenseState, currency: str = "USD") -> StatusView:
    if st.status is Status.FREE:
        return StatusView("good", "Solo Core", "Free forever. No account, no card, no tracking.")
    if st.status is Status.ACTIVE and st.claims:
        renew = st.claims.expires_at.date()
        kind = "Simulated license" if st.claims.issuer == "simulation" else "License"
        return StatusView("good", f"{st.tier.label} active",
                          f"{kind} valid until {renew:%b} {renew.day}, {renew.year} ({st.days_left} days).")
    if st.status is Status.GRACE:
        return StatusView("warning", f"{st.tier.label}: renew soon",
                          f"Your license expired. Features stay on for {st.days_left} more day(s).")
    if st.status is Status.EXPIRED:
        return StatusView("serious", "Back on Solo Core", st.message)
    return StatusView("critical", "License problem", f"{st.message} You're on Solo Core until it's fixed.")


def tier_cards(st: LicenseState, currency: str = "USD") -> list[TierCard]:
    offered = plans(currency)
    cards = []
    below = {Tier.PRO: TIER_INFO[Tier.SOLO].name, Tier.GUILD: TIER_INFO[Tier.PRO].name}
    for tier in (Tier.SOLO, Tier.PRO, Tier.GUILD):
        tier_plans = tuple(p for p in offered if p.tier is tier)
        monthly = next((p for p in tier_plans if p.period is Period.MONTHLY), None)
        annual = next((p for p in tier_plans if p.period is Period.ANNUAL), None)
        if tier is Tier.SOLO:
            price, note = "Free", "forever"
        else:
            ccy = monthly.currency if monthly else currency
            price = f"{money(monthly.amount_minor, ccy)} / month" if monthly else "—"
            note = None
            if annual:
                bp = annual_savings_bp(tier, ccy)
                note = f"or {money(annual.amount_minor, ccy, cents=False)} / year" + \
                    (f" (save {percent_bp(bp, decimals=0)})" if bp else "")
        features = tuple(FEATURES[f].label for f in new_in(tier))
        cards.append(TierCard(tier, TIER_INFO[tier].name, TIER_INFO[tier].tagline, price, note, features,
                              f"Everything in {below[tier]}, plus:" if tier in below else None,
                              st.tier is tier, tier_plans))
    return cards


def comparison() -> list[ComparisonRow]:
    rows = []
    for info in FEATURES.values():
        rows.append(ComparisonRow(info.label, info.description,
                                  tuple(t.includes(info.tier) for t in (Tier.SOLO, Tier.PRO, Tier.GUILD))))
    return rows


def plan_label(p: Plan) -> str:
    per = "month" if p.period is Period.MONTHLY else "year"
    return f"{p.tier.label} · {money(p.amount_minor, p.currency)} / {per}"


# --- gates ---------------------------------------------------------------------------------


def upgrade_message(exc: FeatureLocked) -> str:
    return f"{FEATURES[exc.feature].label} is part of {exc.required.label}."


def vehicle_count(db: Any) -> int:
    with db.connection() as conn:
        return conn.execute("SELECT count(*) FROM vehicles WHERE deleted_at IS NULL").fetchone()[0]


def check_can_add_vehicle(state: AppState) -> None:
    if vehicle_count(state.db) >= FREE_VEHICLE_LIMIT and not state.allows(Feature.FLEET):
        raise FeatureLocked(Feature.FLEET, state.license.tier)


def make_decoder(state: AppState, services: dict[str, Any]) -> Any | None:
    """VIN decoder for Pro (NHTSA, plus DataOne when credentials exist); None on Solo Core."""
    if not state.allows(Feature.VIN_DECODE):
        return None
    injected = services.get("decoder")
    if injected is not None:
        return injected
    from core.fleet.cache import DecodeCache
    from core.fleet.dataone import DataOneClient, DataOneCredentials
    from core.fleet.decoder import VehicleDecoder

    try:
        creds = DataOneCredentials.from_env()
    except Exception:
        creds = None
    return VehicleDecoder(dataone=DataOneClient(creds) if creds else None, cache=DecodeCache(state.db))


# --- exports -------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportFormat:
    key: str
    label: str
    extension: str
    feature: Feature


EXPORT_FORMATS: dict[str, ExportFormat] = {f.key: f for f in (
    ExportFormat("csv", "Ledger CSV (spreadsheet)", "csv", Feature.BASIC_EXPORT),
    ExportFormat("mileage_csv", "Mileage log CSV", "csv", Feature.BASIC_EXPORT),
    ExportFormat("quickbooks", "QuickBooks CSV", "csv", Feature.ADVANCED_EXPORTS),
    ExportFormat("qfx", "Quicken (QFX)", "qfx", Feature.ADVANCED_EXPORTS),
    ExportFormat("ofx", "OFX (Money, GnuCash…)", "ofx", Feature.ADVANCED_EXPORTS),
    ExportFormat("qif", "Quicken legacy (QIF)", "qif", Feature.ADVANCED_EXPORTS),
)}


def export_filename(fmt: ExportFormat, start: date, end: date) -> str:
    kind = "mileage" if fmt.key == "mileage_csv" else "ledger"
    suffix = "-quickbooks" if fmt.key == "quickbooks" else ""
    return f"iflowcore-{kind}{suffix}-{start.isoformat()}-to-{end.isoformat()}.{fmt.extension}"


def run_export(state: AppState, key: str, path: str | Path, *, start: date, end: date) -> Any:
    """Write an export after checking the tier. Returns the core ExportReport."""
    from core.financial.exports import export_ledger, export_mileage

    fmt = EXPORT_FORMATS.get(key)
    if fmt is None:
        raise ValueError(f"Unknown export format '{key}'")
    state.require(fmt.feature)
    if key == "mileage_csv":
        return export_mileage(state.db, path, start=start, end=end, unit_system=state.units)
    if key in ("csv", "quickbooks"):
        return export_ledger(state.db, path, fmt="csv", start=start, end=end, currency=state.currency,
                             base_currency=state.currency, csv_dialect="quickbooks_3col" if key == "quickbooks" else "generic")
    return export_ledger(state.db, path, fmt=key, start=start, end=end, currency=state.currency,  # type: ignore[arg-type]
                         base_currency=state.currency)
