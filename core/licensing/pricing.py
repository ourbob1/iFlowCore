"""
Price book and subscription math, in integer minor units.

Prices are listed per currency, never converted with an exchange rate: a
price in a currency we don't list is simply unavailable (shown in USD). Every
amount is validated against the currency's minor-unit exponent.

    iFlow Pro             699 / month    6000 / year   (USD cents)
    iFlow Health & Guild 1499 / month

All divisions round half-up in minor units, except credits for unused time,
which round *down* (a customer is never promised more than they paid for).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from enum import Enum

from core.config import minor_units, normalize_currency
from core.licensing.tiers import Tier


class Period(str, Enum):
    MONTHLY = "monthly"
    ANNUAL = "annual"

    @property
    def months(self) -> int:
        return 1 if self is Period.MONTHLY else 12


class PricingError(ValueError):
    pass


@dataclass(frozen=True)
class Plan:
    id: str                       # e.g. "pro_monthly"
    tier: Tier
    period: Period
    currency: str
    amount_minor: int

    def __post_init__(self) -> None:
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int) or self.amount_minor < 0:
            raise PricingError("Plan price must be a non-negative integer in minor units")
        normalize_currency(self.currency)

    @property
    def monthly_equivalent_minor(self) -> int:
        return _div(self.amount_minor, self.period.months, ROUND_HALF_UP)


def _div(num: int, den: int, rounding: str) -> int:
    return int((Decimal(num) / Decimal(den)).quantize(Decimal(1), rounding=rounding))


# (tier, period, currency) → price in minor units
PRICE_BOOK: dict[tuple[Tier, Period, str], int] = {
    (Tier.PRO, Period.MONTHLY, "USD"): 699,
    (Tier.PRO, Period.ANNUAL, "USD"): 6000,
    (Tier.GUILD, Period.MONTHLY, "USD"): 1499,
}
DEFAULT_CURRENCY = "USD"


def plan(tier: Tier, period: Period, currency: str = DEFAULT_CURRENCY) -> Plan:
    ccy = normalize_currency(currency)
    amount = PRICE_BOOK.get((tier, period, ccy))
    if amount is None:
        raise PricingError(f"{tier.label} isn't offered {period.value} in {ccy}")
    return Plan(f"{tier.value}_{period.value}", tier, period, ccy, amount)


def plans(currency: str = DEFAULT_CURRENCY) -> list[Plan]:
    """Every plan offered in `currency` (falls back to USD when the currency has no prices)."""
    ccy = normalize_currency(currency)
    if not any(k[2] == ccy for k in PRICE_BOOK):
        ccy = DEFAULT_CURRENCY
    return [plan(t, p, ccy) for (t, p, c) in PRICE_BOOK if c == ccy]


def plan_by_id(plan_id: str, currency: str = DEFAULT_CURRENCY) -> Plan:
    try:
        tier_s, period_s = plan_id.split("_", 1)
        return plan(Tier(tier_s), Period(period_s), currency)
    except ValueError as exc:
        raise PricingError(f"Unknown plan '{plan_id}'") from exc


def annual_savings_minor(tier: Tier, currency: str = DEFAULT_CURRENCY) -> int | None:
    """What paying yearly saves over twelve monthly payments (None if either isn't offered)."""
    try:
        monthly, annual = plan(tier, Period.MONTHLY, currency), plan(tier, Period.ANNUAL, currency)
    except PricingError:
        return None
    return monthly.amount_minor * 12 - annual.amount_minor


def annual_savings_bp(tier: Tier, currency: str = DEFAULT_CURRENCY) -> int | None:
    savings = annual_savings_minor(tier, currency)
    if savings is None:
        return None
    yearly_at_monthly = plan(tier, Period.MONTHLY, currency).amount_minor * 12
    return _div(savings * 10_000, yearly_at_monthly, ROUND_HALF_UP)


def unused_credit_minor(current: Plan, cycle_start: date, cycle_end: date, today: date) -> int:
    """Credit for the unused part of the current billing cycle, by whole days, rounded down.
    `cycle_end` is exclusive (the renewal date)."""
    if cycle_end <= cycle_start:
        raise PricingError("Billing cycle must end after it starts")
    if today < cycle_start:
        raise PricingError("Today is before the billing cycle")
    total = (cycle_end - cycle_start).days
    left = max(0, (cycle_end - today).days)
    return _div(current.amount_minor * left, total, ROUND_FLOOR)


@dataclass(frozen=True)
class Quote:
    plan: Plan
    credit_minor: int
    due_today_minor: int
    renews_on: date
    renewal_minor: int


def add_months(d: date, months: int) -> date:
    """Same day-of-month `months` later, clamped to the month's last day (Jan 31 + 1 → Feb 28/29)."""
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in range(d.day, 27, -1):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return date(year, month, min(d.day, 28))


def quote(target: Plan, today: date, *, current: Plan | None = None, cycle_start: date | None = None,
          cycle_end: date | None = None) -> Quote:
    """What changing to `target` today would cost: a new cycle starts today, and any unused
    time on the current paid plan is credited. Credit never exceeds the new price (no refunds here)."""
    credit = 0
    if current is not None and current.currency != target.currency:
        raise PricingError("Can't prorate across currencies")
    if current is not None and cycle_start is not None and cycle_end is not None:
        credit = min(unused_credit_minor(current, cycle_start, cycle_end, today), target.amount_minor)
    return Quote(target, credit, target.amount_minor - credit, add_months(today, target.period.months),
                 target.amount_minor)


def format_price(amount_minor: int, currency: str) -> str:
    """Plain display (no locale): 699 USD → '6.99', 6000 → '60', JPY 700 → '700'."""
    exp = minor_units(currency)
    value = Decimal(amount_minor).scaleb(-exp)
    text = f"{value:.{exp}f}" if exp else f"{value:.0f}"
    if exp and amount_minor % (10 ** exp) == 0:
        text = text.split(".")[0]
    return text
