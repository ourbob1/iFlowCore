"""
Preventative maintenance scheduling and triggers.

Schedules live per vehicle in `maintenance_schedules` (imported from DataOne,
entered by the user, or a generic fallback); completed work lives in
`maintenance_records`. `due_for_vehicle` evaluates every schedule item by
distance and by time, whichever comes first:

    overdue   — the interval has been reached
    due_soon  — within `soon_meters` (default 500 mi) or `soon_days` (30)
    ok

Items never serviced are measured from when tracking began (the vehicle's
`baseline_odometer_m` and acquisition date), so adding a used car at 60,000
miles doesn't flag everything as overdue. Vehicles marked `severe_service`
(the default — rideshare and delivery driving is the textbook severe duty
cycle) use the shorter severe intervals where the schedule has them.

`stop_trip` calls `due_for_vehicle` after every trip, which is what makes
these alerts fire automatically.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Iterable, Literal

from core.financial.expenses import create_expense
from core.fleet.models import DueItem, MaintenanceItem, maintenance_due, miles_to_meters
from core.timeutil import parse_iso

Source = Literal["dataone", "user", "generic"]
DEFAULT_SOON_METERS = miles_to_meters(500)
DEFAULT_SOON_DAYS = 30


class MaintenanceError(ValueError):
    pass


def generic_schedule(fuel_type: str | None = None) -> tuple[MaintenanceItem, ...]:
    """Conservative industry-typical intervals, used only when no OEM schedule
    is available. Always defer to the owner's manual."""
    m = miles_to_meters
    common = (
        MaintenanceItem("Tire rotation", "tires", m(7_500), None, m(5_000), None),
        MaintenanceItem("Cabin air filter", "hvac", m(20_000), 12, m(15_000), 12),
        MaintenanceItem("Brake inspection", "brakes", m(15_000), 12, m(7_500), 6),
        MaintenanceItem("Wiper blades", "body", None, 12, None, 6),
    )
    if fuel_type == "electric":
        return common
    return (
        MaintenanceItem("Engine oil & filter", "engine", m(7_500), 12, m(5_000), 6),
        MaintenanceItem("Engine air filter", "engine", m(30_000), None, m(15_000), None),
        MaintenanceItem("Coolant inspection", "engine", m(30_000), 24, m(15_000), 12),
        *common,
    )


def import_schedule(db: Any, vehicle_id: str, items: Iterable[MaintenanceItem], *, source: Source) -> int:
    """Insert or update schedule items by service name (case-insensitive).

    User-entered items are never overwritten by DataOne or generic imports.
    Returns the number of rows written.
    """
    written = 0
    with db.transaction() as conn:
        for item in items:
            existing = conn.execute(
                "SELECT id, source FROM maintenance_schedules WHERE vehicle_id = ? AND service = ? COLLATE NOCASE "
                "AND deleted_at IS NULL", (vehicle_id, item.service),
            ).fetchone()
            values = (item.category, item.interval_meters, item.interval_months, item.severe_interval_meters,
                      item.severe_interval_months)
            if existing:
                if existing["source"] == "user" and source != "user":
                    continue
                conn.execute(
                    """UPDATE maintenance_schedules SET category = ?, interval_meters = ?, interval_months = ?,
                           severe_interval_meters = ?, severe_interval_months = ?, source = ?, is_active = 1
                       WHERE id = ?""", (*values, source, existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO maintenance_schedules (id, vehicle_id, service, category, interval_meters,
                           interval_months, severe_interval_meters, severe_interval_months, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), vehicle_id, item.service.strip(), *values, source),
                )
            written += 1
    return written


def load_schedule(db: Any, vehicle_id: str) -> list[MaintenanceItem]:
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM maintenance_schedules WHERE vehicle_id = ? AND deleted_at IS NULL AND is_active = 1 "
            "ORDER BY service COLLATE NOCASE", (vehicle_id,),
        ).fetchall()
    return [
        MaintenanceItem(r["service"], r["category"], r["interval_meters"], r["interval_months"],
                        r["severe_interval_meters"], r["severe_interval_months"])
        for r in rows
    ]


def record_service(
    db: Any,
    vehicle_id: str,
    *,
    service: str,
    performed_on: date,
    odometer_m: int,
    cost_minor: int | None = None,
    currency: str = "USD",
    vendor: str | None = None,
    category: str = "general",
    notes: str | None = None,
    create_expense_row: bool = True,
    device_id: str | None = None,
) -> str:
    """Log completed work; advances the odometer and files a maintenance expense."""
    if not service.strip():
        raise MaintenanceError("service is required")
    if odometer_m < 0:
        raise MaintenanceError("Odometer cannot be negative")
    if cost_minor is not None and (isinstance(cost_minor, bool) or not isinstance(cost_minor, int) or cost_minor < 0):
        raise MaintenanceError("cost_minor must be a non-negative int")
    with db.connection() as conn:
        vehicle = conn.execute(
            "SELECT nickname, odometer_m, business_use_bp FROM vehicles WHERE id = ? AND deleted_at IS NULL",
            (vehicle_id,),
        ).fetchone()
        sched = conn.execute(
            "SELECT service, category FROM maintenance_schedules WHERE vehicle_id = ? AND service = ? COLLATE NOCASE "
            "AND deleted_at IS NULL", (vehicle_id, service.strip()),
        ).fetchone()
    if vehicle is None:
        raise MaintenanceError(f"No vehicle {vehicle_id}")
    # Use the schedule's spelling so records and schedule items always match.
    name = sched["service"] if sched else service.strip()
    if sched and category == "general":
        category = sched["category"]

    expense_id = None
    if create_expense_row and cost_minor:
        expense_id, _ = create_expense(
            db, amount_minor=cost_minor, currency=currency, occurred_at=performed_on.isoformat(),
            vendor=vendor, description=f"{name} — {vehicle['nickname']}", category="maintenance",
            business_use_bp=vehicle["business_use_bp"], device_id=device_id,
        )
    record_id = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO maintenance_records (id, vehicle_id, service, category, performed_on, odometer_m,
                   cost_minor, currency, vendor, expense_id, notes, last_modified_device)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record_id, vehicle_id, name, category, performed_on.isoformat(), odometer_m, cost_minor,
             currency.upper(), vendor, expense_id, notes, device_id),
        )
        if odometer_m > vehicle["odometer_m"]:
            conn.execute("UPDATE vehicles SET odometer_m = ?, odometer_updated_at = ? WHERE id = ?",
                         (odometer_m, performed_on.isoformat() + "T12:00:00.000Z", vehicle_id))
    return record_id


def last_services(db: Any, vehicle_id: str) -> dict[str, tuple[int, date]]:
    """service (casefolded) → (odometer_m, date) of the most recent record."""
    out: dict[str, tuple[int, date]] = {}
    with db.connection() as conn:
        for r in conn.execute(
            "SELECT service, odometer_m, performed_on FROM maintenance_records WHERE vehicle_id = ? "
            "AND deleted_at IS NULL ORDER BY performed_on, odometer_m", (vehicle_id,),
        ):
            out[r["service"].casefold()] = (r["odometer_m"], date.fromisoformat(r["performed_on"]))
    return out


def due_for_vehicle(
    db: Any,
    vehicle_id: str,
    *,
    today: date,
    severe: bool | None = None,
    soon_meters: int = DEFAULT_SOON_METERS,
    soon_days: int = DEFAULT_SOON_DAYS,
) -> list[DueItem]:
    with db.connection() as conn:
        v = conn.execute(
            "SELECT odometer_m, baseline_odometer_m, acquired_on, created_at, severe_service FROM vehicles "
            "WHERE id = ? AND deleted_at IS NULL", (vehicle_id,),
        ).fetchone()
    if v is None:
        raise MaintenanceError(f"No vehicle {vehicle_id}")
    schedule = load_schedule(db, vehicle_id)
    if not schedule:
        return []
    baseline_day = date.fromisoformat(v["acquired_on"]) if v["acquired_on"] else parse_iso(v["created_at"]).date()
    history = last_services(db, vehicle_id)
    last = {
        item.service: history.get(item.service.casefold(), (v["baseline_odometer_m"], baseline_day))
        for item in schedule
    }
    return maintenance_due(
        schedule, odometer_m=v["odometer_m"], today=today, last_service=last,
        severe=bool(v["severe_service"]) if severe is None else severe,
        soon_meters=soon_meters, soon_days=soon_days,
    )


def fleet_alerts(db: Any, *, today: date) -> dict[str, list[DueItem]]:
    """Non-ok items for every active vehicle, keyed by vehicle id."""
    with db.connection() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM vehicles WHERE deleted_at IS NULL AND is_active = 1")]
    out = {}
    for vid in ids:
        alerts = [d for d in due_for_vehicle(db, vid, today=today) if d.status != "ok"]
        if alerts:
            out[vid] = alerts
    return out


__all__ = [
    "MaintenanceError", "generic_schedule", "import_schedule", "load_schedule", "record_service",
    "last_services", "due_for_vehicle", "fleet_alerts",
]
