"""
Insights visuals: hour-of-week heatmap, trend line, relative area grid.

Dataviz rules applied
---------------------
* Heatmap: one hue (sequential blue) in 5 ordinal steps chosen per mode, the
  lowest clearing 2:1 on the card surface; "no data" is a separate neutral
  cell, never the lightest blue. Every cell has a hover/tap tooltip with the
  exact value, a legend states each step's range, and a top-10 table carries
  the numbers so nothing depends on color alone. Peak windows get a 2 px ring
  in the primary text color with a 2 px surface gap inside it (identity by
  outline, not by a second hue; visible on the darkest step too).
* Phones get the grid transposed (24 hour rows × 7 day columns) so cells stay
  large enough to tap; wider screens get 7 × 24.
* Trend: a single 2 px line (no legend needed; the title names it) with
  ≥ 8 px markers, exact values in tooltips, recessive grid, one y-axis. Gaps
  (no data) break the line instead of being drawn as zero.
"""

from __future__ import annotations

from decimal import Decimal

import flet as ft
import flet_charts as fch

from core.config import minor_units
from core.intelligence.models import WEEKDAYS
from ui.components.common import text
from ui.layout import LayoutSpec
from ui.theme import Palette
from ui.viewmodels.intelligence import HeatmapView, TrendView, ZonesView

HOUR_TICKS = {0: "12a", 3: "3a", 6: "6a", 9: "9a", 12: "12p", 15: "3p", 18: "6p", 21: "9p"}


def _fill(level: int, pal: Palette) -> str:
    return pal.heat_empty if level < 0 else pal.heat[level]


def _cell(level: int, tooltip: str, pal: Palette, *, height: int, ring: bool, key: str | None = None) -> ft.Container:
    if ring:
        # Peak outline: 2 px ring + 2 px surface gap, so it reads on the darkest fill too.
        inner = ft.Container(expand=True, bgcolor=_fill(level, pal), border_radius=ft.BorderRadius.all(2))
        return ft.Container(inner, expand=True, height=height, tooltip=tooltip, key=key, padding=ft.Padding.all(2),
                            bgcolor=pal.surface_raised, border=ft.Border.all(2, pal.text_primary),
                            border_radius=ft.BorderRadius.all(4))
    return ft.Container(
        expand=True, height=height, bgcolor=_fill(level, pal), tooltip=tooltip, key=key,
        border_radius=ft.BorderRadius.all(3), border=ft.Border.all(1, pal.grid) if level < 0 else None,
    )


def week_heatmap(view: HeatmapView, pal: Palette, layout: LayoutSpec, *,
                 peak_slots: frozenset[tuple[int, int]] = frozenset()) -> ft.Control:
    cells = {(c.weekday, c.hour): c for c in view.cells}
    gap = 2
    if layout.size_class == "compact":
        header = ft.Row([ft.Container(width=34)] + [ft.Container(text(d[0:2], pal, size=11, role="muted"), expand=True,
                                                                 alignment=ft.Alignment.CENTER) for d in WEEKDAYS],
                        spacing=gap)
        rows = [header]
        for h in range(24):
            label = HOUR_TICKS.get(h, "")
            rows.append(ft.Row(
                [ft.Container(text(label, pal, size=10, role="muted"), width=34)]
                + [_cell(cells[(d, h)].level, cells[(d, h)].tooltip, pal, height=18, ring=(d, h) in peak_slots,
                         key=f"heat-{d}-{h}") for d in range(7)],
                spacing=gap))
        return ft.Column(rows, spacing=gap, key="heatmap")

    header = ft.Row([ft.Container(width=40)] + [ft.Container(text(HOUR_TICKS.get(h, ""), pal, size=11, role="muted"),
                                                             expand=True) for h in range(24)], spacing=gap)
    rows = [header]
    for d in range(7):
        rows.append(ft.Row(
            [ft.Container(text(WEEKDAYS[d], pal, size=12, role="secondary"), width=40)]
            + [_cell(cells[(d, h)].level, cells[(d, h)].tooltip, pal, height=26, ring=(d, h) in peak_slots,
                     key=f"heat-{d}-{h}") for h in range(24)],
            spacing=gap))
    return ft.Column(rows, spacing=gap, key="heatmap")


def heat_legend(view: HeatmapView, pal: Palette, *, show_peaks: bool) -> ft.Row:
    items: list[ft.Control] = []
    for level, label in view.legend:
        items.append(ft.Row([ft.Container(width=14, height=14, bgcolor=pal.heat[level],
                                          border_radius=ft.BorderRadius.all(3)),
                             text(label, pal, size=12, role="secondary")], spacing=6, tight=True))
    items.append(ft.Row([ft.Container(width=14, height=14, bgcolor=pal.heat_empty, border=ft.Border.all(1, pal.grid),
                                      border_radius=ft.BorderRadius.all(3)),
                         text("No data", pal, size=12, role="secondary")], spacing=6, tight=True))
    if show_peaks:
        items.append(ft.Row([ft.Container(width=14, height=14, border=ft.Border.all(2, pal.text_primary),
                                          border_radius=ft.BorderRadius.all(3)),
                             text("Peak window", pal, size=12, role="secondary")], spacing=6, tight=True))
    return ft.Row(items, wrap=True, spacing=14, run_spacing=6, key="heat-legend")


def heat_table(view: HeatmapView, pal: Palette) -> ft.DataTable:
    return ft.DataTable(
        columns=[ft.DataColumn(text("Hour", pal, size=13, role="secondary")),
                 ft.DataColumn(text(view.metric.replace("_", " ").capitalize(), pal, size=13, role="secondary"),
                               numeric=True)],
        rows=[ft.DataRow(cells=[ft.DataCell(text(a, pal, size=13)), ft.DataCell(text(b, pal, size=13))])
              for a, b in view.top],
        key="heat-table",
    )


# --- trend ------------------------------------------------------------------------------------


def _y(value: int, view: TrendView, currency: str) -> float:
    """Chart geometry only (never stored): minor units → major, seconds → hours."""
    if view.is_money:
        return float(Decimal(value).scaleb(-minor_units(currency)))
    return value / 3600


def trend_chart(view: TrendView, pal: Palette, layout: LayoutSpec, currency: str) -> fch.LineChart:
    segments: list[list[fch.LineChartDataPoint]] = [[]]
    for i, (v, t) in enumerate(zip(view.values, view.texts)):
        if v is None:
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(fch.LineChartDataPoint(x=i, y=_y(v, view, currency),
                                                   tooltip=f"{view.categories[i]}: {t}"))
    series = [
        fch.LineChartData(points=seg, stroke_width=2, color=pal.series_income, curved=False,
                          point=fch.ChartCirclePoint(radius=4, color=pal.series_income, stroke_width=2,
                                                     stroke_color=pal.surface_raised))
        for seg in segments if seg
    ]
    n = len(view.categories)
    every = max(1, -(-n // (6 if layout.size_class == "compact" else 12)))
    step = view.ticks[1][0] if len(view.ticks) > 1 else view.y_max or 1
    return fch.LineChart(
        data_series=series, min_x=-0.5, max_x=max(0.5, n - 0.5), min_y=0,
        max_y=_y(view.y_max, view, currency) if view.y_max else 1, interactive=True, height=layout.chart_height,
        horizontal_grid_lines=fch.ChartGridLines(interval=_y(step, view, currency) or 1, color=pal.grid, width=1),
        left_axis=fch.ChartAxis(
            label_size=52 if layout.size_class == "compact" else 60,
            label_spacing=_y(step, view, currency) or 1,         # labels land only on this interval
            labels=[fch.ChartAxisLabel(value=_y(v, view, currency), label=text(lbl, pal, size=11, role="muted"))
                    for v, lbl in view.ticks]),
        bottom_axis=fch.ChartAxis(
            label_size=28,
            labels=[fch.ChartAxisLabel(value=i, label=text(c, pal, size=11, role="muted"))
                    for i, c in enumerate(view.categories) if i % every == 0]),
        tooltip=fch.LineChartTooltip(bgcolor=pal.surface_raised, border_radius=ft.BorderRadius.all(8)),
        point_line_start=0, point_line_end=0,
        key="trend-chart",
    )


def trend_table(view: TrendView, pal: Palette) -> ft.DataTable:
    return ft.DataTable(
        columns=[ft.DataColumn(text("Period", pal, size=13, role="secondary")),
                 ft.DataColumn(text(view.label, pal, size=13, role="secondary"), numeric=True)],
        rows=[ft.DataRow(cells=[ft.DataCell(text(c, pal, size=13)), ft.DataCell(text(t, pal, size=13))])
              for c, t in zip(view.categories, view.texts)],
        key="trend-table",
    )


# --- relative area grid ---------------------------------------------------------------------


def zone_grid(view: ZonesView, pal: Palette, layout: LayoutSpec) -> ft.Control:
    """Zones placed by their position relative to each other (north up). No map, no coordinates."""
    size = 30 if layout.size_class == "compact" else 36
    by_pos = {(c.row, c.col): c for c in view.grid}
    rows = []
    for r in range(view.grid_rows):
        row = []
        for c in range(view.grid_cols):
            cell = by_pos.get((r, c))
            if cell is None:
                row.append(ft.Container(width=size, height=size, bgcolor=pal.heat_empty,
                                        border_radius=ft.BorderRadius.all(4)))
            else:
                badge = ft.Container(text(str(cell.rank), pal, size=11, weight=ft.FontWeight.W_600), width=20,
                                     height=20, bgcolor=pal.surface_raised, border_radius=ft.BorderRadius.all(10),
                                     alignment=ft.Alignment.CENTER)
                row.append(ft.Container(badge, width=size, height=size, bgcolor=pal.heat[cell.level],
                                        border_radius=ft.BorderRadius.all(4), alignment=ft.Alignment.CENTER,
                                        tooltip=cell.tooltip, key=f"zone-cell-{cell.rank}"))
        rows.append(ft.Row(row, spacing=2, tight=True))
    return ft.Column([
        ft.Row([ft.Icon(ft.Icons.NAVIGATION, size=14, color=pal.text_muted),
                text("North is up · positions are relative, not a map", pal, size=11, role="muted")], spacing=4),
        ft.Column(rows, spacing=2, tight=True),
    ], spacing=6, tight=True, key="zone-grid")
