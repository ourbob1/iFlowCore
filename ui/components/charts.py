"""
Income vs expenses chart (grouped columns) + legend + accessible table view.

Dataviz rules applied: categorical slots 1–2 validated in both modes; bars
≤ 20 px with 4 px rounded tops, square at the baseline; 2 px gap between
the pair; hairline recessive gridlines; a legend (two series) with swatches
beside text-token labels; per-bar hover/tap tooltips with exact amounts; a
table view carrying every value so nothing depends on color or hover.
"""

from __future__ import annotations

from decimal import Decimal

import flet as ft
import flet_charts as fch

from core.config import minor_units
from ui.components.common import text
from ui.format import money
from ui.layout import LayoutSpec
from ui.theme import Palette
from ui.viewmodels.dashboard import BarChartData


def to_chart_units(amount_minor: int, currency: str) -> float:
    """Minor units → major-unit float for chart *geometry only* (never stored)."""
    return float(Decimal(amount_minor).scaleb(-minor_units(currency)))


def legend(data: BarChartData, pal: Palette) -> ft.Row:
    items = []
    for s in data.series:
        items.append(ft.Row([
            ft.Container(width=12, height=12, bgcolor=getattr(pal, s.color_role), border_radius=ft.BorderRadius.all(3)),
            text(s.label, pal, size=13, role="secondary"),
        ], spacing=6, tight=True))
    return ft.Row(items, spacing=16, key="chart-legend")


def bar_width(layout: LayoutSpec, categories: int) -> int:
    base = 10 if layout.size_class == "compact" else 16
    if categories <= 6:
        base += 4
    return min(base, 20)


def income_expense_chart(data: BarChartData, pal: Palette, layout: LayoutSpec) -> fch.BarChart:
    width = bar_width(layout, len(data.categories))
    groups = []
    for i, cat in enumerate(data.categories):
        rods = [
            fch.BarChartRod(
                from_y=0,
                to_y=to_chart_units(s.values[i], data.currency),
                width=width,
                color=getattr(pal, s.color_role),
                border_radius=ft.BorderRadius.only(top_left=4, top_right=4),
                tooltip=f"{cat} {s.label.lower()}: {money(s.values[i], data.currency)}",
            )
            for s in data.series
        ]
        groups.append(fch.BarChartGroup(x=i, rods=rods, spacing=2))

    step = data.ticks[1][0] - data.ticks[0][0] if len(data.ticks) > 1 else data.y_max_minor or 1
    return fch.BarChart(
        groups=groups,
        max_y=to_chart_units(data.y_max_minor, data.currency),
        min_y=0,
        interactive=True,
        height=layout.chart_height,
        horizontal_grid_lines=fch.ChartGridLines(interval=to_chart_units(step, data.currency), color=pal.grid, width=1),
        left_axis=fch.ChartAxis(
            label_size=48 if layout.size_class == "compact" else 56,
            labels=[fch.ChartAxisLabel(value=to_chart_units(v, data.currency),
                                       label=text(lbl, pal, size=11, role="muted")) for v, lbl in data.ticks],
        ),
        bottom_axis=fch.ChartAxis(
            label_size=28,
            labels=[fch.ChartAxisLabel(value=i, label=text(c, pal, size=11, role="muted"))
                    for i, c in enumerate(data.categories)],
        ),
        tooltip=fch.BarChartTooltip(bgcolor=pal.surface_raised, border_radius=ft.BorderRadius.all(8)),
        key="income-expense-chart",
    )


def chart_table(data: BarChartData, pal: Palette) -> ft.DataTable:
    return ft.DataTable(
        columns=[ft.DataColumn(text("Month", pal, size=13, role="secondary"))]
        + [ft.DataColumn(text(s.label, pal, size=13, role="secondary"), numeric=True) for s in data.series],
        rows=[
            ft.DataRow(cells=[ft.DataCell(text(row[0], pal, size=13))]
                       + [ft.DataCell(text(money(v, data.currency), pal, size=13)) for v in row[1:]])
            for row in data.table_rows()
        ],
        key="income-expense-table",
    )
