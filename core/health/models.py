"""
iFlow Health data models.

Deliberately *not* modeled, anywhere: diagnoses, conditions, medications,
claims, tobacco use, or any other medical information. Group coverage offered
through an association is priced by coverage tier, age band and area only, so
the app never needs to ask a health question and never stores an answer to
one.

Money is integer minor units; ages are bands, never birth dates, whenever
data leaves the device.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Mapping


class HealthError(ValueError):
    pass


class Line(str, Enum):
    HEALTH = "health"
    DENTAL = "dental"
    LIFE = "life"

    @property
    def label(self) -> str:
        return {"health": "Health", "dental": "Dental", "life": "Life"}[self.value]


class CoverageTier(str, Enum):
    SELF = "self"
    SELF_SPOUSE = "self_spouse"
    SELF_CHILDREN = "self_children"
    FAMILY = "family"

    @property
    def label(self) -> str:
        return {"self": "Just me", "self_spouse": "Me + spouse/partner", "self_children": "Me + children",
                "family": "Family"}[self.value]

    @property
    def members(self) -> int:
        """Minimum people covered (used for plan member limits)."""
        return {"self": 1, "self_spouse": 2, "self_children": 2, "family": 3}[self.value]


AGE_BANDS = ((18, 29), (30, 39), (40, 49), (50, 59), (60, 64), (65, 120))


def age_band(age: int) -> str:
    for lo, hi in AGE_BANDS:
        if lo <= age <= hi:
            return f"{lo}-{hi}" if hi < 120 else f"{lo}+"
    raise HealthError("Coverage is for adults 18 and over")


def age_on(birth_year: int, on: date) -> int:
    """Age in whole years at `on` from the birth *year* only: the age reached this year (quotes
    use the older possible age so a premium is never understated; eligibility checks both)."""
    return on.year - birth_year


@dataclass(frozen=True)
class MemberProfile:
    """What the user tells the app about themselves for coverage. Stored sealed on the device.
    Only `state`, `age band` and `coverage tier` are ever needed for a quote."""

    state: str                              # two-letter US state / region code of residence
    birth_year: int
    coverage_tier: CoverageTier = CoverageTier.SELF
    association_member_since: date | None = None
    association_member_id: str | None = None   # a credential: sealed, never logged, never in quotes

    def __post_init__(self) -> None:
        st = (self.state or "").strip().upper()
        if len(st) != 2 or not st.isalpha():
            raise HealthError("State must be a two-letter code")
        object.__setattr__(self, "state", st)
        if not 1900 <= int(self.birth_year) <= date.today().year:
            raise HealthError("Birth year looks wrong")
        if not isinstance(self.coverage_tier, CoverageTier):
            object.__setattr__(self, "coverage_tier", CoverageTier(self.coverage_tier))

    def age_band(self, on: date) -> str:
        return age_band(age_on(self.birth_year, on))

    def to_dict(self) -> dict:
        return {"state": self.state, "birth_year": self.birth_year, "coverage_tier": self.coverage_tier.value,
                "association_member_since": self.association_member_since.isoformat()
                if self.association_member_since else None,
                "association_member_id": self.association_member_id}

    @classmethod
    def from_dict(cls, d: Mapping) -> "MemberProfile":
        since = d.get("association_member_since")
        return cls(state=d["state"], birth_year=int(d["birth_year"]),
                   coverage_tier=CoverageTier(d.get("coverage_tier", "self")),
                   association_member_since=date.fromisoformat(since) if since else None,
                   association_member_id=d.get("association_member_id"))


@dataclass(frozen=True)
class EligibilityRules:
    """What an association / carrier requires. Every field is optional; None = no requirement."""

    min_membership_days: int | None = None          # member of the association this long
    states: tuple[str, ...] | None = None           # service area
    min_age: int | None = None
    max_age: int | None = None
    min_net_earnings_90d_minor: int | None = None   # evidence of a working self-employed business
    min_hours_per_week: int | None = None           # average worked hours (from the local session engine)
    max_members: int | None = None
    requires_line: Line | None = None               # e.g. life only alongside health


@dataclass(frozen=True)
class PlanOffer:
    """One plan a brokerage / carrier offers, priced for a specific quote request."""

    offer_id: str
    broker_id: str
    association: str
    carrier: str
    line: Line
    name: str
    guild_tier: str                                 # "Bronze", "Silver", ... or "Basic"/"Plus"
    monthly_premium_minor: int
    currency: str
    coverage_tier: CoverageTier
    sponsor_type: str = "association"               # association | carrier | platform (platform is refused)
    member_directed_payment: bool = True            # premiums paid by the member, not a platform
    platform_contribution_minor: int = 0            # any money from a gig platform (must be 0)
    summary: tuple[str, ...] = ()                   # plain-language highlights (deductible, network, ...)
    rules: EligibilityRules = field(default_factory=EligibilityRules)

    def __post_init__(self) -> None:
        for name in ("monthly_premium_minor", "platform_contribution_minor"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise HealthError(f"{name} must be a non-negative integer in minor units")
        if self.monthly_premium_minor == 0:
            raise HealthError("An offer must have a premium")


@dataclass(frozen=True)
class EnrollmentReceipt:
    broker_id: str
    offer_id: str
    enrollment_ref: str
    status: str                                     # pending | active
    coverage_start: date
    message: str = ""
