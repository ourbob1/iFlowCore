"""
Efficiency metrics for a period: what an hour and a mile of work are worth.

"Net" is earnings after platform fees. "After car costs" also subtracts an
estimate of vehicle cost using the IRS standard mileage rate for each trip's
local date (the IRS rate is built to cover gas, maintenance, insurance and
depreciation), so drivers can compare an hour of short trips with an hour of
long ones. It is an estimate, labelled as such in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

from core.intelligence.models import Activity, Session
from core.intelligence.rates import div_round, per_hour, per_km, per_mile, ratio_bp
from core.intelligence.sessions import split_local
from core.timeutil import zone

VehicleCost = Callable[[dict[date, int]], int | None]      # meters by local day → cost in minor units


@dataclass(frozen=True)
class Efficiency:
    net_minor: int
    gross_minor: int
    fee_minor: int
    seconds: int                       # worked (session) time inside the period
    trip_seconds: int                  # time actually driving on timed trips
    meters: int
    trips: int
    sessions: int
    active_days: int
    vehicle_cost_minor: int | None

    @property
    def per_hour_minor(self) -> int | None:
        return per_hour(self.net_minor, self.seconds)

    @property
    def per_mile_minor(self) -> int | None:
        return per_mile(self.net_minor, self.meters)

    @property
    def per_km_minor(self) -> int | None:
        return per_km(self.net_minor, self.meters)

    @property
    def per_trip_minor(self) -> int | None:
        return div_round(self.net_minor, self.trips) if self.trips else None

    @property
    def after_vehicle_minor(self) -> int | None:
        return None if self.vehicle_cost_minor is None else self.net_minor - self.vehicle_cost_minor

    @property
    def after_vehicle_per_hour_minor(self) -> int | None:
        after = self.after_vehicle_minor
        return None if after is None else per_hour(after, self.seconds)

    @property
    def fee_share_bp(self) -> int | None:
        return ratio_bp(self.fee_minor, self.gross_minor) if self.gross_minor > 0 else None

    @property
    def utilization_bp(self) -> int | None:
        """Share of worked time spent on trips (the rest is waiting / driving to pickups)."""
        return ratio_bp(min(self.trip_seconds, self.seconds), self.seconds) if self.seconds else None

    @property
    def avg_session_seconds(self) -> int | None:
        return div_round(self.seconds, self.sessions) if self.sessions else None


def efficiency(activity: Activity, sessions: list[Session], *, vehicle_cost: VehicleCost | None = None) -> Efficiency:
    z = zone(activity.tz)
    in_period = lambda d: activity.start <= d <= activity.end  # noqa: E731
    seconds = 0
    days: set[date] = set()
    counted_sessions = 0
    for s in sessions:
        inside = 0
        for piece in split_local(s, activity.tz):
            if in_period(piece.day):
                inside += piece.seconds
                days.add(piece.day)
        seconds += inside
        counted_sessions += inside > 0
    meters_by_day: dict[date, int] = {}
    trip_seconds = 0
    for t in activity.trips:
        d = t.started_at.astimezone(z).date()
        meters_by_day[d] = meters_by_day.get(d, 0) + t.meters
        days.add(d)
        trip_seconds += t.duration_s or 0
    for e in activity.earnings:
        days.add(e.day)
    meters = sum(meters_by_day.values())
    cost = vehicle_cost(meters_by_day) if (vehicle_cost is not None and meters) else (0 if meters == 0 else None)
    return Efficiency(
        net_minor=sum(e.net_minor for e in activity.earnings),
        gross_minor=sum(e.gross_minor for e in activity.earnings),
        fee_minor=sum(e.fee_minor for e in activity.earnings),
        seconds=seconds, trip_seconds=trip_seconds, meters=meters, trips=len(activity.trips),
        sessions=counted_sessions, active_days=len(days), vehicle_cost_minor=cost,
    )


def irs_vehicle_cost(registry=None) -> VehicleCost:
    """Vehicle cost at the IRS standard mileage rate, per tax year, local dates.
    Returns None when a year has no rate table."""
    from core.financial.tax import TaxError, TaxTableRegistry, mileage_deduction

    reg = registry or TaxTableRegistry.load()

    def cost(meters_by_day: dict[date, int]) -> int | None:
        total = 0
        by_year: dict[int, dict[date, int]] = {}
        for d, m in meters_by_day.items():
            by_year.setdefault(d.year, {})[d] = m
        for year, days in by_year.items():
            try:
                table, _ = reg.get("US", year)
                total += mileage_deduction(days, table)
            except (TaxError, LookupError, ValueError):
                return None
        return total

    return cost
