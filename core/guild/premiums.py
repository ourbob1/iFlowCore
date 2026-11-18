"""
Fractional premium withholding for an Association Health Plan (pure math).

Gig income is lumpy, but the health premium is due on the same day every
month. The idea: set aside a small, fixed *fraction of every payout* so the
premium is covered by the due date, the same way the app already handles tax
set-asides.

    rate_bp = ceil(monthly premium × 10 000 / expected monthly net)

Rounded *up* so the reserve never falls short by a rounding cent; each
payout's share is also rounded up. Expected monthly net comes from the user's
own trailing earnings (computed locally), scaled by days:

    monthly net = net over the window × 365 / (days in window × 12)

Cycle math: premiums are due on `due_day` each month; the "cycle" is the
month from the previous due date up to the next one. The status says how much
is reserved for the next premium, what's still missing, and what that means
per remaining week, all in integer minor units.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

BP = 10_000


class PremiumError(ValueError):
    pass


def _div(num: int, den: int, rounding: str = ROUND_HALF_UP) -> int:
    return int((Decimal(num) / Decimal(den)).quantize(Decimal(1), rounding=rounding))


def _check_money(value: int, name: str, *, positive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PremiumError(f"{name} must be an integer in minor units")
    if value < 0 or (positive and value == 0):
        raise PremiumError(f"{name} must be {'positive' if positive else 'non-negative'}")


def monthly_from_window(net_minor: int, days: int) -> int:
    """Average monthly net from `days` of history (365/12 days per month)."""
    if days <= 0:
        raise PremiumError("days must be positive")
    return _div(net_minor * 365, days * 12)


def withholding_rate_bp(monthly_premium_minor: int, expected_monthly_net_minor: int) -> int | None:
    """Share of each payout to set aside, in bp (rounded up). None when there's no income to
    withhold from; capped at 100% (the premium is more than the income)."""
    _check_money(monthly_premium_minor, "monthly premium", positive=True)
    if expected_monthly_net_minor <= 0:
        return None
    return min(BP, _div(monthly_premium_minor * BP, expected_monthly_net_minor, ROUND_CEILING))


def withhold(payout_minor: int, rate_bp: int) -> int:
    """Amount to set aside from one payout (rounded up to the minor unit; never above the payout)."""
    if not 0 <= rate_bp <= BP:
        raise PremiumError("rate must be 0–10000 bp")
    if payout_minor <= 0:
        return 0
    return min(payout_minor, _div(payout_minor * rate_bp, BP, ROUND_CEILING))


def next_due(today: date, due_day: int) -> date:
    """The next premium due date on or after today."""
    if not 1 <= due_day <= 28:
        raise PremiumError("due day must be 1–28")
    this_month = today.replace(day=due_day)
    if this_month >= today:
        return this_month
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return date(y, m, due_day)


def previous_due(due: date) -> date:
    y, m = (due.year - 1, 12) if due.month == 1 else (due.year, due.month - 1)
    return date(y, m, due.day)


@dataclass(frozen=True)
class CycleStatus:
    due_on: date
    cycle_start: date                  # previous due date (exclusive) → due date
    premium_minor: int
    reserved_minor: int                # net reserve (reserves − withdrawals) available for this premium
    paid_minor: int                    # already paid toward the premium covering `due_on`'s month
    shortfall_minor: int
    days_left: int
    per_week_minor: int                # to close the shortfall by the due date
    level: str                         # good | warning | serious | critical

    @property
    def covered(self) -> bool:
        return self.shortfall_minor == 0


def cycle_status(*, premium_minor: int, due_day: int, today: date, reserved_minor: int,
                 paid_minor: int = 0) -> CycleStatus:
    _check_money(premium_minor, "premium", positive=True)
    due = next_due(today, due_day)
    start = previous_due(due)
    covered = max(0, reserved_minor) + max(0, paid_minor)
    shortfall = max(0, premium_minor - covered)
    days_left = (due - today).days
    weeks = max(1, -(-days_left // 7))
    per_week = _div(shortfall, weeks, ROUND_CEILING) if shortfall else 0
    if shortfall == 0:
        level = "good"
    elif days_left <= 3:
        level = "critical"
    elif days_left <= 7:
        level = "serious"
    else:
        level = "warning"
    return CycleStatus(due, start, premium_minor, max(0, reserved_minor), max(0, paid_minor), shortfall, days_left,
                       per_week, level)


@dataclass(frozen=True)
class CombinedWithholding:
    tax_bp: int
    health_bp: int

    @property
    def total_bp(self) -> int:
        return min(BP, self.tax_bp + self.health_bp)

    def split(self, payout_minor: int) -> tuple[int, int, int]:
        """(tax, health, take-home) for one payout; parts always add up to the payout."""
        health = withhold(payout_minor, self.health_bp)
        tax = min(payout_minor - health, withhold(payout_minor, self.tax_bp)) if payout_minor > 0 else 0
        return tax, health, payout_minor - tax - health


def months_covered(paid_months: set[str], start: date, today: date) -> tuple[int, list[str]]:
    """(months paid, months missing) from coverage start through the current month ("YYYY-MM")."""
    missing = []
    paid = 0
    y, m = start.year, start.month
    while (y, m) <= (today.year, today.month):
        key = f"{y:04d}-{m:02d}"
        if key in paid_months:
            paid += 1
        else:
            missing.append(key)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return paid, missing


def trailing_window(today: date, days: int = 90) -> tuple[date, date]:
    return today - timedelta(days=days - 1), today
