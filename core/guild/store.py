"""
AHP pool tracking: the user's health plan and its premium reserve ledger.

Ledger semantics (health_premium_entries):
  reserve     money moved into the premium pot (e.g. the withheld share of payouts)
  withdrawal  money taken back out of the pot
  payment     a premium paid to the association pool, for one month (covers_month)

  pot balance = reserves − withdrawals − payments

A premium paid straight from a checking account can be recorded as a payment
without a reserve first; the balance may then go negative, which the status
treats as zero available.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from core.config import normalize_currency
from core.guild.premiums import CycleStatus, PremiumError, cycle_status, months_covered, next_due

KINDS = ("reserve", "payment", "withdrawal")
LINES = ("health", "dental", "life")
COVERAGE_TIERS = ("self", "self_spouse", "self_children", "family")
STATUSES = ("pending", "active", "ended")


@dataclass(frozen=True)
class HealthPlan:
    id: str
    association: str
    plan_name: str
    monthly_premium_minor: int
    currency: str
    due_day: int
    members_covered: int
    coverage_start: date
    coverage_end: date | None
    withholding_bp: int | None
    line: str = "health"
    carrier: str | None = None
    broker_id: str | None = None
    offer_id: str | None = None
    coverage_tier: str = "self"
    status: str = "active"
    enrollment_ref: str | None = None


def _row_to_plan(r: Any) -> HealthPlan:
    return HealthPlan(r["id"], r["association"], r["plan_name"], r["monthly_premium_minor"], r["currency"],
                      r["due_day"], r["members_covered"], date.fromisoformat(r["coverage_start"]),
                      date.fromisoformat(r["coverage_end"]) if r["coverage_end"] else None, r["withholding_bp"],
                      r["line"], r["carrier"], r["broker_id"], r["offer_id"], r["coverage_tier"], r["status"],
                      r["enrollment_ref"])


def save_plan(db: Any, *, association: str, plan_name: str, monthly_premium_minor: int, currency: str = "USD",
              due_day: int = 1, members_covered: int = 1, coverage_start: date, coverage_end: date | None = None,
              withholding_bp: int | None = None, plan_id: str | None = None, line: str = "health",
              carrier: str | None = None, broker_id: str | None = None, offer_id: str | None = None,
              coverage_tier: str = "self", status: str = "active", enrollment_ref: str | None = None) -> str:
    """Create or update a plan. One plan per coverage line is active at a time (a new health plan
    replaces the old health plan; dental and life are separate lines)."""
    if line not in LINES:
        raise PremiumError(f"line must be one of {LINES}")
    if coverage_tier not in COVERAGE_TIERS:
        raise PremiumError(f"coverage tier must be one of {COVERAGE_TIERS}")
    if status not in STATUSES:
        raise PremiumError(f"status must be one of {STATUSES}")
    if isinstance(monthly_premium_minor, bool) or not isinstance(monthly_premium_minor, int) or monthly_premium_minor <= 0:
        raise PremiumError("Monthly premium must be a positive integer in minor units")
    if not association.strip() or not plan_name.strip():
        raise PremiumError("Association and plan name are required")
    if not 1 <= due_day <= 28:
        raise PremiumError("Due day must be 1–28 (every month has one)")
    if withholding_bp is not None and not 1 <= withholding_bp <= 10_000:
        raise PremiumError("Withholding must be between 0.01% and 100%")
    ccy = normalize_currency(currency)
    values = (association.strip(), plan_name.strip(), monthly_premium_minor, ccy, due_day, members_covered,
              coverage_start.isoformat(), coverage_end.isoformat() if coverage_end else None, withholding_bp,
              line, carrier, broker_id, offer_id, coverage_tier, status, enrollment_ref)
    cols = ("association, plan_name, monthly_premium_minor, currency, due_day, members_covered, coverage_start, "
            "coverage_end, withholding_bp, line, carrier, broker_id, offer_id, coverage_tier, status, enrollment_ref")
    with db.transaction() as conn:
        if plan_id:
            sets = ", ".join(f"{c.strip()} = ?" for c in cols.split(","))
            conn.execute(f"UPDATE health_plans SET {sets} WHERE id = ? AND deleted_at IS NULL", (*values, plan_id))
            return plan_id
        conn.execute("UPDATE health_plans SET is_active = 0 WHERE is_active = 1 AND line = ? AND deleted_at IS NULL",
                     (line,))
        new_id = str(uuid.uuid4())
        conn.execute(f"INSERT INTO health_plans (id, {cols}) VALUES ({', '.join('?' * 17)})", (new_id, *values))
        return new_id


def active_plans(db: Any, *, include_pending: bool = False) -> list[HealthPlan]:
    """Current plan per line (health, dental, life order)."""
    statuses = ("active", "pending") if include_pending else ("active",)
    with db.connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM health_plans WHERE is_active = 1 AND deleted_at IS NULL "
            f"AND status IN ({', '.join('?' * len(statuses))}) ORDER BY created_at DESC", statuses).fetchall()
    by_line: dict[str, HealthPlan] = {}
    for r in rows:
        by_line.setdefault(r["line"], _row_to_plan(r))
    return [by_line[line] for line in LINES if line in by_line]


def active_plan(db: Any, line: str = "health") -> HealthPlan | None:
    return next((p for p in active_plans(db) if p.line == line), None)


def end_plan(db: Any, plan_id: str, *, on: date) -> None:
    """Coverage ended (cancelled / replaced). History and the pot stay; the plan stops needing premiums."""
    with db.transaction() as conn:
        conn.execute("UPDATE health_plans SET status = 'ended', is_active = 0, coverage_end = ? "
                     "WHERE id = ? AND deleted_at IS NULL", (on.isoformat(), plan_id))


def record_entry(db: Any, plan_id: str, *, kind: str, amount_minor: int, on: date, covers_month: str | None = None,
                 note: str | None = None, transaction_id: str | None = None, rate_bp: int | None = None,
                 source: str = "manual") -> str:
    if kind not in KINDS:
        raise PremiumError(f"kind must be one of {KINDS}")
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
        raise PremiumError("Amount must be a positive integer in minor units")
    if kind == "payment":
        covers_month = covers_month or on.strftime("%Y-%m")
        try:
            date.fromisoformat(covers_month + "-01")
        except ValueError as exc:
            raise PremiumError("covers_month must look like 2026-09") from exc
    entry_id = str(uuid.uuid4())
    with db.transaction() as conn:
        plan = conn.execute("SELECT currency FROM health_plans WHERE id = ? AND deleted_at IS NULL",
                            (plan_id,)).fetchone()
        if plan is None:
            raise PremiumError("No such health plan")
        conn.execute("INSERT INTO health_premium_entries (id, plan_id, kind, amount_minor, currency, occurred_on, "
                     "covers_month, note, transaction_id, rate_bp, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (entry_id, plan_id, kind, amount_minor, plan["currency"], on.isoformat(),
                      covers_month if kind == "payment" else None, note, transaction_id, rate_bp, source))
    return entry_id


@dataclass(frozen=True)
class PoolSummary:
    plan: HealthPlan
    balance_minor: int                 # pot: reserves − withdrawals − payments
    reserved_total_minor: int
    paid_total_minor: int
    paid_ytd_minor: int
    months_paid: int
    months_missing: tuple[str, ...]
    cycle: CycleStatus


def pool_summary(db: Any, plan: HealthPlan, today: date) -> PoolSummary:
    with db.connection() as conn:
        rows = conn.execute("SELECT kind, amount_minor, occurred_on, covers_month FROM health_premium_entries "
                            "WHERE plan_id = ? AND deleted_at IS NULL", (plan.id,)).fetchall()
    reserves = sum(r["amount_minor"] for r in rows if r["kind"] == "reserve")
    withdrawals = sum(r["amount_minor"] for r in rows if r["kind"] == "withdrawal")
    payments = [r for r in rows if r["kind"] == "payment"]
    paid_total = sum(r["amount_minor"] for r in payments)
    paid_ytd = sum(r["amount_minor"] for r in payments if r["occurred_on"][:4] == str(today.year))
    by_month: dict[str, int] = {}
    for r in payments:
        by_month[r["covers_month"]] = by_month.get(r["covers_month"], 0) + r["amount_minor"]
    fully_paid = {m for m, amt in by_month.items() if amt >= plan.monthly_premium_minor}
    last = min(today, plan.coverage_end) if plan.coverage_end else today
    paid_n, missing = months_covered(fully_paid, plan.coverage_start, last) if plan.coverage_start <= last else (0, [])
    due = next_due(today, plan.due_day)
    balance = reserves - withdrawals - paid_total
    cycle = cycle_status(premium_minor=plan.monthly_premium_minor, due_day=plan.due_day, today=today,
                         reserved_minor=balance, paid_minor=by_month.get(due.strftime("%Y-%m"), 0))
    return PoolSummary(plan, balance, reserves, paid_total, paid_ytd, paid_n, tuple(missing), cycle)
