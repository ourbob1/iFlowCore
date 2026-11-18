"""
Co-employment guardrails.

iFlow Health is designed so that nothing about a gig worker's coverage looks
like an employment relationship with the platforms they drive or deliver
for (the pattern that raises PEO / co-employment questions):

  1. The sponsor is an association or a carrier, never a gig platform.
  2. No platform pays toward premiums (contribution must be exactly 0).
  3. Premiums are paid by the member ("member-directed"), never by payroll
     deduction at a platform. The fractional withholding in this app is the
     member moving *their own* money into *their own* reserve on their own
     device; platforms never see it or take part.
  4. Enrollment data sent to a brokerage never names the platforms the member
     works for, their earnings per platform, or any platform identifiers.

These checks make the app's own behavior consistent with that design. They
are not a legal determination: whether a given association health plan may
cover working owners depends on federal rules (which have changed several
times) and state law, and that is the association's and carrier's call.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from core.health.models import HealthError, PlanOffer

ALLOWED_SPONSORS = ("association", "carrier")
# Keys that must never appear anywhere in a payload sent to a brokerage.
FORBIDDEN_PAYLOAD_KEYS = frozenset({
    "platform", "platforms", "platform_id", "platform_ids", "employer", "employer_id", "payroll",
    "payroll_deduction", "earnings_by_platform", "gig_platforms", "transactions",
    # medical information: never collected, so never sent
    "diagnosis", "diagnoses", "conditions", "medications", "tobacco", "claims", "health_history",
})
DISCLAIMER = ("Eligibility and plan rules are set by the association and its carrier. iFlow Core's checks are a "
              "pre-screen, not insurance or legal advice.")


class ComplianceError(HealthError):
    pass


def check_offer(offer: PlanOffer) -> None:
    """Refuse offers that would tie coverage to a gig platform."""
    if offer.sponsor_type not in ALLOWED_SPONSORS:
        raise ComplianceError(f"Offer '{offer.name}' is sponsored by a {offer.sponsor_type}; only association or "
                              "carrier plans are offered, to keep coverage independent of any platform")
    if offer.platform_contribution_minor != 0:
        raise ComplianceError(f"Offer '{offer.name}' includes a platform contribution")
    if not offer.member_directed_payment:
        raise ComplianceError(f"Offer '{offer.name}' requires payroll deduction instead of member-paid premiums")


def filter_offers(offers: Iterable[PlanOffer]) -> tuple[list[PlanOffer], list[str]]:
    """(allowed offers, reasons for the refused ones)."""
    ok, refused = [], []
    for o in offers:
        try:
            check_offer(o)
        except ComplianceError as exc:
            refused.append(str(exc))
        else:
            ok.append(o)
    return ok, refused


def forbidden_keys(payload: Any, path: str = "") -> list[str]:
    """Every forbidden key anywhere in a nested payload (dotted paths)."""
    found: list[str] = []
    if isinstance(payload, Mapping):
        for k, v in payload.items():
            here = f"{path}.{k}" if path else str(k)
            if str(k).lower() in FORBIDDEN_PAYLOAD_KEYS:
                found.append(here)
            found += forbidden_keys(v, here)
    elif isinstance(payload, (list, tuple)):
        for i, v in enumerate(payload):
            found += forbidden_keys(v, f"{path}[{i}]")
    return found


def check_payload(payload: Mapping) -> None:
    bad = forbidden_keys(payload)
    if bad:
        raise ComplianceError(f"Payload contains fields that must never be sent: {', '.join(bad)}")
