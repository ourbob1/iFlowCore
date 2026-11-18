"""
Insights view-model: turns an IntelligenceReport into display-ready data
(strings, heat levels, chart values). Pure and testable; no Flet here.

State lives in the route (`/insights?period=90d&trend=net&heat=per_hour`), so
every control is a link and a re-render is always consistent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.config import UnitSystem
from core.fleet.demand import AIRPORTS
from core.intelligence import PERIODS, IntelligenceReport, build_report
from core.intelligence.models import WEEKDAY_NAMES, WEEKDAYS
from core.intelligence.rates import div_round, per_hour
from core.intelligence.temporal import HourCell, hour_label
from ui.format import distance, duration, money, money_compact, percent_bp
from ui.state import AppState

TREND_METRICS = {"net": "Net earnings", "per_hour": "Net per hour", "per_distance": "Net per mile", "hours": "Hours"}
HEAT_METRICS = {"per_hour": "Per hour", "earnings": "Earnings", "trips": "Trips"}
MIN_RATED_SECONDS = 30 * 60          # a heat cell needs this much worked time to show an hourly rate
LEVELS = 5


def _pick(value: str | None, options: dict[str, Any], default: str) -> str:
    return value if value in options else default


# --- view data ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Kpi:
    key: str
    label: str
    value: str
    sub: str | None = None
    change_bp: int | None = None
    change_good: bool | None = None     # True: better than before; False: worse; None: flat/unknown


@dataclass(frozen=True)
class HeatCellView:
    weekday: int
    hour: int
    level: int                         # -1: no data; 0..4 low → high
    tooltip: str


@dataclass(frozen=True)
class HeatmapView:
    metric: str
    cells: tuple[HeatCellView, ...]    # 168, weekday * 24 + hour
    legend: tuple[tuple[int, str], ...]      # (level, range text)
    top: tuple[tuple[str, str], ...]         # accessible table: (slot, value), best first
    note: str | None = None


@dataclass(frozen=True)
class PeakRow:
    title: str
    time_range: str
    rate: str
    lift: str
    detail: str
    confidence: str
    slots: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class TrendView:
    metric: str
    label: str
    categories: tuple[str, ...]
    values: tuple[int | None, ...]     # minor units (money) or seconds (hours); None = no data
    texts: tuple[str, ...]
    ticks: tuple[tuple[int, str], ...]
    y_max: int
    is_money: bool

    @property
    def empty(self) -> bool:
        return all(v in (None, 0) for v in self.values)


@dataclass(frozen=True)
class ZoneRow:
    rank: int
    label: str
    detail: str
    per_trip: str | None
    best_time: str | None


@dataclass(frozen=True)
class ZoneGridCell:
    row: int
    col: int
    rank: int
    level: int
    tooltip: str


@dataclass
class ZonesView:
    enabled: bool
    status: str
    message: str
    rows: list[ZoneRow] = field(default_factory=list)
    grid: list[ZoneGridCell] = field(default_factory=list)
    grid_rows: int = 0
    grid_cols: int = 0
    footer: str | None = None
    stored: int = 0


@dataclass
class InsightsData:
    period: str
    period_label: str
    range_label: str
    currency: str
    empty: bool
    kpis: list[Kpi]
    trend: TrendView
    heatmap: HeatmapView
    peak_status: str
    peak_message: str
    peaks: list[PeakRow]
    zones: ZonesView
    notes: list[str]
    report: IntelligenceReport


# --- builders ------------------------------------------------------------------------------


def reference_point(state: AppState) -> tuple[str, Any] | None:
    airport = AIRPORTS.get((state.settings.radar_airport_icao or "").upper())
    return (airport.iata, airport.location) if airport else None


def load_insights(state: AppState, params: dict[str, str] | None = None) -> InsightsData:
    params = params or {}
    period = _pick(params.get("period"), PERIODS, "90d")
    report = build_report(state.db, today=state.today(), tz=state.tz, currency=state.currency, period=period,
                          zone_store=state.zone_store(), reference=reference_point(state))
    return insights_from_report(report, state.units, params=params,
                                zones_enabled=state.settings.insights_zones_enabled)


def insights_from_report(report: IntelligenceReport, units: UnitSystem, *, params: dict[str, str] | None = None,
                         zones_enabled: bool = False) -> InsightsData:
    params = params or {}
    ccy = report.currency
    trend_metric = _pick(params.get("trend"), TREND_METRICS, "net")
    heat_metric = _pick(params.get("heat"), HEAT_METRICS, "per_hour" if report.grid.total_seconds else "earnings")
    rng = f"{report.start:%b} {report.start.day}, {report.start.year} – {report.end:%b} {report.end.day}, {report.end.year}"
    return InsightsData(
        period=report.period, period_label=PERIODS[report.period], range_label=rng, currency=ccy,
        empty=report.empty, kpis=kpis(report, units), trend=trend_view(report, trend_metric, units),
        heatmap=heatmap_view(report, heat_metric), peak_status=report.peaks.status,
        peak_message=report.peaks.message, peaks=peak_rows(report), zones=zones_view(report, units, zones_enabled),
        notes=list(report.notes), report=report,
    )


def _change(report: IntelligenceReport, metric: str, higher_is_better: bool = True) -> tuple[str | None, int | None,
                                                                                               bool | None]:
    bp = report.change(metric)
    if bp is None:
        return None, None, None
    sign = "+" if bp > 0 else ""
    good = None if bp == 0 else (bp > 0) == higher_is_better
    return f"{sign}{percent_bp(bp, decimals=0)} vs previous {PERIODS[report.period].lower()}" \
        if report.period != "ytd" else f"{sign}{percent_bp(bp, decimals=0)} vs same dates last year", bp, good


def kpis(report: IntelligenceReport, units: UnitSystem) -> list[Kpi]:
    e, ccy = report.efficiency, report.currency
    metric_units = units is UnitSystem.METRIC
    per_dist = e.per_km_minor if metric_units else e.per_mile_minor
    unit = units.distance_unit
    out = []
    sub, bp, good = _change(report, "net_minor")
    out.append(Kpi("net", "Net earnings", money(e.net_minor, ccy, cents=False),
                   sub or f"after {money(e.fee_minor, ccy, cents=False)} platform fees", bp, good))
    sub, bp, good = _change(report, "per_hour_minor")
    out.append(Kpi("per_hour", "Net per hour", money(e.per_hour_minor, ccy) if e.per_hour_minor is not None else "—",
                   sub or ("no trip times yet" if e.per_hour_minor is None else None), bp, good))
    sub, bp, good = _change(report, "per_km_minor" if metric_units else "per_mile_minor")
    out.append(Kpi("per_distance", f"Net per {unit}", money(per_dist, ccy) if per_dist is not None else "—",
                   sub or ("no trips logged" if per_dist is None else None), bp, good))
    after = e.after_vehicle_per_hour_minor
    cost_sub = (f"car costs est. {money(e.vehicle_cost_minor, ccy, cents=False)} at the IRS rate"
                if e.vehicle_cost_minor else None)
    out.append(Kpi("after_car", "Per hour after car costs", money(after, ccy) if after is not None else "—",
                   cost_sub or "needs trip times and miles"))
    util = e.utilization_bp
    hours_text = (f"{div_round(e.seconds, 3600):,} h" if e.seconds >= 10 * 3600 else duration(e.seconds)) \
        if e.seconds else "—"
    out.append(Kpi("hours", "Hours worked", hours_text,
                   f"{e.sessions} session(s) · {percent_bp(util, decimals=0)} on trips" if util is not None
                   else f"{e.active_days} active day(s)"))
    out.append(Kpi("trips", "Business trips", f"{e.trips:,}",
                   f"{distance(e.meters, units, decimals=0)} · {money(e.per_trip_minor, ccy)} per trip"
                   if e.per_trip_minor is not None else distance(e.meters, units, decimals=0)))
    return out


# --- trend -----------------------------------------------------------------------------------


def nice_ceiling(value: int) -> int:
    """Round up to 1, 2, 2.5 or 5 × 10ⁿ so axis ticks land on round numbers."""
    if value <= 0:
        return 0
    mag = 1
    while mag * 10 <= value:
        mag *= 10
    for m in (1, 2, 2.5, 5, 10):
        if value <= m * mag:
            return int(m * mag)
    return 10 * mag


def trend_view(report: IntelligenceReport, metric: str, units: UnitSystem) -> TrendView:
    ccy = report.currency
    label = TREND_METRICS[metric]
    if metric == "per_distance" and units is UnitSystem.METRIC:
        label = "Net per km"
    values: list[int | None] = []
    for p in report.trend:
        if metric == "net":
            values.append(p.net_minor)
        elif metric == "per_hour":
            values.append(p.per_hour_minor)
        elif metric == "per_distance":
            from core.intelligence.rates import per_km

            values.append(per_km(p.net_minor, p.meters) if units is UnitSystem.METRIC else p.per_mile_minor)
        else:
            values.append(p.seconds)
    is_money = metric != "hours"
    fmt = (lambda v: money(v, ccy)) if is_money else (lambda v: duration(v))
    texts = tuple("no data" if v is None else fmt(v) for v in values)
    top = nice_ceiling(max((v for v in values if v is not None), default=0))
    if not is_money:                                   # hours: round the axis to whole hours
        top = nice_ceiling(-(-top // 3600)) * 3600 if top else 0
    ticks = tuple((top * i // 4, money_compact(top * i // 4, ccy) if is_money else f"{top * i // 4 // 3600} h")
                  for i in range(5)) if top else ()
    return TrendView(metric, label, tuple(p.label for p in report.trend), tuple(values), texts, ticks, top, is_money)


# --- heatmap ---------------------------------------------------------------------------------


def _levels(values: dict[int, int]) -> dict[int, int]:
    """Rank-based quintiles over the cells that have data, so one huge night
    doesn't flatten everything else into the lowest color."""
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda kv: (kv[1], kv[0]))
    n = len(ordered)
    out = {}
    for rank, (idx, _value) in enumerate(ordered):
        out[idx] = min(LEVELS - 1, rank * LEVELS // n)
    # equal values share the highest level any of them reached
    by_value: dict[int, int] = {}
    for idx, value in ordered:
        by_value[value] = max(by_value.get(value, 0), out[idx])
    return {idx: by_value[v] for idx, v in values.items()}


def heatmap_view(report: IntelligenceReport, metric: str) -> HeatmapView:
    grid, ccy = report.grid, report.currency
    values: dict[int, int] = {}
    fmt: dict[int, str] = {}
    note = None

    def describe(c: HourCell) -> str:
        parts = [f"{money(c.net_minor, ccy)} net" if c.net_minor else "no timed earnings"]
        if c.seconds:
            parts.append(f"{duration(c.seconds)} worked")
        if c.trips:
            parts.append(f"{c.trips} trip(s)")
        parts.append(f"{c.weeks} week(s)")
        return ", ".join(parts)

    for c in grid.cells:
        if metric == "per_hour":
            if c.seconds >= MIN_RATED_SECONDS:
                rate = per_hour(c.net_minor, c.seconds)
                values[c.index] = rate or 0
                fmt[c.index] = f"{money(rate or 0, ccy)}/h"
        elif metric == "earnings":
            if c.net_minor > 0:
                values[c.index] = grid.typical_net_minor(c)
                fmt[c.index] = f"{money(values[c.index], ccy)} a week"
        elif c.trips:
            values[c.index] = c.trips
            fmt[c.index] = f"{c.trips} trip(s)"
    if metric == "per_hour" and not values:
        note = "Hourly rates show once a time slot has at least 30 minutes of logged work."
    levels = _levels(values)
    cells = []
    for c in grid.cells:
        level = levels.get(c.index, -1)
        lead = fmt.get(c.index, "not enough data" if (c.seconds or c.net_minor or c.trips) else "no activity")
        cells.append(HeatCellView(c.weekday, c.hour, level,
                                  f"{WEEKDAY_NAMES[c.weekday]} {hour_label(c.hour)}: {lead} ({describe(c)})"))
    legend = []
    for lvl in range(LEVELS):
        members = [values[i] for i, l in levels.items() if l == lvl]
        if members:
            lo, hi = min(members), max(members)
            show = (lambda v: f"{money(v, ccy, cents=False)}") if metric != "trips" else str
            legend.append((lvl, show(lo) if lo == hi else f"{show(lo)}–{show(hi)}"))
    top = sorted(values.items(), key=lambda kv: kv[1], reverse=True)[:10]
    table = tuple((f"{WEEKDAYS[i // 24]} {hour_label(i % 24)}", fmt[i]) for i, _ in top)
    return HeatmapView(metric, tuple(cells), tuple(legend), table, note)


# --- peaks -----------------------------------------------------------------------------------


def peak_rows(report: IntelligenceReport) -> list[PeakRow]:
    ccy = report.currency
    rows = []
    for w in report.peaks.windows:
        rows.append(PeakRow(
            title=w.label.capitalize(), time_range=w.time_range, rate=f"{money(w.per_hour_minor, ccy)}/h",
            lift=f"{percent_bp(w.lift_bp - 10_000, decimals=0)} above your average" if w.lift_bp > 10_000 else "",
            detail=f"{duration(w.seconds)} worked over {w.weeks}+ weeks · {money(w.extra_minor, ccy, cents=False)} "
                   f"more than average hours would have paid",
            confidence=w.confidence, slots=tuple(w.slots),
        ))
    return rows


# --- zones ------------------------------------------------------------------------------------


def zones_view(report: IntelligenceReport, units: UnitSystem, enabled: bool) -> ZonesView:
    sp = report.spatial
    if not enabled and (sp is None or sp.status == "no_zones"):
        return ZonesView(False, "off", "Turn on area insights to see which parts of town pay best.")
    if sp is None:
        return ZonesView(enabled, "no_zones", "No area data yet.")
    ccy = report.currency
    view = ZonesView(enabled, sp.status, sp.message)
    if sp.status != "ok":
        return view
    top_value = max((z.per_trip_minor or 0 for z in sp.zones), default=0)
    for z in sp.zones:
        where = z.label
        if z.distance_m is not None and z.direction:
            where = f"{distance(z.distance_m, units)} {z.label}"
        detail = f"{z.drop_offs} drop-off(s)"
        if z.matched_trips:
            detail += f" · {money(z.net_minor, ccy, cents=False)} matched from {z.matched_trips} trip(s)"
        view.rows.append(ZoneRow(z.rank, where, detail, money(z.per_trip_minor, ccy) if z.per_trip_minor else None,
                                 z.best_time))
    for c in sp.grid.cells:
        z = sp.zones[c.rank - 1]
        level = (min(LEVELS - 1, c.value_minor * LEVELS // top_value) if top_value > 0 else 0)
        where = view.rows[c.rank - 1].label
        per_trip = f"{money(c.value_minor, ccy)} per trip" if c.value_minor else "no matched earnings"
        view.grid.append(ZoneGridCell(c.row, c.col, c.rank, level,
                                      f"#{c.rank} {where}: {z.drop_offs} drop-off(s), {per_trip}"))
    view.grid_rows, view.grid_cols = sp.grid.rows, sp.grid.cols
    footer = []
    if sp.hidden_areas:
        footer.append(f"{sp.hidden_areas} other area(s) with fewer drop-offs aren't shown")
    footer.append(f"{percent_bp(sp.coverage_bp, decimals=0)} of trips have an area recorded")
    view.footer = "; ".join(footer) + "."
    return view

