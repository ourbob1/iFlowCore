"""Dashboard view-model math, chart data formatting, and the rendered dashboard component tree."""

from __future__ import annotations

from datetime import date

import flet as ft
import flet_charts as fch
import pytest

from core.financial.tax import TaxEstimator, TaxInputs, TaxTableRegistry
from ui.components.charts import chart_table, income_expense_chart, legend, to_chart_units
from ui.layout import layout_for
from ui.theme import DARK, LIGHT
from ui.viewmodels.dashboard import (
    BarChartData,
    ChartSeries,
    load_dashboard,
    monthly_income_expense,
    nice_step,
    nice_ticks,
    record_set_aside,
    set_aside_totals,
    tax_alert,
)

from .conftest import new_id
from .ui_helpers import find_key, of_type, texts

D = 100


def add_txn(conn, pid, gross, fee, when, currency="USD", base=None):
    conn.execute("INSERT INTO transactions (id, platform_id, gross_amount_minor, platform_fee_minor, currency, "
                 "base_amount_minor, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (new_id(), pid, gross, fee, currency, base, when))


# --- ticks ---------------------------------------------------------------------------------


@pytest.mark.parametrize("max_minor,step", [(0, 100), (95, 25), (999, 250), (4_120, 2_000), (12_345, 5_000),
                                            (98_000, 25_000), (250_001, 100_000), (1_000_000, 250_000)])
def test_nice_step(max_minor, step):
    assert nice_step(max_minor) == step


def test_ticks_cover_max_with_whole_units():
    top, ticks = nice_ticks(98_000, "USD")
    assert top == 100_000 and [v for v, _ in ticks] == [0, 25_000, 50_000, 75_000, 100_000]
    assert [lbl for _, lbl in ticks] == ["$0", "$250", "$500", "$750", "$1k"]
    top, ticks = nice_ticks(95, "USD")                   # never finer than $1
    assert ticks[1][0] == 100
    assert nice_ticks(45_000, "JPY")[1][-1] == (60_000, "¥60k")          # step ¥20k


# --- monthly buckets -------------------------------------------------------------------------


def test_monthly_buckets_use_local_months_and_base_currency(db, platform_id):
    with db.transaction() as conn:
        add_txn(conn, platform_id, 10_000, 2_000, "2026-02-01T03:30:00Z")          # Jan 31, 10:30 pm EST
        add_txn(conn, platform_id, 5_000, 0, "2026-02-15T12:00:00Z")
        add_txn(conn, platform_id, 1_000, 100, "2026-03-05T12:00:00Z", "EUR", 1_100)  # pinned base amount
        add_txn(conn, platform_id, 9_999, 0, "2026-03-06T12:00:00Z", "GBP")        # unconvertible → skipped
        add_txn(conn, platform_id, 7_777, 0, "2025-12-31T12:00:00Z")               # other year
        conn.execute("INSERT INTO expenses (category, amount_minor, currency, occurred_at) "
                     "VALUES ('phone', 4_000, 'USD', '2026-02-10')".replace("4_000", "4000"))
    chart = monthly_income_expense(db, 2026, tz="America/New_York", currency="USD", through_month=3)
    income = dict(zip(chart.categories, chart.series[0].values))
    expense = dict(zip(chart.categories, chart.series[1].values))
    assert income == {"Jan": 10_000, "Feb": 5_000, "Mar": 1_100}
    assert expense == {"Jan": 2_000, "Feb": 4_000, "Mar": 110}               # fees + expenses (FX fee scaled)
    assert chart.categories == ("Jan", "Feb", "Mar") and chart.y_max_minor >= 10_000
    assert chart.table_rows()[0] == ("Jan", 10_000, 2_000)
    utc = monthly_income_expense(db, 2026, tz="UTC", currency="USD", through_month=2)
    assert utc.series[0].values == (0, 15_000)


# --- tax set-aside alert -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def estimate():
    return TaxEstimator(TaxTableRegistry.load()).estimate(TaxInputs(2026, gross_receipts=40_000 * D))


def test_alert_levels(estimate):
    total = estimate.total_tax
    q = total // 4
    # Before any due date, fully reserved → good.
    assert tax_alert(estimate, reserved=total, paid=0, today=date(2026, 3, 1)).level == "good"
    # Not enough reserved, next due far away → warning.
    warn = tax_alert(estimate, reserved=0, paid=0, today=date(2026, 3, 1))
    assert warn.level == "warning" and warn.shortfall_minor == total and warn.next_due == date(2026, 4, 15)
    # Within 14 days of a due date with money owed → serious, amount = that quarter.
    soon = tax_alert(estimate, reserved=total, paid=0, today=date(2026, 4, 5))
    assert soon.level == "serious" and soon.amount_due_minor in (q, q + 1)
    # A due date passed without the payment → critical.
    late = tax_alert(estimate, reserved=total, paid=0, today=date(2026, 4, 20))
    assert late.level == "critical" and late.amount_due_minor in (q, q + 1)
    # Paid through Q1 → no longer critical.
    assert tax_alert(estimate, reserved=total, paid=q + 1, today=date(2026, 4, 20)).level != "critical"


def test_set_aside_records(db):
    record_set_aside(db, year=2026, kind="reserve", amount_minor=50_000, on=date(2026, 3, 1))
    record_set_aside(db, year=2026, kind="withdrawal", amount_minor=10_000, on=date(2026, 3, 2))
    record_set_aside(db, year=2026, kind="payment", amount_minor=30_000, on=date(2026, 4, 14))
    assert set_aside_totals(db, 2026) == (40_000, 30_000)
    for kind, amount in (("gift", 1), ("reserve", 0), ("reserve", 12.5)):
        with pytest.raises(ValueError):
            record_set_aside(db, year=2026, kind=kind, amount_minor=amount, on=date(2026, 3, 1))  # type: ignore[arg-type]


def test_load_dashboard(db, platform_id):
    with db.transaction() as conn:
        for m in range(1, 10):
            add_txn(conn, platform_id, 400_000, 80_000, f"2026-{m:02d}-10T12:00:00Z")
    record_set_aside(db, year=2026, kind="payment", amount_minor=100_000, on=date(2026, 4, 10))
    d = load_dashboard(db, today=date(2026, 9, 26), tz="America/New_York", currency="USD", goal_minor=400_000)
    assert d.gross_minor == 3_600_000 and d.spend_minor == 720_000 and d.net_minor == 2_880_000
    assert d.margin_bp == 8_000 and d.month_net_minor == 320_000 and d.goal_progress_bp == 8_000
    assert d.taxable_profit_minor == 2_880_000
    assert d.set_aside_rate_bp == d.tax_total_minor * 10_000 // 2_880_000
    assert d.alert.level == "critical"                                    # Q1–Q3 underpaid
    assert len(d.chart.categories) == 9


# --- chart component ----------------------------------------------------------------------------

DATA = BarChartData(
    ("Jan", "Feb"),
    (ChartSeries("income", "Income", (123_456, 50_000), "series_income"),
     ChartSeries("expenses", "Expenses", (20_000, 0), "series_expense")),
    150_000, ((0, "$0"), (50_000, "$500"), (100_000, "$1k"), (150_000, "$1.5k")), "USD",
)


def test_chart_geometry_and_tooltips():
    chart = income_expense_chart(DATA, LIGHT, layout_for(1280, "linux"))
    assert isinstance(chart, fch.BarChart) and chart.max_y == 1_500.0 and chart.min_y == 0
    rods = [r for g in chart.groups for r in g.rods]
    assert [r.to_y for r in rods] == [1234.56, 200.0, 500.0, 0.0]
    assert rods[0].tooltip == "Jan income: $1,234.56"                    # exact, from integers
    assert rods[0].color == LIGHT.series_income and rods[1].color == LIGHT.series_expense
    assert all(r.width <= 20 for r in rods)
    assert all(g.spacing == 2 for g in chart.groups)                     # 2 px surface gap
    assert chart.horizontal_grid_lines.interval == 500.0 and chart.horizontal_grid_lines.width == 1
    assert [lbl.value for lbl in chart.left_axis.labels] == [0.0, 500.0, 1000.0, 1500.0]
    dark = income_expense_chart(DATA, DARK, layout_for(390, "android"))
    assert dark.groups[0].rods[0].color == DARK.series_income and dark.height == 220


def test_legend_and_table_view():
    assert texts(legend(DATA, LIGHT)) == ["Income", "Expenses"]
    table = chart_table(DATA, LIGHT)
    assert isinstance(table, ft.DataTable) and len(table.rows) == 2
    assert texts(table.rows[0]) == ["Jan", "$1,234.56", "$200.00"]


def test_to_chart_units_respects_currency_exponent():
    assert to_chart_units(1200, "JPY") == 1200.0
    assert to_chart_units(1234, "KWD") == 1.234


# --- rendered dashboard ----------------------------------------------------------------------------


def test_dashboard_render_tree(db, platform_id):
    from ui.views import dashboard
    from ui.views.base import ViewContext
    from ui.state import AppState
    from core.config import load_config

    with db.transaction() as conn:
        add_txn(conn, platform_id, 400_000, 80_000, "2026-09-10T12:00:00Z")
    state = AppState(load_config({"IFLOW_DATA_DIR": "/tmp"}))
    state.attach(db)
    data = load_dashboard(db, today=date(2026, 9, 26), tz="UTC", currency="USD", goal_minor=None)
    for width in (390, 1400):
        ctx = ViewContext(state, layout_for(width, "linux"), LIGHT, lambda p: None, lambda: None, lambda m: None,
                          lambda d: None, lambda: None)
        tree = dashboard.render(ctx, data)
        for key in ("tax-alert", "kpi-net", "kpi-tax-rate", "chart-card", "income-expense-chart",
                    "income-expense-table", "meter-margin", "meter-tax"):
            assert find_key(tree, key) is not None, key
        assert find_key(tree, "income-expense-table").visible is False       # chart first, table on toggle
        assert "$3,200" in texts(tree)                                        # net = 4,000 − 800
        assert find_key(tree, "meter-goal") is None
        assert of_type(tree, fch.BarChart)
