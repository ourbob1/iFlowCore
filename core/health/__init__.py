"""
iFlow Health & Guild engine (Phase 8).

    models.py       lines (health/dental/life), coverage tiers, member profile, offers, eligibility rules
    compliance.py   co-employment guardrails + fields that may never be sent (platform, payroll, medical)
    eligibility.py  AHP eligibility pre-check from the profile and local work evidence
    brokers.py      approved brokerages: sandbox catalog, HTTPS REST adapter, minimized payloads
    enrollment.py   browse → pre-check → consent → enroll → status tracking
    withholding.py  fractional premium withholding driven by the earnings stream, reserve status

No medical information is modeled, stored, logged or sent.
"""

from core.health.models import CoverageTier, HealthError, Line, MemberProfile, PlanOffer
from core.health.withholding import WithholdingPolicy, reserve_status, sync_withholdings

__all__ = ["CoverageTier", "HealthError", "Line", "MemberProfile", "PlanOffer", "WithholdingPolicy",
           "reserve_status", "sync_withholdings"]
