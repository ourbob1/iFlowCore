"""Phase 7 iFlow Health & Guild: premium withholding math (integer) and AHP pool tracking."""

from __future__ import annotations

from datetime import date

import pytest

from core.guild import (
    CombinedWithholding,
    PremiumError,
    active_plan,
    cycle_status,
    monthly_from_window,
    pool_summary,
    record_entry,
    save_plan,
    withhold,
    withholding_rate_bp,
)
from core.guild.premiums import months_covered, next_due, previous_due

TODAY = date(2026, 9, 27)


# --- math -----------------------------------------------------------------------------------------


def test_withholding_rate_rounds_up_and_caps():
    assert withholding_rate_bp(45_000, 300_000) == 1500              # $450 of $3,000 = 15%
    assert withholding_rate_bp(45_000, 700_000) == 643               # 6.4286% → 6.43% (up, never short)
    assert withholding_rate_bp(45_000, 20_000) == 10_000             # premium above income: capped at 100%
    assert withholding_rate_bp(45_000, 0) is None and withholding_rate_bp(45_000, -5) is None
    for bad in (0, -1, 4.5, True):
        with pytest.raises(PremiumError):
            withholding_rate_bp(bad, 100_000)


def test_rate_covers_the_premium_over_a_month_of_payouts():
    premium, monthly = 45_000, 700_000
    rate = withholding_rate_bp(premium, monthly)
    payouts = [monthly // 20] * 20                                   # 20 equal payouts
    assert sum(withhold(p, rate) for p in payouts) >= premium


def test_withhold_per_payout():
    assert withhold(10_000, 643) == 643
    assert withhold(1, 643) == 1                                     # rounds up to a cent
    assert withhold(999, 10_000) == 999 and withhold(0, 500) == 0 and withhold(-50, 500) == 0
    with pytest.raises(PremiumError):
        withhold(100, 10_001)


def test_monthly_from_window():
    assert monthly_from_window(900_000, 90) == 304_167               # 900000 × 365 / 1080
    assert monthly_from_window(0, 30) == 0
    with pytest.raises(PremiumError):
        monthly_from_window(1, 0)


@pytest.mark.parametrize("today,due_day,expected", [
    (date(2026, 9, 27), 1, date(2026, 10, 1)), (date(2026, 9, 1), 1, date(2026, 9, 1)),
    (date(2026, 12, 20), 15, date(2027, 1, 15)), (date(2026, 2, 28), 28, date(2026, 2, 28)),
])
def test_next_due(today, due_day, expected):
    assert next_due(today, due_day) == expected


def test_previous_due_and_bad_day():
    assert previous_due(date(2027, 1, 15)) == date(2026, 12, 15)
    with pytest.raises(PremiumError):
        next_due(TODAY, 31)


@pytest.mark.parametrize("reserved,paid,days_to,level,shortfall", [
    (45_000, 0, 4, "good", 0), (0, 45_000, 4, "good", 0), (20_000, 0, 20, "warning", 25_000),
    (20_000, 0, 6, "serious", 25_000), (20_000, 0, 2, "critical", 25_000), (-5_000, 0, 20, "warning", 45_000),
])
def test_cycle_status(reserved, paid, days_to, level, shortfall):
    due_day = 1
    today = date(2026, 10, 1).fromordinal(date(2026, 10, 1).toordinal() - days_to)
    st = cycle_status(premium_minor=45_000, due_day=due_day, today=today, reserved_minor=reserved, paid_minor=paid)
    assert (st.level, st.shortfall_minor, st.days_left, st.due_on) == (level, shortfall, days_to, date(2026, 10, 1))
    if shortfall:
        weeks = -(-days_to // 7)
        assert st.per_week_minor * weeks >= shortfall                 # per-week plan closes the gap


def test_combined_split_always_adds_up():
    split = CombinedWithholding(tax_bp=2250, health_bp=643)
    for payout in (1, 3, 999, 10_000, 123_457):
        tax, health, mine = split.split(payout)
        assert tax + health + mine == payout and min(tax, health, mine) >= 0
    assert split.total_bp == 2893
    # over 100% combined: health is taken first, tax gets what's left, never a negative take-home
    assert CombinedWithholding(9000, 5000).split(100) == (50, 50, 0)


def test_months_covered():
    paid, missing = months_covered({"2026-07", "2026-09"}, date(2026, 7, 15), TODAY)
    assert paid == 2 and missing == ["2026-08"]
    paid, missing = months_covered(set(), date(2026, 12, 1), date(2027, 1, 5))
    assert paid == 0 and missing == ["2026-12", "2027-01"]


# --- storage ----------------------------------------------------------------------------------------


def plan_id(db, **kw):
    args = dict(association="Freelancers Guild", plan_name="Silver PPO", monthly_premium_minor=45_000,
                due_day=1, coverage_start=date(2026, 7, 1))
    args.update(kw)
    return save_plan(db, **args)


def test_save_plan_validation(db):
    for bad in ({"monthly_premium_minor": 0}, {"monthly_premium_minor": 450.0}, {"due_day": 29},
                {"association": " "}, {"withholding_bp": 0}, {"currency": "XYZ"}):
        with pytest.raises((PremiumError, ValueError)):
            plan_id(db, **bad)


def test_single_active_plan_and_edit(db):
    first = plan_id(db)
    second = plan_id(db, plan_name="Gold HMO", monthly_premium_minor=52_000)
    assert active_plan(db).id == second and active_plan(db).plan_name == "Gold HMO"
    plan_id(db, plan_id=second, plan_name="Gold HMO+", monthly_premium_minor=53_000, withholding_bp=800)
    p = active_plan(db)
    assert (p.id, p.plan_name, p.monthly_premium_minor, p.withholding_bp) == (second, "Gold HMO+", 53_000, 800)
    with db.connection() as conn:
        assert conn.execute("SELECT is_active FROM health_plans WHERE id = ?", (first,)).fetchone()[0] == 0


def test_record_entry_validation(db):
    pid = plan_id(db)
    for kind, amount in (("bonus", 100), ("reserve", 0), ("reserve", -5), ("reserve", 12.5)):
        with pytest.raises(PremiumError):
            record_entry(db, pid, kind=kind, amount_minor=amount, on=TODAY)
    with pytest.raises(PremiumError):
        record_entry(db, "00000000-0000-4000-8000-000000000000", kind="reserve", amount_minor=100, on=TODAY)
    with pytest.raises(PremiumError):
        record_entry(db, pid, kind="payment", amount_minor=100, on=TODAY, covers_month="Sept")
    record_entry(db, pid, kind="payment", amount_minor=45_000, on=TODAY)                  # month defaults to today's
    with db.connection() as conn:
        assert conn.execute("SELECT covers_month FROM health_premium_entries").fetchone()[0] == "2026-09"


def test_database_constraints(db):
    pid = plan_id(db)
    with pytest.raises(Exception, match="CHECK constraint"):             # sqlite3 / sqlcipher3 IntegrityError
        with db.transaction() as conn:
            conn.execute("INSERT INTO health_premium_entries (plan_id, kind, amount_minor, occurred_on) "
                         "VALUES (?, 'payment', 100, '2026-09-01')", (pid,))          # payment without a month


def test_pool_summary(db):
    pid = plan_id(db)
    for m, on in (("2026-07", date(2026, 7, 1)), ("2026-08", date(2026, 8, 1))):
        record_entry(db, pid, kind="reserve", amount_minor=45_000, on=on)
        record_entry(db, pid, kind="payment", amount_minor=45_000, on=on, covers_month=m)
    record_entry(db, pid, kind="reserve", amount_minor=30_000, on=date(2026, 9, 20))
    record_entry(db, pid, kind="withdrawal", amount_minor=5_000, on=date(2026, 9, 21))
    s = pool_summary(db, active_plan(db), TODAY)
    assert s.balance_minor == 25_000 and s.paid_total_minor == 90_000 and s.paid_ytd_minor == 90_000
    assert s.months_paid == 2 and s.months_missing == ("2026-09",)
    c = s.cycle
    assert (c.due_on, c.shortfall_minor, c.level) == (date(2026, 10, 1), 20_000, "serious")   # 4 days left


def test_partial_payments_count_once_the_month_is_full(db):
    pid = plan_id(db, coverage_start=date(2026, 9, 1))
    record_entry(db, pid, kind="payment", amount_minor=20_000, on=date(2026, 9, 1), covers_month="2026-09")
    assert pool_summary(db, active_plan(db), TODAY).months_paid == 0
    record_entry(db, pid, kind="payment", amount_minor=25_000, on=date(2026, 9, 5), covers_month="2026-09")
    assert pool_summary(db, active_plan(db), TODAY).months_paid == 1


def test_health_rows_follow_sync_conventions(db):
    pid = plan_id(db)
    with db.transaction() as conn:
        conn.execute("UPDATE health_plans SET plan_name = 'Renamed' WHERE id = ?", (pid,))
    with db.connection() as conn:
        row = conn.execute("SELECT version, sync_status FROM health_plans WHERE id = ?", (pid,)).fetchone()
    assert tuple(row) == (2, "pending")
    from ui.state import SYNCED_TABLES

    assert {"health_plans", "health_premium_entries"} <= set(SYNCED_TABLES)
