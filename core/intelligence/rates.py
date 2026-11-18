"""Integer rate helpers. Money stays in minor units; every division rounds half-up (away from zero)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

METERS_PER_MILE_X1000 = 1_609_344        # 1 mile = 1609.344 m, kept exact as an integer
BP = 10_000


def div_round(num: int, den: int) -> int:
    return int((Decimal(num) / Decimal(den)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def per_hour(amount_minor: int, seconds: int) -> int | None:
    """Minor units per hour; None when no time was recorded."""
    if seconds <= 0:
        return None
    return div_round(amount_minor * 3600, seconds)


def per_mile(amount_minor: int, meters: int) -> int | None:
    if meters <= 0:
        return None
    return div_round(amount_minor * METERS_PER_MILE_X1000, meters * 1000)


def per_km(amount_minor: int, meters: int) -> int | None:
    if meters <= 0:
        return None
    return div_round(amount_minor * 1000, meters)


def ratio_bp(num: int, den: int) -> int | None:
    if den == 0:
        return None
    return div_round(num * BP, den)


def change_bp(current: int, previous: int) -> int | None:
    """Relative change in basis points (+1500 = +15%); None when there's no baseline."""
    if previous <= 0:
        return None
    return div_round((current - previous) * BP, previous)
