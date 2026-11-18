"""
iFlow Health view-model: coverage, the withholding reserve, per-platform contributions, and the
guild catalog with eligibility pre-checks. Everything is computed locally; the only network
traffic is a quote/enrollment to an approved brokerage (none at all with the sandbox).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from core.guild.store import HealthPlan, active_plans
from core.health.brokers import SandboxBroker, adapter_for
from core.health.compliance import DISCLAIMER
from core.health.eligibility import WorkEvidence, work_evidence
from core.health.enrollment import Catalog, browse, refresh_statuses
from core.health.models import CoverageTier, HealthError, Line, MemberProfile
from core.health.withholding import (
    PlatformShare,
    ReserveStatus,
    SyncReport,
    WithholdingPolicy,
    platform_split,
    reserve_status,
    sync_withholdings,
)
from core.licensing import Feature
from ui.format import money, percent_bp
from ui.state import AppState

SAMPLE_PROFILE = MemberProfile(state="FL", birth_year=1990)       # preview pricing for non-members

MODE_LABELS = {"auto": "Automatic (just enough for the next premium)", "percent": "Fixed % of each payout",
               "per_payout": "Fixed amount per payout", "off": "Off"}
STATUS_LABELS = {"likely_eligible": "Likely eligible", "check_with_association": "Check with the association",
                 "not_eligible": "Not eligible yet"}
STATUS_LEVEL = {"likely_eligible": "good", "check_with_association": "warning", "not_eligible": "serious"}


def profile(state: AppState) -> MemberProfile | None:
    raw = state.settings.health_profile
    if not raw:
        return None
    try:
        return MemberProfile.from_dict(raw)
    except (HealthError, KeyError, ValueError, TypeError):
        return None


def save_profile(state: AppState, p: MemberProfile) -> None:
    state.update_settings(health_profile=p.to_dict())


def policy(state: AppState) -> WithholdingPolicy:
    try:
        return WithholdingPolicy.from_dict(state.settings.health_withholding)
    except ValueError:
        return WithholdingPolicy()


def save_policy(state: AppState, p: WithholdingPolicy) -> None:
    state.update_settings(health_withholding=p.to_dict())


def sync_now(state: AppState) -> SyncReport | None:
    """Withhold from new payouts (iFlow Health & Guild only). Safe to call as often as you like."""
    if state.db is None or not state.allows(Feature.PREMIUM_WITHHOLDING):
        return None
    refresh_statuses(state.db, today=state.today())
    return sync_withholdings(state.db, policy=policy(state), today=state.today(), tz=state.tz,
                             currency=state.currency)


def adapter(state: AppState, services: dict[str, Any], broker_id: str = "sandbox") -> Any:
    if broker_id == "sandbox":
        return services.setdefault("health_sandbox", SandboxBroker())
    return adapter_for(broker_id, tokens=state.settings.health_broker_tokens or {})


# --- view data -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class CoverageRow:
    plan: HealthPlan
    title: str
    detail: str
    status: str


@dataclass
class HealthView:
    allowed: bool
    profile: MemberProfile | None
    policy: WithholdingPolicy
    coverage: list[CoverageRow]
    reserve: ReserveStatus
    split: list[PlatformShare]
    catalog: Catalog | None
    catalog_note: str | None
    evidence: WorkEvidence | None
    sync: SyncReport | None
    tier: CoverageTier
    notes: list[str] = field(default_factory=list)
    disclaimer: str = DISCLAIMER


def coverage_rows(plans: list[HealthPlan]) -> list[CoverageRow]:
    rows = []
    for p in plans:
        tier = CoverageTier(p.coverage_tier).label
        status = {"pending": f"Starts {p.coverage_start:%b} {p.coverage_start.day}", "active": "Active",
                  "ended": "Ended"}[p.status]
        detail = f"{p.association}" + (f" · {p.carrier}" if p.carrier else "") + \
            f" · {tier} · {money(p.monthly_premium_minor, p.currency)}/month, due day {p.due_day}"
        rows.append(CoverageRow(p, f"{Line(p.line).label}: {p.plan_name}", detail, status))
    return rows


def load_health(state: AppState, services: dict[str, Any], params: dict[str, str] | None = None) -> HealthView:
    params = params or {}
    allowed = state.allows(Feature.AHP_POOL)
    today = state.today()
    report = sync_now(state) if allowed else None
    prof = profile(state)
    try:
        tier = CoverageTier(params.get("tier") or (prof.coverage_tier.value if prof else "self"))
    except ValueError:
        tier = CoverageTier.SELF
    plans = active_plans(state.db, include_pending=True)
    evidence = work_evidence(state.db, today=today, tz=state.tz, currency=state.currency) if allowed else None
    quote_profile = prof if (allowed and prof) else SAMPLE_PROFILE
    catalog, note = None, None
    try:
        catalog = browse(adapter(state, services), quote_profile, today=today, evidence=evidence,
                         active_lines=[Line(p.line) for p in plans], coverage_tier=tier, currency="USD")
    except HealthError as exc:
        note = str(exc)
    if quote_profile is SAMPLE_PROFILE:
        note = ("Sample prices for a 30–39 year-old in FL. " if not allowed
                else "Add your details below to see your own prices and eligibility. Sample: 30–39 in FL. ")
    month_start = today.replace(day=1)
    view = HealthView(
        allowed=allowed, profile=prof, policy=policy(state), coverage=coverage_rows(plans),
        reserve=reserve_status(state.db, today=today, currency=state.currency),
        split=platform_split(state.db, start=month_start, end=today, tz=state.tz, currency=state.currency)
        if allowed else [],
        catalog=catalog, catalog_note=note, evidence=evidence, sync=report, tier=tier,
    )
    if report and report.plans_other_currency:
        view.notes.append(f"{report.plans_other_currency} plan(s) aren't in {state.currency}, so they aren't "
                          "funded automatically.")
    if report and report.skipped_currency:
        view.notes.append(f"{report.skipped_currency} foreign-currency payout(s) without a converted amount were "
                          "skipped.")
    return view


def policy_summary(p: WithholdingPolicy, currency: str) -> str:
    if p.mode == "auto":
        text = "Automatic: just enough of each payout to fund your next premiums"
    elif p.mode == "percent":
        text = f"{percent_bp(p.rate_bp, decimals=2)} of each payout ({p.rate_bp / 100:g}¢ per dollar)"
    elif p.mode == "per_payout":
        text = f"{money(p.per_payout_minor, currency)} from each payout"
    else:
        return "Off: nothing is set aside automatically"
    extra = f", never more than {percent_bp(p.cap_bp, decimals=0)} of a payout"
    if p.excluded_platforms:
        extra += f", {len(p.excluded_platforms)} platform(s) excluded"
    return text + extra


def reserve_lines(r: ReserveStatus, currency: str) -> list[str]:
    if r.due_on is None:
        return ["No coverage yet. Enroll in a plan, or add one you already have, and payouts start funding it."]
    lines = [f"Next premiums due {r.due_on:%b} {r.due_on.day}: {money(r.premium_due_minor, currency)}"
             + (" · covered" if r.shortfall_minor == 0 else f" · {money(r.shortfall_minor, currency)} still to go")]
    lines.append(f"This week: {money(r.this_week_minor, currency)} set aside of about "
                 f"{money(r.weekly_need_minor, currency)} a week")
    return lines


def evidence_text(e: WorkEvidence | None, currency: str) -> str | None:
    if e is None:
        return None
    return (f"From your last 90 days on this device: {money(e.net_earnings_minor, currency, cents=False)} net, "
            f"about {e.hours_per_week} hours a week. Used only for the pre-check; never sent.")


def week_range(today: date) -> tuple[date, date]:
    monday = today - timedelta(days=today.weekday())
    return monday, today
