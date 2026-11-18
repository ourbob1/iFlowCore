"""
Personal Intelligence: one call that builds everything the Insights view shows.

    report = build_report(db, today=date.today(), tz="America/New_York", currency="USD", period="90d",
                          zone_store=ZoneStore(db), reference=("TPA", airport_point))

All inputs come from the local database; the report holds aggregates only
(sums, rates, hour-of-week slots, relative zone positions). It is never sent
anywhere; nothing in this package imports a network client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

from core.fleet.geo import GeoPoint
from core.intelligence.data import load_activity
from core.intelligence.metrics import Efficiency, VehicleCost, efficiency, irs_vehicle_cost
from core.intelligence.models import Activity
from core.intelligence.peaks import PeakResult, find_peaks
from core.intelligence.rates import change_bp
from core.intelligence.sessions import DEFAULT_GAP, build_sessions
from core.intelligence.spatial import SpatialResult, build_zones
from core.intelligence.temporal import Granularity, HourGrid, TrendPoint, build_grid, build_trend, pick_granularity
from core.intelligence.zones import ZoneStore

Period = Literal["30d", "90d", "365d", "ytd"]
PERIODS: dict[str, str] = {"30d": "30 days", "90d": "90 days", "365d": "12 months", "ytd": "This year"}


def period_bounds(period: str, today: date) -> tuple[date, date]:
    """Inclusive local dates. Rolling windows end today; "ytd" starts Jan 1."""
    if period == "ytd":
        return date(today.year, 1, 1), today
    days = {"30d": 30, "90d": 90, "365d": 365}.get(period)
    if days is None:
        raise ValueError(f"Unknown period {period!r}")
    return today - timedelta(days=days - 1), today


def previous_bounds(start: date, end: date) -> tuple[date, date]:
    """The same number of days immediately before (for YTD: the same dates last year)."""
    if start.month == 1 and start.day == 1 and end.year == start.year and (end - start).days < 366:
        try:
            return start.replace(year=start.year - 1), end.replace(year=end.year - 1)
        except ValueError:                     # Feb 29 → Feb 28
            return start.replace(year=start.year - 1), end.replace(year=end.year - 1, day=28)
    length = (end - start).days + 1
    return start - timedelta(days=length), start - timedelta(days=1)


@dataclass
class IntelligenceReport:
    period: str
    start: date
    end: date
    tz: str
    currency: str
    activity: Activity
    efficiency: Efficiency
    previous: Efficiency
    granularity: Granularity
    trend: list[TrendPoint]
    grid: HourGrid
    peaks: PeakResult
    spatial: SpatialResult | None
    notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return self.activity.empty

    def change(self, metric: str) -> int | None:
        """Relative change vs the previous period, in bp, for net / per_hour / per_mile / meters / seconds."""
        cur, prev = getattr(self.efficiency, metric), getattr(self.previous, metric)
        if cur is None or prev is None:
            return None
        return change_bp(cur, prev)


def build_report(db: Any, *, today: date, tz: str, currency: str, period: str = "90d",
                 zone_store: ZoneStore | None = None, reference: tuple[str, GeoPoint] | None = None,
                 gap: timedelta = DEFAULT_GAP, vehicle_cost: VehicleCost | None = None,
                 min_zone_trips: int | None = None) -> IntelligenceReport:
    start, end = period_bounds(period, today)
    cost = vehicle_cost if vehicle_cost is not None else irs_vehicle_cost()

    act = load_activity(db, start=start, end=end, tz=tz, currency=currency)
    sessions = build_sessions(act, gap=gap)
    p_start, p_end = previous_bounds(start, end)
    prev_act = load_activity(db, start=p_start, end=p_end, tz=tz, currency=currency)
    prev_eff = efficiency(prev_act, build_sessions(prev_act, gap=gap), vehicle_cost=cost)

    granularity = pick_granularity(act.days)
    grid = build_grid(act, sessions)
    spatial = None
    if zone_store is not None:
        cells = zone_store.load(t.id for t in act.trips)
        kwargs = {"min_trips": min_zone_trips} if min_zone_trips else {}
        spatial = build_zones(act, cells, reference=reference, unreadable=zone_store.unreadable, **kwargs)

    report = IntelligenceReport(
        period=period, start=start, end=end, tz=tz, currency=act.currency, activity=act,
        efficiency=efficiency(act, sessions, vehicle_cost=cost), previous=prev_eff,
        granularity=granularity, trend=build_trend(act, sessions, granularity), grid=grid,
        peaks=find_peaks(grid), spatial=spatial,
    )
    report.notes = data_notes(report)
    return report


def data_notes(r: IntelligenceReport) -> list[str]:
    a = r.activity
    notes = []
    if a.untimed_earnings:
        notes.append(f"{a.untimed_earnings} earning(s) had no time of day (date-only statements); they count in "
                     "totals and trends but not in the hourly views.")
    if a.excluded_unconverted:
        notes.append(f"{a.excluded_unconverted} foreign-currency earning(s) without a converted amount were left out.")
    if a.excluded_unreviewed:
        notes.append(f"{a.excluded_unreviewed} detected trip(s) waiting for review aren't included yet.")
    if a.earnings and not r.efficiency.seconds:
        notes.append("No trip times recorded, so per-hour figures aren't available. Trip detection fills them in.")
    if r.efficiency.meters and r.efficiency.vehicle_cost_minor is None:
        notes.append("No IRS mileage rate for part of this period, so car-cost estimates are hidden.")
    if r.spatial is not None and r.spatial.unreadable:
        notes.append(f"{r.spatial.unreadable} trip zone(s) couldn't be unlocked on this device and were skipped.")
    return notes
