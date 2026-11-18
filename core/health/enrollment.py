"""
Enrollment workflow: quote → eligibility pre-check → consent → enroll → track status.

The enrolled plan is stored as a `health_plans` row (line, carrier, brokerage,
offer, coverage tier, status, enrollment reference) so the premium pot,
withholding and due dates all work the same for enrolled and hand-entered
plans. Applicant details (legal name, email, birth date) are used for the one
enrollment request and are never stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from core.guild.store import active_plans, save_plan
from core.health.brokers import Applicant, BrokerAdapter, QuoteResult, SandboxBroker, enrollment_payload, quote_payload
from core.health.eligibility import EligibilityResult, WorkEvidence, evaluate
from core.health.models import CoverageTier, HealthError, Line, MemberProfile, PlanOffer


def first_of_next_month(today: date) -> date:
    return date(today.year + (today.month == 12), today.month % 12 + 1, 1)


@dataclass(frozen=True)
class QuotedOffer:
    offer: PlanOffer
    eligibility: EligibilityResult


@dataclass(frozen=True)
class Catalog:
    offers: tuple[QuotedOffer, ...]
    refused: tuple[str, ...]

    def by_line(self, line: Line) -> list[QuotedOffer]:
        return [q for q in self.offers if q.offer.line is line]


def browse(adapter: BrokerAdapter, profile: MemberProfile, *, today: date, evidence: WorkEvidence | None,
           lines: Iterable[Line] = tuple(Line), active_lines: Iterable[Line] = (),
           coverage_tier: CoverageTier | None = None, currency: str = "USD") -> Catalog:
    tier = coverage_tier or profile.coverage_tier
    result: QuoteResult = adapter.quote(quote_payload(profile, today=today, lines=lines, currency=currency,
                                                      coverage_tier=tier))
    active = list(active_lines)
    quoted = tuple(QuotedOffer(o, evaluate(o.rules, profile, today=today, evidence=evidence, coverage_tier=tier,
                                           active_lines=active))
                   for o in sorted(result.offers, key=lambda o: (o.line.value, o.monthly_premium_minor)))
    return Catalog(quoted, result.refused)


def enroll(db: Any, adapter: BrokerAdapter, quoted: QuotedOffer, profile: MemberProfile, applicant: Applicant, *,
           today: date, consented_at: str, coverage_start: date | None = None) -> str:
    """Send one enrollment and record the plan (pending until the carrier confirms / coverage starts).
    Refuses offers the pre-check says the member is not eligible for."""
    if quoted.eligibility.status == "not_eligible":
        raise HealthError("The pre-check says you're not eligible for this plan yet")
    offer = quoted.offer
    start = coverage_start or first_of_next_month(today)
    if start <= today:
        raise HealthError("Coverage has to start in the future")
    payload = enrollment_payload(offer, profile, applicant, coverage_start=start, consented_at=consented_at)
    receipt = adapter.enroll(payload)
    return save_plan(
        db, association=offer.association, plan_name=offer.name, monthly_premium_minor=offer.monthly_premium_minor,
        currency=offer.currency, due_day=min(receipt.coverage_start.day, 28),
        members_covered=offer.coverage_tier.members, coverage_start=receipt.coverage_start, line=offer.line.value,
        carrier=offer.carrier, broker_id=offer.broker_id, offer_id=offer.offer_id,
        coverage_tier=offer.coverage_tier.value, status=receipt.status, enrollment_ref=receipt.enrollment_ref,
    )


def refresh_statuses(db: Any, *, today: date, adapters: dict[str, BrokerAdapter] | None = None) -> int:
    """Pending enrollments become active: sandbox ones on their start date, others per the brokerage.
    Returns how many changed. Brokerage errors leave the plan pending."""
    changed = 0
    for plan in active_plans(db, include_pending=True):
        if plan.status != "pending":
            continue
        new_status = None
        adapter = (adapters or {}).get(plan.broker_id or "")
        if plan.broker_id == "sandbox" or adapter is None:
            if today >= plan.coverage_start:
                new_status = "active"
        else:
            try:
                new_status = adapter.status(plan.enrollment_ref or "").status
            except HealthError:
                continue
        if new_status and new_status != plan.status:
            with db.transaction() as conn:
                conn.execute("UPDATE health_plans SET status = ? WHERE id = ?", (new_status, plan.id))
            changed += 1
    return changed


def default_adapter() -> BrokerAdapter:
    return SandboxBroker()
