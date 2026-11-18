"""
Dashboard view-model: everything the dashboard shows, computed from the
database as integers. Views only format and lay out.

  * KPIs — YTD gross, spend (platform fees + expenses), cash net profit and
    margin, this month's net vs an optional goal.
  * Tax set-aside — liability from the Phase 2 tax engine (`estimate_for_year`),
    reserves and payments from `tax_set_asides`, and an alert level.
  * Chart — income vs expenses per local calendar month, with "nice" y ticks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from core.config import minor_units
from core.financial.money import allocate
from core.financial.tax import TaxEstimate, TaxEstimator, estimate_for_year
from core.timeutil import local_date
from ui.format import money_compact

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
SOON_DAYS = 14


@dataclass(frozen=True)
class ChartSeries:
    key: str
    label: str
    values: tuple[int, ...]           # minor units, one per category
    color_role: str                   # "series_income" | "series_expense"


@dataclass(frozen=True)
class BarChartData:
    categories: tuple[str, ...]
    series: tuple[ChartSeries, ...]
    y_max_minor: int
    ticks: tuple[tuple[int, str], ...]
    currency: str

    @property
    def empty(self) -> bool:
        return all(v == 0 for s in self.series for v in s.values)

    def table_rows(self) -> list[tuple[str, ...]]:
        """Accessible table view: category + one value per series (minor units)."""
        return [(cat, *(s.values[i] for s in self.series)) for i, cat in enumerate(self.categories)]


@dataclass(frozen=True)
class TaxAlert:
    level: str                        # good | warning | serious | critical
    title: str
    message: str
    next_due: date | None
    amount_due_minor: int
    shortfall_minor: int


@dataclass(frozen=True)
class DashboardData:
    year: int
    currency: str
    gross_minor: int
    spend_minor: int
    net_minor: int
    margin_bp: int
    month_net_minor: int
    goal_minor: int | None
    goal_progress_bp: int | None
    taxable_profit_minor: int
    tax_total_minor: int
    set_aside_rate_bp: int
    reserved_minor: int
    paid_minor: int
    alert: TaxAlert
    chart: BarChartData
    mileage_deduction_minor: int
    business_meters: int
    needs_review: int
    notes: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Chart helpers
# ---------------------------------------------------------------------------


def nice_step(max_minor: int, target_ticks: int = 4) -> int:
    """Smallest 1/2/2.5/5 × 10^k step (minor units) giving ≤ target_ticks intervals."""
    if max_minor <= 0:
        return 100
    raw = -(-max_minor // target_ticks)             # ceil division
    magnitude = 1
    while magnitude * 10 <= raw:
        magnitude *= 10
    candidates = [magnitude, 2 * magnitude, 5 * magnitude, 10 * magnitude]
    if (5 * magnitude) % 2 == 0:
        candidates.append(5 * magnitude // 2)       # 2.5 × 10^k when it's a whole number
    return min(c for c in candidates if c >= raw)


def nice_ticks(max_minor: int, currency: str, target_ticks: int = 4) -> tuple[int, tuple[tuple[int, str], ...]]:
    # Never tick finer than one whole major unit ($1, ¥1).
    step = max(nice_step(max_minor, target_ticks), 10 ** minor_units(currency))
    top = max(step, -(-max_minor // step) * step)
    ticks = tuple((v, money_compact(v, currency)) for v in range(0, top + 1, step))
    return top, ticks


def monthly_income_expense(db: Any, year: int, *, tz: str, currency: str, through_month: int = 12) -> BarChartData:
    income = [0] * 12
    expense = [0] * 12
    lo, hi = f"{year - 1}-12-31", f"{year + 1}-01-02"
    with db.connection() as conn:
        for r in conn.execute(
            "SELECT occurred_at, currency, gross_amount_minor, platform_fee_minor, base_amount_minor FROM transactions "
            "WHERE deleted_at IS NULL AND occurred_at >= ? AND occurred_at < ?", (lo, hi),
        ):
            day = local_date(r["occurred_at"], tz)
            if day.year != year:
                continue
            gross, fee = _to_base(r["gross_amount_minor"], r["platform_fee_minor"], r["currency"], r["base_amount_minor"], currency)
            if gross is None:
                continue
            income[day.month - 1] += gross
            expense[day.month - 1] += fee
        for r in conn.execute(
            "SELECT occurred_at, currency, amount_minor, base_amount_minor FROM expenses "
            "WHERE deleted_at IS NULL AND occurred_at >= ? AND occurred_at < ?", (lo, hi),
        ):
            day = local_date(r["occurred_at"], tz)
            if day.year != year:
                continue
            amount = r["amount_minor"] if r["currency"] == currency else r["base_amount_minor"]
            if amount is not None:
                expense[day.month - 1] += amount
    n = max(1, min(12, through_month))
    top, ticks = nice_ticks(max(income[:n] + expense[:n] + [0]), currency)
    return BarChartData(
        MONTHS[:n],
        (ChartSeries("income", "Income", tuple(income[:n]), "series_income"),
         ChartSeries("expenses", "Expenses", tuple(expense[:n]), "series_expense")),
        top, ticks, currency,
    )


def _to_base(gross: int, fee: int, row_ccy: str, base: int | None, currency: str) -> tuple[int | None, int]:
    if row_ccy == currency:
        return gross, fee
    if base is None or gross == 0:
        return None, 0
    return base, _ratio(fee, base, gross)


def _ratio(value: int, num: int, den: int) -> int:
    from decimal import ROUND_HALF_UP, Decimal

    return int((Decimal(value) * num / den).quantize(Decimal(1), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Tax set-aside
# ---------------------------------------------------------------------------


def set_aside_totals(db: Any, year: int) -> tuple[int, int]:
    """(net reserved, paid) for the year."""
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT kind, sum(amount_minor) FROM tax_set_asides WHERE tax_year = ? AND deleted_at IS NULL GROUP BY kind",
            (year,),
        ).fetchall()
    totals = {r[0]: r[1] for r in rows}
    return totals.get("reserve", 0) - totals.get("withdrawal", 0), totals.get("payment", 0)


def tax_alert(estimate: TaxEstimate, *, reserved: int, paid: int, today: date) -> TaxAlert:
    liability = estimate.total_tax
    shortfall = max(0, liability - reserved - paid)
    quarters = allocate(liability, [1, 1, 1, 1]) if liability > 0 else [0, 0, 0, 0]
    dues = estimate.quarterly_due_dates

    past = [i for i, d in enumerate(dues) if d < today]
    required_past = sum(quarters[: len(past)])
    if past and paid < required_past:
        missed = required_past - paid
        return TaxAlert("critical", "Estimated payment behind",
                        f"Payments through Q{len(past)} are short. Pay now to limit IRS underpayment penalties.",
                        dues[len(past)] if len(past) < 4 else None, missed, shortfall)

    upcoming = next((i for i, d in enumerate(dues) if d >= today), None)
    if upcoming is None:
        due_amount, next_due = 0, None
    else:
        due_amount = max(0, sum(quarters[: upcoming + 1]) - paid)
        next_due = dues[upcoming]
    if next_due and due_amount and (next_due - today).days <= SOON_DAYS:
        return TaxAlert("serious", f"Q{upcoming + 1} estimated tax due soon",
                        "Your next estimated payment is coming up.", next_due, due_amount, shortfall)
    if shortfall:
        return TaxAlert("warning", "Set aside more for taxes",
                        "Your tax reserve is below the estimate for income so far.", next_due, due_amount, shortfall)
    return TaxAlert("good", "Taxes covered", "Your reserve covers the estimated tax so far.",
                    next_due, due_amount, 0)


def record_set_aside(db: Any, *, year: int, kind: str, amount_minor: int, on: date, currency: str = "USD",
                     note: str | None = None) -> str:
    import uuid

    if kind not in ("reserve", "payment", "withdrawal"):
        raise ValueError("kind must be reserve, payment or withdrawal")
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
        raise ValueError("amount_minor must be a positive int")
    new_id = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO tax_set_asides (id, tax_year, kind, amount_minor, currency, occurred_on, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (new_id, year, kind, amount_minor, currency.upper(), on.isoformat(), note))
    return new_id


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def load_dashboard(
    db: Any,
    *,
    today: date,
    tz: str,
    currency: str,
    goal_minor: int | None = None,
    estimator: TaxEstimator | None = None,
) -> DashboardData:
    year = today.year
    chart = monthly_income_expense(db, year, tz=tz, currency=currency, through_month=today.month)
    income = next(s for s in chart.series if s.key == "income").values
    spend = next(s for s in chart.series if s.key == "expenses").values
    gross, spent = sum(income), sum(spend)
    net = gross - spent
    month_net = income[today.month - 1] - spend[today.month - 1]

    estimate, summary = estimate_for_year(db, year, base_currency=currency, estimator=estimator, timezone=tz)
    reserved, paid = set_aside_totals(db, year)
    taxable = estimate.net_profit
    rate_bp = estimate.total_tax * 10_000 // taxable if taxable > 0 else 0

    with db.connection() as conn:
        review = conn.execute(
            "SELECT count(*) FROM mileage_logs WHERE review_status = 'needs_review' AND deleted_at IS NULL"
        ).fetchone()[0]

    goal_progress = None
    if goal_minor and goal_minor > 0:
        goal_progress = max(0, month_net) * 10_000 // goal_minor

    return DashboardData(
        year=year, currency=currency, gross_minor=gross, spend_minor=spent, net_minor=net,
        margin_bp=net * 10_000 // gross if gross > 0 else 0,
        month_net_minor=month_net, goal_minor=goal_minor, goal_progress_bp=goal_progress,
        taxable_profit_minor=taxable, tax_total_minor=estimate.total_tax, set_aside_rate_bp=rate_bp,
        reserved_minor=reserved, paid_minor=paid,
        alert=tax_alert(estimate, reserved=reserved, paid=paid, today=today),
        chart=chart, mileage_deduction_minor=summary.mileage_deduction, business_meters=summary.business_meters,
        needs_review=review, notes=tuple(estimate.provisional) + tuple(summary.warnings),
    )
