"""Fleet & mileage view-model: active trip, vehicles, maintenance, trips to review."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from core.config import UnitSystem
from core.fleet.maintenance import due_for_vehicle
from core.fleet.mileage import IRSMileageCalculator, Trip, get_active_trip
from core.fleet.vehicle import list_vehicles, resolve_vehicle
from core.timeutil import parse_iso
from ui.format import distance, local_time, money


@dataclass(frozen=True)
class VehicleRow:
    id: str
    label: str
    odometer: str
    overdue: tuple[str, ...]
    due_soon: tuple[str, ...]


@dataclass(frozen=True)
class TripRow:
    id: str
    when: str
    distance: str
    purpose: str
    auto: bool
    needs_review: bool


@dataclass(frozen=True)
class FleetData:
    active_trip: Trip | None
    default_vehicle_id: str | None
    vehicles: tuple[VehicleRow, ...]
    recent: tuple[TripRow, ...]
    review: tuple[TripRow, ...]
    ytd_deduction: str
    ytd_distance: str
    rate_lines: tuple[str, ...]


def _trip_row(r: Any, tz: str, units: UnitSystem) -> TripRow:
    return TripRow(r["id"], local_time(parse_iso(r["started_at"]), tz), distance(r["distance_m"], units),
                   r["purpose"], r["detection"] == "auto", r["review_status"] == "needs_review")


def load_fleet(db: Any, *, today: date, tz: str, units: UnitSystem, currency: str,
               calc: IRSMileageCalculator | None = None) -> FleetData:
    vehicles = []
    for v in list_vehicles(db):
        due = due_for_vehicle(db, v.id, today=today)
        vehicles.append(VehicleRow(
            v.id, v.label, distance(v.odometer_m, units, decimals=0),
            tuple(d.item.service for d in due if d.status == "overdue"),
            tuple(d.item.service for d in due if d.status == "due_soon"),
        ))
    default_vehicle = resolve_vehicle(db, on=today)
    active = get_active_trip(db, default_vehicle) or get_active_trip(db, None)

    with db.connection() as conn:
        recent = conn.execute(
            "SELECT id, started_at, distance_m, purpose, detection, review_status FROM mileage_logs "
            "WHERE deleted_at IS NULL AND trip_state = 'completed' ORDER BY started_at DESC LIMIT 15"
        ).fetchall()
        review = conn.execute(
            "SELECT id, started_at, distance_m, purpose, detection, review_status FROM mileage_logs "
            "WHERE deleted_at IS NULL AND trip_state = 'completed' AND review_status = 'needs_review' "
            "ORDER BY started_at DESC"
        ).fetchall()

    report = (calc or IRSMileageCalculator()).report_for_year(db, today.year, timezone=tz)
    rate_lines = tuple(
        f"From {p.effective_from:%b} {p.effective_from.day}: {distance(p.meters, units)} at "
        f"{p.rate_deci_minor // 10}.{p.rate_deci_minor % 10}¢ = {money(p.deduction_minor, currency)}"
        for p in report.periods
    )
    return FleetData(
        active, default_vehicle, tuple(vehicles),
        tuple(_trip_row(r, tz, units) for r in recent), tuple(_trip_row(r, tz, units) for r in review),
        money(report.total_deduction_minor, currency), distance(report.business_meters, units), rate_lines,
    )
