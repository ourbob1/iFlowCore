"""
Dashboard: net profit, tax set-aside alert, meters, income vs expenses.

Compact (phone): one column — alert, 2×2 KPI tiles, meters, chart.
Expanded (desktop): KPI row of four; chart (8/12) beside meters (4/12).
"""

from __future__ import annotations

import flet as ft

from core.financial.money import MoneyError, parse_amount
from ui.components.charts import chart_table, income_expense_chart, legend
from ui.components.common import card, empty_state, kpi_tile, meter, status_banner, text
from ui.format import days_until, distance, money, percent_bp
from ui.theme import STATUS_COLOR
from ui.viewmodels.dashboard import DashboardData, load_dashboard, record_set_aside
from ui.views.base import ViewContext, page_column


def build(ctx: ViewContext) -> ft.Control:
    s = ctx.state
    data = load_dashboard(ctx.db, today=s.today(), tz=s.tz, currency=s.currency,
                          goal_minor=s.settings.monthly_profit_goal_minor)
    return render(ctx, data)


def render(ctx: ViewContext, data: DashboardData) -> ft.Control:
    pal, lay, cur = ctx.pal, ctx.layout, data.currency
    controls: list[ft.Control] = []

    if data.needs_review:
        controls.append(status_banner(
            "warning", f"{data.needs_review} auto-detected trip(s) to review",
            "Confirm business vs personal so your mileage log stays audit-ready.", pal,
            trailing=ft.TextButton("Review", on_click=lambda e: ctx.navigate("/fleet"), height=lay.min_target),
            key="review-banner"))

    a = data.alert
    detail = a.message
    if a.next_due:
        detail += f" Next due {a.next_due:%b} {a.next_due.day} ({days_until(a.next_due, ctx.state.today())})"
        if a.amount_due_minor:
            detail += f": {money(a.amount_due_minor, cur)}"
        detail += "."
    controls.append(status_banner(a.level, a.title, detail, pal, key="tax-alert",
                                  trailing=ft.TextButton("Log", on_click=lambda e: open_set_aside_dialog(ctx, data),
                                                         height=lay.min_target)))

    col = lay.kpi_col
    controls.append(ft.ResponsiveRow([
        kpi_tile("Net profit (YTD)", money(data.net_minor, cur, cents=False), pal,
                 sub=f"{percent_bp(data.margin_bp)} margin", col=col, key="kpi-net"),
        kpi_tile("Income (YTD)", money(data.gross_minor, cur, cents=False), pal, col=col, key="kpi-income"),
        kpi_tile("Expenses & fees", money(data.spend_minor, cur, cents=False), pal, col=col, key="kpi-spend"),
        kpi_tile("Set aside for taxes", percent_bp(data.set_aside_rate_bp, decimals=0), pal,
                 sub=f"of each payout · est. {money(data.tax_total_minor, cur, cents=False)} so far",
                 col=col, key="kpi-tax-rate"),
    ], spacing=12, run_spacing=12))

    covered = data.reserved_minor + data.paid_minor
    coverage_bp = covered * 10_000 // data.tax_total_minor if data.tax_total_minor else 10_000
    meters: list[ft.Control] = [
        meter("Profit margin", data.margin_bp, percent_bp(data.margin_bp), pal, color=pal.series_income,
              key="meter-margin"),
        meter("Tax reserve coverage", coverage_bp,
              f"{money(covered, cur, cents=False)} saved · {money(data.tax_total_minor, cur, cents=False)} est.", pal,
              color=STATUS_COLOR[data.alert.level], key="meter-tax"),
    ]
    if data.goal_minor and data.goal_progress_bp is not None:
        meters.append(meter("This month vs goal", data.goal_progress_bp,
                            f"{money(data.month_net_minor, cur, cents=False)} of {money(data.goal_minor, cur, cents=False)}",
                            pal, color=pal.series_income, key="meter-goal"))
    meters.append(text(f"Mileage deduction: {money(data.mileage_deduction_minor, cur)} "
                       f"({distance(data.business_meters, ctx.state.units)})", pal, size=13, role="secondary"))

    controls.append(ft.ResponsiveRow([
        chart_card(ctx, data, col={"xs": 12, "lg": 8}),
        card(*meters, pal=pal, title="Meters", col={"xs": 12, "lg": 4}, key="meters-card"),
    ], spacing=12, run_spacing=12))

    for note in data.notes:
        controls.append(text(note, pal, size=12, role="muted"))
    return page_column(ctx, *controls, key="view-dashboard")


def chart_card(ctx: ViewContext, data: DashboardData, *, col: dict) -> ft.Container:
    pal = ctx.pal
    if data.chart.empty:
        body: ft.Control = empty_state("BAR_CHART", "No income or expenses yet",
                                       "Import a statement to see your month-by-month picture.", pal,
                                       action=("Import", lambda e: ctx.navigate("/finances")))
        return card(body, pal=pal, title=f"Income vs expenses · {data.year}", col=col, key="chart-card")

    chart = income_expense_chart(data.chart, pal, ctx.layout)
    table = chart_table(data.chart, pal)
    table.visible = False
    toggle = ft.SegmentedButton(
        segments=[ft.Segment(value="chart", label=ft.Text("Chart")), ft.Segment(value="table", label=ft.Text("Table"))],
        selected=["chart"], key="chart-toggle",
    )

    def on_toggle(e: ft.Event) -> None:
        show_table = "table" in (toggle.selected or [])
        chart.visible, table.visible = not show_table, show_table
        chart.update()
        table.update()

    toggle.on_change = on_toggle
    return card(legend(data.chart, pal), chart, ft.Row([table], scroll=ft.ScrollMode.AUTO),
                pal=pal, title=f"Income vs expenses · {data.year}", col=col, actions=[toggle], key="chart-card")


def open_set_aside_dialog(ctx: ViewContext, data: DashboardData) -> None:
    amount = ft.TextField(label=f"Amount ({data.currency})", keyboard_type=ft.KeyboardType.NUMBER, autofocus=True)
    kind = ft.Dropdown(label="Type", value="reserve", options=[
        ft.DropdownOption(key="reserve", text="Moved to tax savings"),
        ft.DropdownOption(key="payment", text="Estimated tax payment"),
        ft.DropdownOption(key="withdrawal", text="Took money out of savings"),
    ])

    def save(e: ft.Event) -> None:
        try:
            minor = parse_amount(amount.value or "", data.currency)
            record_set_aside(ctx.db, year=data.year, kind=kind.value or "reserve", amount_minor=minor,
                             on=ctx.state.today(), currency=data.currency)
        except (MoneyError, ValueError) as exc:
            amount.error = str(exc)
            amount.update()
            return
        ctx.close_dialog()
        ctx.notify("Saved")
        ctx.refresh()

    ctx.show_dialog(ft.AlertDialog(
        title=ft.Text("Log tax set-aside"),
        content=ft.Column([amount, kind], tight=True, spacing=12),
        actions=[ft.TextButton("Cancel", on_click=lambda e: ctx.close_dialog()), ft.FilledButton("Save", on_click=save)],
    ))
