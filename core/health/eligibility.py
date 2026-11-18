"""
AHP eligibility pre-check, computed on the device.

Each rule in an offer's EligibilityRules becomes one check with an outcome:

  pass      the rule is met
  fail      the rule is clearly not met
  unknown   the app can't tell (missing profile data, or a birth-year-only age
            that could fall on either side of a limit); the association decides

Overall: any fail → not_eligible; else any unknown → check_with_association;
else likely_eligible. "Likely": the association and carrier make the actual
determination (see compliance.DISCLAIMER). No medical question is ever part
of eligibility; group plans offered here don't underwrite on health.

Work evidence (earnings, hours) comes from the local Personal Intelligence
engine over the last 90 days and never leaves the device; only the pass/fail
result is shown to the user.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable

from core.health.models import CoverageTier, EligibilityRules, Line, MemberProfile

EVIDENCE_DAYS = 90


@dataclass(frozen=True)
class WorkEvidence:
    net_earnings_minor: int          # last 90 days, base currency
    worked_seconds: int              # reconstructed work sessions, last 90 days
    days: int = EVIDENCE_DAYS

    @property
    def hours_per_week(self) -> int:
        """Whole hours per week (rounded down: evidence should never be overstated)."""
        return self.worked_seconds * 7 // (self.days * 3600) if self.days else 0


@dataclass(frozen=True)
class Check:
    rule: str
    outcome: str                     # pass | fail | unknown
    message: str


@dataclass(frozen=True)
class EligibilityResult:
    status: str                      # likely_eligible | check_with_association | not_eligible
    checks: tuple[Check, ...]

    @property
    def failed(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.outcome == "fail")


def work_evidence(db: Any, *, today: date, tz: str, currency: str) -> WorkEvidence:
    from core.intelligence.data import load_activity
    from core.intelligence.metrics import efficiency
    from core.intelligence.sessions import build_sessions

    act = load_activity(db, start=today - timedelta(days=EVIDENCE_DAYS - 1), end=today, tz=tz, currency=currency)
    eff = efficiency(act, build_sessions(act), vehicle_cost=lambda d: 0)
    return WorkEvidence(eff.net_minor, eff.seconds)


def _ages(birth_year: int, on: date) -> tuple[int, int]:
    """(youngest, oldest) possible age from a birth year alone."""
    return on.year - birth_year - 1, on.year - birth_year


def evaluate(rules: EligibilityRules, profile: MemberProfile | None, *, today: date,
             evidence: WorkEvidence | None = None, coverage_tier: CoverageTier | None = None,
             active_lines: Iterable[Line] = ()) -> EligibilityResult:
    checks: list[Check] = []
    tier = coverage_tier or (profile.coverage_tier if profile else CoverageTier.SELF)
    lines = set(active_lines)

    def add(rule: str, outcome: str, message: str) -> None:
        checks.append(Check(rule, outcome, message))

    if rules.states is not None:
        if profile is None:
            add("service_area", "unknown", "Add your state to check the service area.")
        elif profile.state in rules.states:
            add("service_area", "pass", f"Available in {profile.state}.")
        else:
            add("service_area", "fail", f"Not offered in {profile.state} (available in {', '.join(rules.states)}).")

    if rules.min_age is not None or rules.max_age is not None:
        if profile is None:
            add("age", "unknown", "Add your birth year to check age limits.")
        else:
            young, old = _ages(profile.birth_year, today)
            lo = rules.min_age if rules.min_age is not None else 0
            hi = rules.max_age if rules.max_age is not None else 200
            if lo <= young and old <= hi:
                add("age", "pass", "Within the plan's age range.")
            elif old < lo or young > hi:
                add("age", "fail", f"This plan covers ages {lo}–{hi}.")
            else:
                add("age", "unknown", f"You're at the edge of the {lo}–{hi} age range; the association will confirm.")

    if rules.min_membership_days is not None:
        since = profile.association_member_since if profile else None
        if since is None:
            add("membership", "unknown", f"Association membership of {rules.min_membership_days}+ days is required; "
                                         "add your join date.")
        else:
            days = (today - since).days
            if days >= rules.min_membership_days:
                add("membership", "pass", f"Member for {days} days.")
            else:
                ready = since + timedelta(days=rules.min_membership_days)
                add("membership", "fail", f"Eligible from {ready:%b} {ready.day}, {ready.year} "
                                          f"({rules.min_membership_days} days of membership).")

    if rules.min_net_earnings_90d_minor is not None:
        if evidence is None:
            add("self_employment", "unknown", "No earnings history to show self-employment yet.")
        elif evidence.net_earnings_minor >= rules.min_net_earnings_90d_minor:
            add("self_employment", "pass", "Your recent earnings show an active self-employed business.")
        else:
            add("self_employment", "fail", "Recent self-employment earnings are below the association's minimum.")

    if rules.min_hours_per_week is not None:
        if evidence is None or evidence.worked_seconds == 0:
            add("hours", "unknown", "Log trips (or turn on trip detection) so working hours can be shown.")
        elif evidence.hours_per_week >= rules.min_hours_per_week:
            add("hours", "pass", f"About {evidence.hours_per_week} hours a week of work.")
        else:
            add("hours", "fail", f"About {evidence.hours_per_week} hours a week; the plan asks for "
                                 f"{rules.min_hours_per_week}+.")

    if rules.max_members is not None:
        if tier.members <= rules.max_members:
            add("members", "pass", f"{tier.label} is covered.")
        else:
            add("members", "fail", f"This plan covers up to {rules.max_members} people.")

    if rules.requires_line is not None:
        if rules.requires_line in lines:
            add("companion_line", "pass", f"You have {rules.requires_line.label.lower()} coverage.")
        else:
            add("companion_line", "fail", f"Available alongside {rules.requires_line.label.lower()} coverage.")

    outcomes = {c.outcome for c in checks}
    status = "not_eligible" if "fail" in outcomes else "check_with_association" if "unknown" in outcomes \
        else "likely_eligible"
    return EligibilityResult(status, tuple(checks))
