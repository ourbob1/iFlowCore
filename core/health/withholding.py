"""
Fractional premium withholding, driven by the ingested earnings stream.

Every earning row in `transactions` (statement import, manual entry) is a
payout. `sync_withholdings` walks payouts in time order and moves a slice of
each into the premium reserve: one `health_premium_entries` row per (payout,
plan), `source = 'auto'`, linked by `transaction_id`. Re-running is safe: the
unique index makes each payout count once. Payouts that needed nothing (the
premium was already covered) are re-checked on later syncs, so a plan added
later is funded from the same cash flow, as of each payout's own date. If a
payout is later deleted, its withholding is reversed on the next sync.

Strategies (WithholdingPolicy.mode)
-----------------------------------
  auto        the rate needed *right now* to fund the next premiums by the
              earliest due date, rounded up:
                need     = premiums − already paid for that month − reserve on hand
                expected = this payout + trailing daily average × days left
                           (average over the last 28 days, or over the days seen so far
                           for a new user; the very first payout has no average)
                rate     = need ÷ expected
              It stops once the premiums are covered (a surplus carries over).
  percent     a fixed share, e.g. 700 bp = 7¢ of every dollar
  per_payout  a fixed amount per payout (e.g. $2.00)
  off         nothing is withheld

Every mode respects `cap_bp` (never more than this share of a payout; 25% by
default) and `excluded_platforms`, and never withholds from zero or negative
rows (refunds and adjustments are never clawed back). Reimbursements aren't
payouts and are skipped.

Multi-plan and multi-platform
-----------------------------
One withholding per payout, split across the active plans by premium share
with lossless integer allocation (a $450 health + $39 dental premium split a
$10.00 withholding 920/80). Payouts from every platform feed the same
reserve; `platform_split` reports what each platform's payouts contributed.

Money is integer minor units throughout; withheld amounts round up so the
reserve never ends a cycle short by rounding.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any, Mapping

from core.financial.money import allocate
from core.guild.premiums import next_due, previous_due, withhold
from core.guild.store import HealthPlan, active_plans
from core.intelligence.data import classify_time, to_base
from core.timeutil import parse_iso

MODES = ("auto", "percent", "per_payout", "off")
DEFAULT_CAP_BP = 2_500
TRAILING_DAYS = 28
BP = 10_000


class WithholdingError(ValueError):
    pass


def _ceil_div(num: int, den: int) -> int:
    return int((Decimal(num) / Decimal(den)).to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class WithholdingPolicy:
    mode: str = "auto"
    rate_bp: int = 0                              # percent mode
    per_payout_minor: int = 0                     # per_payout mode
    cap_bp: int = DEFAULT_CAP_BP
    excluded_platforms: tuple[str, ...] = ()
    start: date | None = None                     # payouts on/after this local date

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise WithholdingError(f"mode must be one of {MODES}")
        for name in ("rate_bp", "per_payout_minor", "cap_bp"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise WithholdingError(f"{name} must be a non-negative integer")
        if not 0 < self.cap_bp <= BP:
            raise WithholdingError("cap must be between 0.01% and 100%")
        if self.rate_bp > self.cap_bp:
            raise WithholdingError("rate can't exceed the cap")
        if self.mode == "percent" and self.rate_bp == 0:
            raise WithholdingError("percent mode needs a rate")
        if self.mode == "per_payout" and self.per_payout_minor == 0:
            raise WithholdingError("per-payout mode needs an amount")

    def to_dict(self) -> dict:
        return {"mode": self.mode, "rate_bp": self.rate_bp, "per_payout_minor": self.per_payout_minor,
                "cap_bp": self.cap_bp, "excluded_platforms": list(self.excluded_platforms),
                "start": self.start.isoformat() if self.start else None}

    @classmethod
    def from_dict(cls, d: Mapping | None) -> "WithholdingPolicy":
        if not d:
            return cls()
        start = d.get("start")
        return cls(mode=d.get("mode", "auto"), rate_bp=int(d.get("rate_bp", 0)),
                   per_payout_minor=int(d.get("per_payout_minor", 0)), cap_bp=int(d.get("cap_bp", DEFAULT_CAP_BP)),
                   excluded_platforms=tuple(d.get("excluded_platforms") or ()),
                   start=date.fromisoformat(start) if start else None)


@dataclass(frozen=True)
class Payout:
    transaction_id: str
    day: date                                      # local date
    platform_id: str | None
    net_minor: int                                 # base currency, after platform fees


@dataclass(frozen=True)
class Decision:
    rate_bp: int
    amount_minor: int
    reason: str                                    # withheld | covered | excluded | not_a_payout | off | capped


def decide(payout: Payout, policy: WithholdingPolicy, *, need_minor: int, expected_minor: int) -> Decision:
    """How much of one payout to withhold. Pure function of its inputs."""
    if payout.net_minor <= 0:
        return Decision(0, 0, "not_a_payout")
    if policy.mode == "off":
        return Decision(0, 0, "off")
    if payout.platform_id and payout.platform_id in policy.excluded_platforms:
        return Decision(0, 0, "excluded")
    cap_amount = withhold(payout.net_minor, policy.cap_bp)
    if policy.mode == "percent":
        rate = policy.rate_bp
        amount = withhold(payout.net_minor, rate)
    elif policy.mode == "per_payout":
        amount = policy.per_payout_minor
        rate = min(BP, _ceil_div(amount * BP, payout.net_minor))
    else:                                           # auto
        if need_minor <= 0:
            return Decision(0, 0, "covered")
        expected = max(expected_minor, payout.net_minor)
        rate = min(policy.cap_bp, _ceil_div(need_minor * BP, expected))
        amount = min(withhold(payout.net_minor, rate), need_minor)
    reason = "withheld"
    if amount > cap_amount:
        amount, rate, reason = cap_amount, policy.cap_bp, "capped"
    return Decision(min(rate, policy.cap_bp), min(amount, payout.net_minor), reason)


def split_across_plans(amount_minor: int, plans: list[HealthPlan]) -> list[tuple[str, int]]:
    """Lossless split of one withholding across plans, weighted by monthly premium."""
    if amount_minor <= 0 or not plans:
        return []
    parts = allocate(amount_minor, [p.monthly_premium_minor for p in plans])
    return [(p.id, part) for p, part in zip(plans, parts) if part > 0]


def cycle_for(plans: list[HealthPlan], day: date) -> tuple[date, date]:
    """(previous due, next due) for the earliest upcoming premium among `plans`, as of `day`."""
    due = min(next_due(day, p.due_day) for p in plans)
    return previous_due(due), due


def weekly_need_minor(plans: list[HealthPlan]) -> int:
    """Monthly premiums as a weekly amount (× 12 / 52, rounded up)."""
    return _ceil_div(sum(p.monthly_premium_minor for p in plans) * 12, 52)


class _Ledger:
    """Premium pot as of any day: need = premiums − already paid for the due month − available reserve."""

    def __init__(self, rows: Any) -> None:
        self.by_day: dict[date, int] = {}           # reserves − withdrawals − payments, by entry date
        self.paid_for: dict[str, int] = {}          # payments by covered month
        for r in rows:
            d, amt = date.fromisoformat(r["occurred_on"]), r["amount_minor"]
            self.by_day[d] = self.by_day.get(d, 0) + (amt if r["kind"] == "reserve" else -amt)
            if r["kind"] == "payment" and r["covers_month"]:
                self.paid_for[r["covers_month"]] = self.paid_for.get(r["covers_month"], 0) + amt

    def add_reserve(self, day: date, amount: int) -> None:
        self.by_day[day] = self.by_day.get(day, 0) + amount

    def balance(self, as_of: date) -> int:
        return sum(v for d, v in self.by_day.items() if d <= as_of)

    def need(self, premium_total: int, due: date, as_of: date) -> int:
        paid = self.paid_for.get(due.strftime("%Y-%m"), 0)
        return max(0, premium_total - paid - max(0, self.balance(as_of)))


# --- sync ------------------------------------------------------------------------------------------


@dataclass
class SyncReport:
    processed: int = 0                   # payouts evaluated (those that withheld 0 are re-checked next sync)
    withheld_count: int = 0              # payouts that contributed this sync
    withheld_minor: int = 0
    reversed: int = 0
    by_platform: dict[str | None, int] = field(default_factory=dict)
    reasons: dict[str, int] = field(default_factory=dict)
    skipped_currency: int = 0            # payouts that couldn't be converted to the base currency
    plans_other_currency: int = 0        # plans not in the base currency (not auto-funded)
    last_rate_bp: int | None = None


def _load_payouts(conn: Any, start: date, end: date, *, tz: str, currency: str,
                  report: SyncReport) -> list[Payout]:
    from core.intelligence.data import local_bounds_utc

    lo, hi = local_bounds_utc(start - timedelta(days=1), end + timedelta(days=1), tz)
    rows = conn.execute(
        "SELECT id, occurred_at, gross_amount_minor, platform_fee_minor, currency, base_amount_minor, platform_id "
        "FROM transactions WHERE deleted_at IS NULL AND kind <> 'reimbursement' "
        "AND julianday(occurred_at) >= julianday(?) AND julianday(occurred_at) < julianday(?) "
        "ORDER BY julianday(occurred_at), id", (lo, hi)).fetchall()
    out = []
    for r in rows:
        conv = to_base(r["gross_amount_minor"], r["platform_fee_minor"], r["currency"], r["base_amount_minor"],
                       currency)
        if conv is None:
            report.skipped_currency += 1
            continue
        day, _ = classify_time(r["occurred_at"], parse_iso(r["occurred_at"]), tz)
        if start <= day <= end:
            out.append(Payout(r["id"], day, r["platform_id"], conv[0] - conv[1]))
    out.sort(key=lambda p: (p.day, p.transaction_id))
    return out


def _reverse_orphans(conn: Any) -> int:
    """Auto withholdings whose payout was deleted are reversed (soft-deleted)."""
    cur = conn.execute(
        "UPDATE health_premium_entries SET deleted_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE source = 'auto' AND deleted_at IS NULL AND (transaction_id IS NULL OR transaction_id IN "
        "(SELECT id FROM transactions WHERE deleted_at IS NOT NULL))")
    return cur.rowcount


def sync_withholdings(db: Any, *, policy: WithholdingPolicy, today: date, tz: str, currency: str) -> SyncReport:
    """Withhold from every payout not yet processed (idempotent). Returns what happened."""
    report = SyncReport()
    all_plans = active_plans(db, include_pending=True)
    plans = [p for p in all_plans if p.currency == currency]
    report.plans_other_currency = len(all_plans) - len(plans)
    with db.transaction() as conn:
        report.reversed = _reverse_orphans(conn)
    if not plans or policy.mode == "off":
        return report
    start = policy.start or min(p.coverage_start for p in plans) - timedelta(days=31)
    if start > today:
        return report

    with db.connection() as conn:
        history = _load_payouts(conn, start - timedelta(days=TRAILING_DAYS), today, tz=tz, currency=currency,
                                report=SyncReport())
        done = {r[0] for r in conn.execute(
            "SELECT DISTINCT transaction_id FROM health_premium_entries WHERE source = 'auto' "
            "AND deleted_at IS NULL AND transaction_id IS NOT NULL")}
        plan_ids = [p.id for p in plans]
        marks = ", ".join("?" * len(plan_ids))
        ledger = conn.execute(
            f"SELECT kind, amount_minor, occurred_on, covers_month FROM health_premium_entries "
            f"WHERE deleted_at IS NULL AND plan_id IN ({marks})", plan_ids).fetchall()
    book = _Ledger(ledger)
    premium_total = sum(p.monthly_premium_minor for p in plans)
    new_rows: list[tuple] = []
    daily_net: dict[date, int] = {}
    for p in history:
        daily_net[p.day] = daily_net.get(p.day, 0) + max(0, p.net_minor)
    first_day = min(daily_net) if daily_net else today

    for payout in history:
        if payout.day < start or payout.transaction_id in done:
            continue
        _, due = cycle_for(plans, payout.day)
        need = book.need(premium_total, due, payout.day)
        window_start = max(payout.day - timedelta(days=TRAILING_DAYS), first_day)
        observed = (payout.day - window_start).days       # cold start: average over the days actually seen
        trailing = sum(v for d, v in daily_net.items() if window_start <= d < payout.day)
        days_left = max(0, (due - payout.day).days)
        expected = payout.net_minor + (trailing * days_left // observed if observed else 0)
        decision = decide(payout, policy, need_minor=need, expected_minor=expected)
        report.processed += 1
        report.reasons[decision.reason] = report.reasons.get(decision.reason, 0) + 1
        if decision.amount_minor <= 0:
            continue
        report.last_rate_bp = decision.rate_bp
        report.withheld_count += 1
        report.withheld_minor += decision.amount_minor
        report.by_platform[payout.platform_id] = report.by_platform.get(payout.platform_id, 0) + decision.amount_minor
        book.add_reserve(payout.day, decision.amount_minor)
        for plan_id, part in split_across_plans(decision.amount_minor, plans):
            new_rows.append((str(uuid.uuid4()), plan_id, part, currency, payout.day.isoformat(),
                             payout.transaction_id, decision.rate_bp))

    if new_rows:
        with db.transaction() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO health_premium_entries (id, plan_id, kind, amount_minor, currency, occurred_on, "
                "transaction_id, rate_bp, source, note) VALUES (?, ?, 'reserve', ?, ?, ?, ?, ?, 'auto', "
                "'Automatic withholding')", new_rows)
    return report


# --- reporting --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlatformShare:
    platform_id: str | None
    name: str
    net_minor: int
    withheld_minor: int

    @property
    def effective_bp(self) -> int | None:
        return _ceil_div(self.withheld_minor * BP, self.net_minor) if self.net_minor > 0 else None


def platform_split(db: Any, *, start: date, end: date, tz: str = "UTC", currency: str = "USD") -> list[PlatformShare]:
    """Every platform's payouts between two local dates (inclusive) and what they contributed to the
    reserve. Payouts that needed no withholding (premium already covered) still count toward the net."""
    with db.connection() as conn:
        payouts = _load_payouts(conn, start, end, tz=tz, currency=currency, report=SyncReport())
        names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM platforms")}
        withheld_rows = conn.execute(
            "SELECT transaction_id, sum(amount_minor) AS amt FROM health_premium_entries "
            "WHERE source = 'auto' AND deleted_at IS NULL AND transaction_id IS NOT NULL GROUP BY transaction_id"
        ).fetchall()
    withheld = {r["transaction_id"]: r["amt"] for r in withheld_rows}
    net: dict[str | None, int] = {}
    kept: dict[str | None, int] = {}
    for p in payouts:
        if p.net_minor <= 0:
            continue
        net[p.platform_id] = net.get(p.platform_id, 0) + p.net_minor
        kept[p.platform_id] = kept.get(p.platform_id, 0) + int(withheld.get(p.transaction_id, 0))
    shares = [PlatformShare(pid, names.get(pid, "Other"), net[pid], kept[pid]) for pid in net]
    return sorted(shares, key=lambda s: (s.withheld_minor, s.net_minor), reverse=True)


@dataclass(frozen=True)
class ReserveStatus:
    balance_minor: int                   # all plans' pots together
    due_on: date | None
    premium_due_minor: int               # all active premiums, funded by the earliest due date
    covered_minor: int                   # paid for that month + reserve available
    shortfall_minor: int
    weekly_need_minor: int
    this_week_minor: int                 # withheld since Monday


def reserve_status(db: Any, *, today: date, currency: str) -> ReserveStatus:
    plans = [p for p in active_plans(db, include_pending=True) if p.currency == currency]
    if not plans:
        return ReserveStatus(0, None, 0, 0, 0, 0, 0)
    _, due = cycle_for(plans, today)
    ids = [p.id for p in plans]
    marks = ", ".join("?" * len(ids))
    monday = today - timedelta(days=today.weekday())
    with db.connection() as conn:
        rows = conn.execute(f"SELECT kind, amount_minor, occurred_on, covers_month, source FROM health_premium_entries "
                            f"WHERE deleted_at IS NULL AND plan_id IN ({marks})", ids).fetchall()
    book = _Ledger(rows)
    week = sum(r["amount_minor"] for r in rows if r["source"] == "auto" and r["kind"] == "reserve"
               and monday <= date.fromisoformat(r["occurred_on"]) <= today)
    premium = sum(p.monthly_premium_minor for p in plans)
    balance = book.balance(today)
    shortfall = book.need(premium, due, today)
    return ReserveStatus(balance, due, premium, premium - shortfall, shortfall, weekly_need_minor(plans), week)
