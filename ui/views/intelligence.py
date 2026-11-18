"""
Insights (Personal Intelligence): trends, peak earning hours, hour-of-week
heatmap, and the best-paying areas, all computed on this device from the
local database.

Controls are links (`/insights?period=…&trend=…&heat=…`), so the view is a
pure function of the route and the data.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import flet as ft

from core.intelligence import PERIODS
from ui.components.common import card, empty_state, kpi_tile, primary_button, text
from ui.components.insights import heat_legend, heat_table, trend_chart, trend_table, week_heatmap, zone_grid
from ui.viewmodels.intelligence import HEAT_METRICS, TREND_METRICS, InsightsData, Kpi, load_insights
from ui.views.base import ViewContext, page_column

PRIVACY_LINE = "Computed on this device from your encrypted data. None of it is sent anywhere."


def build(ctx: ViewContext) -> ft.Control:
    return render(ctx, load_insights(ctx.state, ctx.params))


def _link(ctx: ViewContext, data: InsightsData, **changes: str) -> str:
    params = {"period": data.period, "trend": data.trend.metric, "heat": data.heatmap.metric, **changes}
    return "/insights?" + urlencode(params)


def _chips(ctx: ViewContext, data: InsightsData, param: str, options: dict[str, str], current: str,
           key: str) -> ft.Row:
    return ft.Row([
        ft.Chip(label=ft.Text(label), selected=value == current, show_checkmark=False, key=f"{key}-{value}",
                on_select=lambda e, v=value: ctx.navigate(_link(ctx, data, **{param: v})))
        for value, label in options.items()
    ], wrap=True, spacing=8, run_spacing=8, key=key)


def render(ctx: ViewContext, data: InsightsData) -> ft.Control:
    pal = ctx.pal
    header = ft.Column([
        _chips(ctx, data, "period", PERIODS, data.period, "period"),
        text(data.range_label, pal, size=12, role="muted"),
    ], spacing=6, tight=True)
    if data.empty:
        return page_column(ctx, header, empty_state(
            "INSIGHTS", "Nothing to analyze yet",
            "Import a platform statement or log some trips; your trends and best hours will appear here.", pal,
            key="insights-empty"), text(PRIVACY_LINE, pal, size=12, role="muted"), key="view-insights")

    controls: list[ft.Control] = [
        header,
        ft.ResponsiveRow([kpi(ctx, k) for k in data.kpis], spacing=12, run_spacing=12, key="insight-kpis"),
        peaks_card(ctx, data),
        heatmap_card(ctx, data),
        trend_card(ctx, data),
        areas_card(ctx, data),
    ]
    for n in data.notes:
        controls.append(text(n, pal, size=12, role="muted"))
    controls.append(ft.Row([ft.Icon(ft.Icons.LOCK_OUTLINE, size=14, color=pal.text_muted),
                            ft.Container(text(PRIVACY_LINE, pal, size=12, role="muted"), expand=True)],
                           spacing=6, key="privacy-line"))
    return page_column(ctx, *controls, key="view-insights")


def kpi(ctx: ViewContext, k: Kpi) -> ft.Container:
    sub = k.sub
    if k.change_bp is not None and sub:
        arrow = "↑" if k.change_bp > 0 else "↓" if k.change_bp < 0 else "→"
        sub = f"{arrow} {sub}"
    col = {"xs": 6, "md": 4, "lg": 4, "xl": 2}
    return kpi_tile(k.label, k.value, ctx.pal, sub=sub, col=col, key=f"kpi-{k.key}")


def peaks_card(ctx: ViewContext, data: InsightsData) -> ft.Container:
    pal = ctx.pal
    body: list[ft.Control] = [text(data.peak_message, pal, size=13, role="secondary")]
    for i, p in enumerate(data.peaks, start=1):
        badge = ft.Container(text(str(i), pal, size=14, weight=ft.FontWeight.W_700), width=28, height=28,
                             border=ft.Border.all(2, pal.text_primary), border_radius=ft.BorderRadius.all(14),
                             alignment=ft.Alignment.CENTER)
        chip = ft.Container(text(f"{p.confidence} confidence", pal, size=11, role="secondary"),
                            padding=ft.Padding.symmetric(horizontal=8, vertical=4),
                            border=ft.Border.all(1, pal.grid), border_radius=ft.BorderRadius.all(10))
        # No wrapping Row here: an expanding child inside a wrap is a layout error on narrow screens.
        body.append(ft.Container(ft.Row([
            badge,
            ft.Column([
                text(f"{p.title} · {p.time_range}", pal, size=14, weight=ft.FontWeight.W_600),
                text(" · ".join(x for x in (p.rate, p.lift) if x), pal, size=13),
                text(p.detail, pal, size=12, role="muted"),
                chip,
            ], spacing=4, tight=True, expand=True),
        ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.START), key=f"peak-{i}"))
    return card(*body, pal=pal, title="Your peak earning hours", key="peaks-card")


def heatmap_card(ctx: ViewContext, data: InsightsData) -> ft.Container:
    pal, view = ctx.pal, data.heatmap
    peak_slots = frozenset(s for p in data.peaks for s in p.slots)
    body: list[ft.Control] = [_chips(ctx, data, "heat", HEAT_METRICS, view.metric, "heat-metric")]
    if view.note:
        body.append(text(view.note, pal, size=12, role="muted"))
    body += [
        week_heatmap(view, pal, ctx.layout, peak_slots=peak_slots),
        heat_legend(view, pal, show_peaks=bool(peak_slots)),
    ]
    if view.top:
        body.append(ft.ExpansionTile(title=text("Top hours as a table", pal, size=13, role="secondary"),
                                     controls=[heat_table(view, pal)], key="heat-table-tile"))
    title = {"per_hour": "Net per hour by time of week", "earnings": "Typical weekly earnings by hour",
             "trips": "Trips by hour started"}[view.metric]
    return card(*body, pal=pal, title=title, key="heatmap-card")


def trend_card(ctx: ViewContext, data: InsightsData) -> ft.Container:
    pal, view = ctx.pal, data.trend
    labels = dict(TREND_METRICS)
    if "km" in view.label:
        labels["per_distance"] = "Net per km"
    body: list[ft.Control] = [_chips(ctx, data, "trend", labels, view.metric, "trend-metric")]
    if view.empty:
        body.append(text("No data for this measure in the period.", pal, size=13, role="secondary"))
    else:
        body += [trend_chart(view, pal, ctx.layout, data.currency),
                 ft.ExpansionTile(title=text("Show as a table", pal, size=13, role="secondary"),
                                  controls=[trend_table(view, pal)], key="trend-table-tile")]
    grain = {"day": "daily", "week": "weekly", "month": "monthly"}[data.report.granularity]
    return card(*body, pal=pal, title=f"{view.label}, {grain}", key="trend-card")


def areas_card(ctx: ViewContext, data: InsightsData) -> ft.Container:
    pal, z = ctx.pal, data.zones
    if not z.enabled:
        def enable(e: Any) -> None:
            ctx.state.update_settings(insights_zones_enabled=True)
            ctx.notify("Area insights on. New trips from trip detection will be counted.")
            ctx.refresh()

        return card(
            text(z.message, pal, size=13, role="secondary"),
            text("When trip detection finishes a trip, iFlow Core keeps only the rough area (about 1 km) where it "
                 "started and ended. Your route and exact addresses are never kept, nothing is recorded near your "
                 "home, and area data never leaves this device or syncs.", pal, size=12, role="muted"),
            primary_button("Turn on area insights", enable, ctx.layout, icon="PLACE", key="btn-enable-zones"),
            pal=pal, title="Best-paying areas", key="areas-card")

    body: list[ft.Control] = [text(z.message, pal, size=13, role="secondary")]
    if z.status == "ok":
        body.append(ft.ResponsiveRow([
            ft.Container(zone_grid(z, pal, ctx.layout), col={"xs": 12, "md": 5}),
            ft.Column([
                ft.Column([
                    text(f"{r.rank}. {r.label}", pal, size=14, weight=ft.FontWeight.W_600),
                    text(" · ".join(x for x in (f"{r.per_trip} per trip" if r.per_trip else None, r.best_time) if x),
                         pal, size=13),
                    text(r.detail, pal, size=12, role="muted"),
                ], spacing=2, tight=True, key=f"zone-row-{r.rank}") for r in z.rows
            ], spacing=10, col={"xs": 12, "md": 7}),
        ], spacing=16, run_spacing=12))
        if z.footer:
            body.append(text(z.footer, pal, size=12, role="muted"))

    def forget(e: Any) -> None:
        removed = ctx.state.zone_store().forget_all()
        ctx.state.update_settings(insights_zones_enabled=False)
        ctx.notify(f"Deleted {removed} saved area record(s) and turned area insights off")
        ctx.refresh()

    body.append(ft.Row([
        ft.TextButton("Forget saved areas & turn off", icon=ft.Icons.DELETE_OUTLINE, on_click=forget,
                      key="btn-forget-zones"),
    ]))
    return card(*body, pal=pal, title="Best-paying areas", key="areas-card")
