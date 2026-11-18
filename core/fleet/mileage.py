"""
Mileage tracking and the IRS standard-mileage calculator.

Trip lifecycle
--------------
    trip = start_trip(db, vehicle_id=..., odometer_start_m=...)   # row with trip_state='active'
    result = stop_trip(db, trip.id, odometer_end_m=...)            # or distance_m=..., or gps=[...]

`stop_trip` resolves the distance from the best evidence available:

    1. an explicit distance the user typed (`distance_m`)
    2. odometer readings (end − start)
    3. the GPS trace (`core.fleet.geo.route_distance`, noise-filtered)

and cross-checks the others when present (odometer vs GPS disagreement,
route shorter than the straight line, implausible speed). Distances are
stored as integer meters; convert user input with `UnitSystem.to_meters`.
GPS points are used to compute the distance and are *not* stored — only the
resulting meters are, which keeps precise location history off disk.

IRS calculator
--------------
`IRSMileageCalculator` applies the standard mileage rate in effect on each
trip's **local** calendar date (2026: 72.5¢ through June 30, 76¢ from July 1
per Announcement 2026-11), using the effective-dated rates in the federal
tax tables. The official total is computed per rate period — meters summed
per rate and rounded once — the same method `core.financial.tax` uses, so
the tax estimate and the mileage report always agree. Per-trip amounts are
also reported for display; their sum can differ from the total by a few cents
of rounding.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Iterable, Literal, Sequence

from core.financial.tax import METERS_PER_MILE, TaxTable, TaxTableRegistry, mileage_deduction
from core.fleet.geo import GpsFix, route_distance, straight_line_m
from core.timeutil import local_date, parse_iso, to_iso_utc, utc_now

Purpose = Literal["business", "commute", "personal"]

MAX_PLAUSIBLE_SPEED_MPS = 60          # ~134 mph average over a whole trip
MAX_PLAUSIBLE_TRIP_M = 1_600_000      # 1,600 km (~1,000 mi) in one trip
ODOMETER_GPS_TOLERANCE_BP = 1_000     # 10% disagreement triggers a warning


class MileageError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Trip records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Trip:
    id: str
    vehicle_id: str | None
    platform_id: str | None
    started_at: str
    ended_at: str | None
    distance_m: int
    purpose: str
    trip_state: str
    distance_source: str | None
    odometer_start_m: int | None
    odometer_end_m: int | None
    description: str | None
    start_location: str | None
    end_location: str | None

    @classmethod
    def from_row(cls, row: Any) -> "Trip":
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class StopResult:
    trip: Trip
    warnings: tuple[str, ...] = ()
    maintenance_alerts: tuple[Any, ...] = ()     # core.fleet.models.DueItem


_TRIP_COLUMNS = ", ".join(Trip.__dataclass_fields__)


def get_trip(db: Any, trip_id: str) -> Trip:
    with db.connection() as conn:
        row = conn.execute(
            f"SELECT {_TRIP_COLUMNS} FROM mileage_logs WHERE id = ? AND deleted_at IS NULL", (trip_id,)
        ).fetchone()
    if row is None:
        raise MileageError(f"No trip {trip_id}")
    return Trip.from_row(row)


def get_active_trip(db: Any, vehicle_id: str | None = None) -> Trip | None:
    with db.connection() as conn:
        row = conn.execute(
            f"SELECT {_TRIP_COLUMNS} FROM mileage_logs WHERE trip_state = 'active' AND deleted_at IS NULL "
            "AND ifnull(vehicle_id, '') = ifnull(?, '')", (vehicle_id,)
        ).fetchone()
    return Trip.from_row(row) if row else None


def _vehicle_row(conn: Any, vehicle_id: str) -> Any:
    row = conn.execute(
        "SELECT id, odometer_m, is_active FROM vehicles WHERE id = ? AND deleted_at IS NULL", (vehicle_id,)
    ).fetchone()
    if row is None:
        raise MileageError(f"No vehicle {vehicle_id}")
    if not row["is_active"]:
        raise MileageError("Vehicle is inactive")
    return row


def start_trip(
    db: Any,
    *,
    vehicle_id: str | None = None,
    platform_id: str | None = None,
    purpose: Purpose = "business",
    description: str | None = None,
    start_location: str | None = None,
    odometer_start_m: int | None = None,
    started_at: datetime | None = None,
    auto_assign: bool = True,
    timezone: str = "UTC",
    device_id: str | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> Trip:
    """Begin a live trip. With no `vehicle_id`, the fleet assignment for the
    platform (or the default vehicle) is used when `auto_assign` is on."""
    if purpose not in ("business", "commute", "personal"):
        raise MileageError(f"Invalid purpose '{purpose}'")
    when = started_at or clock()
    if vehicle_id is None and auto_assign:
        from core.fleet.vehicle import resolve_vehicle

        vehicle_id = resolve_vehicle(db, platform_id=platform_id, on=local_date(when, timezone))

    trip_id = str(uuid.uuid4())
    with db.transaction() as conn:
        if vehicle_id is not None:
            vehicle = _vehicle_row(conn, vehicle_id)
            if odometer_start_m is not None and odometer_start_m < vehicle["odometer_m"]:
                raise MileageError(
                    f"Odometer {odometer_start_m} m is below the last reading ({vehicle['odometer_m']} m)"
                )
        if odometer_start_m is not None and odometer_start_m < 0:
            raise MileageError("Odometer cannot be negative")
        existing = conn.execute(
            "SELECT id FROM mileage_logs WHERE trip_state = 'active' AND deleted_at IS NULL "
            "AND ifnull(vehicle_id, '') = ifnull(?, '')", (vehicle_id,)
        ).fetchone()
        if existing:
            raise MileageError(f"Trip {existing['id']} is already active for this vehicle; stop it first")
        conn.execute(
            """INSERT INTO mileage_logs (id, vehicle_id, platform_id, started_at, distance_m, odometer_start_m,
                   purpose, description, start_location, source, trip_state, last_modified_device)
               VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, 'manual', 'active', ?)""",
            (trip_id, vehicle_id, platform_id, to_iso_utc(when), odometer_start_m, purpose, description,
             start_location, device_id),
        )
    return get_trip(db, trip_id)


def resolve_distance(
    *,
    started_at: datetime,
    ended_at: datetime,
    distance_m: int | None = None,
    odometer_start_m: int | None = None,
    odometer_end_m: int | None = None,
    gps: Sequence[GpsFix] | None = None,
) -> tuple[int, str, list[str]]:
    """Pick the trip distance and collect consistency warnings.

    Returns (meters, source, warnings) where source is manual|odometer|gps.
    """
    warnings: list[str] = []
    odo = None
    if odometer_end_m is not None:
        if odometer_start_m is None:
            warnings.append("End odometer given without a start reading; not used for distance")
        elif odometer_end_m < odometer_start_m:
            raise MileageError("End odometer is lower than start odometer")
        else:
            odo = odometer_end_m - odometer_start_m

    route = None
    if gps:
        result = route_distance(gps)
        if result.kept_fixes >= 2:
            route = result.distance_m
            ordered = sorted(gps, key=lambda f: f.at)
            baseline = straight_line_m(ordered[0].point, ordered[-1].point)
            if route < baseline * 0.98:
                warnings.append(
                    f"GPS route ({route} m) is shorter than the straight-line distance ({baseline} m); trace is incomplete"
                )
            if result.dropped_speed:
                warnings.append(f"Ignored {result.dropped_speed} GPS jump(s) that implied impossible speed")
        else:
            warnings.append("Not enough accurate GPS fixes to measure the route")

    if distance_m is not None:
        if isinstance(distance_m, bool) or not isinstance(distance_m, int) or distance_m < 0:
            raise MileageError("distance_m must be a non-negative integer (meters)")
        chosen, source = distance_m, "manual"
    elif odo is not None:
        chosen, source = odo, "odometer"
    elif route is not None:
        chosen, source = route, "gps"
    else:
        raise MileageError("No distance: provide distance_m, both odometer readings, or a GPS trace")

    if odo is not None and route is not None and max(odo, route) > 0:
        diff_bp = abs(odo - route) * 10_000 // max(odo, route)
        if diff_bp > ODOMETER_GPS_TOLERANCE_BP:
            warnings.append(f"Odometer ({odo} m) and GPS ({route} m) disagree by {diff_bp / 100:.1f}%")

    seconds = (ended_at - started_at).total_seconds()
    if seconds <= 0 and chosen > 0:
        raise MileageError("Trip must end after it starts")
    if seconds > 0 and chosen / seconds > MAX_PLAUSIBLE_SPEED_MPS:
        warnings.append("Average speed is implausibly high; check the distance")
    if chosen > MAX_PLAUSIBLE_TRIP_M:
        warnings.append("Unusually long single trip; check the distance")
    return chosen, source, warnings


def stop_trip(
    db: Any,
    trip_id: str,
    *,
    ended_at: datetime | None = None,
    distance_m: int | None = None,
    odometer_end_m: int | None = None,
    gps: Sequence[GpsFix] | None = None,
    end_location: str | None = None,
    check_maintenance: bool = True,
    clock: Callable[[], datetime] = utc_now,
) -> StopResult:
    trip = get_trip(db, trip_id)
    if trip.trip_state != "active":
        raise MileageError("Trip is not active")
    started = parse_iso(trip.started_at)
    ended = ended_at or clock()

    meters, source, warnings = resolve_distance(
        started_at=started, ended_at=ended, distance_m=distance_m,
        odometer_start_m=trip.odometer_start_m, odometer_end_m=odometer_end_m, gps=gps,
    )

    with db.transaction() as conn:
        conn.execute(
            """UPDATE mileage_logs SET ended_at = ?, distance_m = ?, odometer_end_m = ?, end_location = ?,
                   trip_state = 'completed', distance_source = ?, source = ?
               WHERE id = ?""",
            (to_iso_utc(ended), meters, odometer_end_m, end_location or trip.end_location, source,
             "gps" if source == "gps" else "manual", trip_id),
        )
        if trip.vehicle_id:
            _advance_odometer(conn, trip.vehicle_id, odometer_end_m, meters, ended)

    alerts: tuple[Any, ...] = ()
    if check_maintenance and trip.vehicle_id:
        from core.fleet.maintenance import due_for_vehicle

        alerts = tuple(d for d in due_for_vehicle(db, trip.vehicle_id, today=ended.date()) if d.status != "ok")
    return StopResult(get_trip(db, trip_id), tuple(warnings), alerts)


def _advance_odometer(conn: Any, vehicle_id: str, reading: int | None, meters: int, when: datetime) -> None:
    """Record a new odometer reading, or estimate one from the trip distance."""
    row = conn.execute("SELECT odometer_m FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone()
    current = row["odometer_m"]
    new = max(current, reading) if reading is not None else current + meters
    if new != current:
        conn.execute("UPDATE vehicles SET odometer_m = ?, odometer_updated_at = ? WHERE id = ?",
                     (new, to_iso_utc(when), vehicle_id))


def cancel_trip(db: Any, trip_id: str) -> None:
    trip = get_trip(db, trip_id)
    if trip.trip_state != "active":
        raise MileageError("Only active trips can be cancelled; delete completed trips instead")
    with db.transaction() as conn:
        conn.execute("UPDATE mileage_logs SET deleted_at = ? WHERE id = ?", (to_iso_utc(utc_now()), trip_id))


def log_trip(
    db: Any,
    *,
    started_at: datetime,
    distance_m: int,
    ended_at: datetime | None = None,
    vehicle_id: str | None = None,
    platform_id: str | None = None,
    purpose: Purpose = "business",
    description: str | None = None,
    start_location: str | None = None,
    end_location: str | None = None,
    device_id: str | None = None,
) -> Trip:
    """Record a completed trip after the fact (manual entry)."""
    if isinstance(distance_m, bool) or not isinstance(distance_m, int) or distance_m < 0:
        raise MileageError("distance_m must be a non-negative integer (meters)")
    if purpose not in ("business", "commute", "personal"):
        raise MileageError(f"Invalid purpose '{purpose}'")
    if ended_at is not None and ended_at < started_at:
        raise MileageError("Trip must end after it starts")
    trip_id = str(uuid.uuid4())
    with db.transaction() as conn:
        if vehicle_id:
            _vehicle_row(conn, vehicle_id)
        conn.execute(
            """INSERT INTO mileage_logs (id, vehicle_id, platform_id, started_at, ended_at, distance_m, purpose,
                   description, start_location, end_location, source, trip_state, distance_source, last_modified_device)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', 'completed', 'manual', ?)""",
            (trip_id, vehicle_id, platform_id, to_iso_utc(started_at),
             to_iso_utc(ended_at) if ended_at else None, distance_m, purpose, description,
             start_location, end_location, device_id),
        )
    return get_trip(db, trip_id)


# ---------------------------------------------------------------------------
# IRS standard mileage calculator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TripLine:
    trip_id: str
    local_date: date
    meters: int
    rate_deci_minor: int           # 725 = 72.5¢/mi
    deduction_minor: int           # this trip alone, rounded (display)
    depreciation_minor: int        # basis-reduction portion, rounded (display)

    @property
    def miles(self) -> Decimal:
        return (Decimal(self.meters) / METERS_PER_MILE).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class RatePeriod:
    effective_from: date
    rate_deci_minor: int
    meters: int
    deduction_minor: int


@dataclass
class MileageReport:
    tax_year: int
    timezone: str
    lines: list[TripLine] = field(default_factory=list)
    periods: list[RatePeriod] = field(default_factory=list)
    total_deduction_minor: int = 0       # official figure (per-period rounding)
    depreciation_minor: int = 0          # basis reduction for the year
    business_meters: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def business_miles(self) -> Decimal:
        return (Decimal(self.business_meters) / METERS_PER_MILE).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

    @property
    def per_trip_total_minor(self) -> int:
        return sum(line.deduction_minor for line in self.lines)


def _per_mile(meters: int, deci_minor: int) -> int:
    value = Decimal(meters) * deci_minor / (METERS_PER_MILE * 10)
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


class IRSMileageCalculator:
    """Standard mileage deduction with effective-dated (split-year) rates."""

    def __init__(self, registry: TaxTableRegistry | None = None) -> None:
        self.registry = registry or TaxTableRegistry.load()

    def table(self, tax_year: int) -> tuple[TaxTable, bool]:
        return self.registry.get("US", tax_year)

    def rate_on(self, day: date) -> int:
        """Rate in tenths of a cent per mile for a trip on `day`."""
        return self.table(day.year)[0].mileage_rate_on(day).rate_per_mile_deci_minor

    def deduction_for(self, meters: int, day: date) -> int:
        """Deduction for a single trip, in cents."""
        return mileage_deduction({day: meters}, self.table(day.year)[0])

    def calculate(self, tax_year: int, trips: Iterable[tuple[str, date, int]], *, timezone: str = "UTC") -> MileageReport:
        """`trips` are (trip_id, local_date, meters) for business trips."""
        table, provisional = self.table(tax_year)
        report = MileageReport(tax_year, timezone)
        if provisional:
            report.warnings.append(
                f"No {tax_year} IRS rates in tables yet; using {table.tax_year} rates (provisional)"
            )

        meters_by_day: dict[date, int] = {}
        dep_by_rate: dict[int, int] = {}
        for trip_id, day, meters in trips:
            if day.year != tax_year:
                raise MileageError(f"Trip {trip_id} on {day} is outside tax year {tax_year}")
            if meters < 0:
                raise MileageError(f"Trip {trip_id} has negative distance")
            rate = table.mileage_rate_on(day).rate_per_mile_deci_minor
            dep_rate = (table.mileage_depreciation_rate_on(day).rate_per_mile_deci_minor
                        if table.mileage_depreciation_rates else 0)
            report.lines.append(TripLine(trip_id, day, meters, rate, _per_mile(meters, rate), _per_mile(meters, dep_rate)))
            meters_by_day[day] = meters_by_day.get(day, 0) + meters
            dep_by_rate[dep_rate] = dep_by_rate.get(dep_rate, 0) + meters

        report.lines.sort(key=lambda l: (l.local_date, l.trip_id))
        report.business_meters = sum(meters_by_day.values())
        report.total_deduction_minor = mileage_deduction(meters_by_day, table) if meters_by_day else 0
        report.depreciation_minor = sum(_per_mile(m, r) for r, m in dep_by_rate.items())

        by_period: dict[date, list[int]] = {}
        for day, meters in meters_by_day.items():
            rate = table.mileage_rate_on(day)
            by_period.setdefault(rate.effective_from, [rate.rate_per_mile_deci_minor, 0])[1] += meters
        report.periods = [
            RatePeriod(start, rate, meters, _per_mile(meters, rate))
            for start, (rate, meters) in sorted(by_period.items())
        ]
        return report

    def report_for_year(
        self,
        db: Any,
        tax_year: int,
        *,
        timezone: str = "UTC",
        vehicle_id: str | None = None,
        platform_id: str | None = None,
    ) -> MileageReport:
        """Business trips from the log, dated in the user's timezone."""
        sql = ("SELECT id, started_at, distance_m FROM mileage_logs WHERE deleted_at IS NULL "
               "AND purpose = 'business' AND trip_state = 'completed' AND started_at >= ? AND started_at < ?")
        params: list[Any] = [f"{tax_year - 1}-12-31", f"{tax_year + 1}-01-02"]
        if vehicle_id is not None:
            sql += " AND vehicle_id = ?"
            params.append(vehicle_id)
        if platform_id is not None:
            sql += " AND platform_id = ?"
            params.append(platform_id)
        with db.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        trips = []
        for row in rows:
            day = local_date(row["started_at"], timezone)
            if day.year == tax_year:
                trips.append((row["id"], day, int(row["distance_m"])))
        return self.calculate(tax_year, trips, timezone=timezone)
