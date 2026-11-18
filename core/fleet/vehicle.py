"""
Vehicle assets and multi-vehicle fleet management.

* Registration — optionally decoded from the VIN through
  `core.fleet.decoder.VehicleDecoder` (NHTSA base + DataOne enrichment,
  encrypted cache, offline check-digit validation). Decoded maintenance
  schedules are imported automatically.
* Odometer — monotonic; trips, fuel logs and services all advance it.
* Fuel & energy — fill-ups in milliliters, charging in watt-hours, cost in
  minor units; fuel economy by the full-tank-to-full-tank method; each log can
  create the matching `expenses` row.
* Depreciation — MACRS 5-year (or ADS when business use ≤ 50%) capped by the
  §280F passenger-auto limits in the tax tables, and the standard-mileage
  basis reduction (depreciation portion of the IRS rate).
* Fleet assignment — which vehicle works which platform over which dates;
  `resolve_vehicle` picks the right car when a trip starts.

Units: meters, milliliters, watt-hours, minor units, basis points. Display
values (MPG, L/100 km, ¢/mi) are integers scaled ×10 so no floats leak out.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Sequence

from core.financial.expenses import create_expense
from core.financial.money import apply_bp
from core.financial.tax import METERS_PER_MILE, TaxTableRegistry
from core.fleet.models import VehicleProfile
from core.fleet.vin import validate_vin
from core.timeutil import to_iso_utc, utc_now

ML_PER_US_GALLON = Decimal("3785.411784")
METERS_PER_KM = Decimal(1000)

VehicleType = Literal["car", "truck", "van", "motorcycle", "scooter", "bicycle", "ebike", "other"]
MOTORIZED = frozenset({"car", "truck", "van", "motorcycle", "scooter"})


class FleetError(ValueError):
    pass


def _q(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def gallons_to_ml(gallons: str | Decimal | int) -> int:
    """US gallons (as text/Decimal, never float) → integer milliliters."""
    if isinstance(gallons, float):
        raise FleetError("Pass fuel volume as str or Decimal, not float")
    return _q(Decimal(gallons) * ML_PER_US_GALLON)


def liters_to_ml(liters: str | Decimal | int) -> int:
    if isinstance(liters, float):
        raise FleetError("Pass fuel volume as str or Decimal, not float")
    return _q(Decimal(liters) * 1000)


# ---------------------------------------------------------------------------
# Vehicles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Vehicle:
    id: str
    nickname: str
    vin: str | None
    vehicle_type: str
    year: int | None
    make: str | None
    model: str | None
    trim: str | None
    style_id: str | None
    fuel_type: str | None
    rated_mpg_combined: int | None
    equipment: frozenset[str]
    odometer_m: int
    baseline_odometer_m: int
    acquired_on: str | None
    placed_in_service_on: str | None
    purchase_price_minor: int | None
    currency: str
    business_use_bp: int
    deduction_method: str
    severe_service: bool
    is_active: bool
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> "Vehicle":
        data = {k: row[k] for k in cls.__dataclass_fields__ if k != "equipment"}
        data["equipment"] = frozenset(json.loads(row["equipment_json"] or "[]"))
        data["severe_service"] = bool(row["severe_service"])
        data["is_active"] = bool(row["is_active"])
        return cls(**data)

    @property
    def label(self) -> str:
        desc = " ".join(str(p) for p in (self.year, self.make, self.model, self.trim) if p)
        return f"{self.nickname} ({desc})" if desc else self.nickname


def get_vehicle(db: Any, vehicle_id: str) -> Vehicle:
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM vehicles WHERE id = ? AND deleted_at IS NULL", (vehicle_id,)).fetchone()
    if row is None:
        raise FleetError(f"No vehicle {vehicle_id}")
    return Vehicle.from_row(row)


def list_vehicles(db: Any, *, active_only: bool = True) -> list[Vehicle]:
    sql = "SELECT * FROM vehicles WHERE deleted_at IS NULL"
    if active_only:
        sql += " AND is_active = 1"
    with db.connection() as conn:
        return [Vehicle.from_row(r) for r in conn.execute(sql + " ORDER BY nickname COLLATE NOCASE")]


def register_vehicle(
    db: Any,
    *,
    nickname: str,
    vin: str | None = None,
    decoder: Any | None = None,            # core.fleet.decoder.VehicleDecoder
    style_id: str | None = None,
    trim_hint: str | None = None,
    vehicle_type: VehicleType = "car",
    year: int | None = None,
    make: str | None = None,
    model: str | None = None,
    trim: str | None = None,
    fuel_type: str | None = None,
    odometer_m: int = 0,
    acquired_on: date | None = None,
    placed_in_service_on: date | None = None,
    purchase_price_minor: int | None = None,
    currency: str = "USD",
    business_use_bp: int = 10_000,
    deduction_method: Literal["standard_mileage", "actual"] = "standard_mileage",
    severe_service: bool = True,
    import_maintenance: bool = True,
    require_check_digit: bool = True,
    device_id: str | None = None,
) -> tuple[Vehicle, VehicleProfile | None]:
    """Create a vehicle, decoding its VIN when a decoder is supplied.

    Explicit arguments win over decoded values. Maintenance schedules come
    from DataOne when available, otherwise a generic schedule is installed.
    """
    if odometer_m < 0:
        raise FleetError("Odometer cannot be negative")
    if purchase_price_minor is not None and (isinstance(purchase_price_minor, bool) or not isinstance(purchase_price_minor, int)):
        raise FleetError("purchase_price_minor must be int minor units")
    if vin is not None:
        vin = validate_vin(vin, require_check_digit=require_check_digit)

    profile: VehicleProfile | None = None
    if vin and decoder is not None:
        profile = decoder.decode(vin, style_id=style_id, trim_hint=trim_hint)

    def pick(explicit: Any, decoded: Any) -> Any:
        return explicit if explicit is not None else decoded

    values = {
        "year": pick(year, profile.year if profile else None),
        "make": pick(make, profile.make if profile else None),
        "model": pick(model, profile.model if profile else None),
        "trim": pick(trim, profile.trim if profile else None),
        "style_id": profile.style_id if profile else None,
        "fuel_type": pick(fuel_type, profile.fuel_type if profile else ("none" if vehicle_type == "bicycle" else None)),
        "rated_mpg_combined": profile.mpg_combined if profile else None,
        "equipment_json": json.dumps(sorted(profile.equipment)) if profile else "[]",
    }
    vehicle_id = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO vehicles (id, nickname, vin, vehicle_type, year, make, model, trim, style_id, fuel_type,
                   rated_mpg_combined, equipment_json, odometer_m, odometer_updated_at, baseline_odometer_m,
                   acquired_on, placed_in_service_on, purchase_price_minor, currency, business_use_bp,
                   deduction_method, severe_service, last_modified_device)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (vehicle_id, nickname.strip(), vin, vehicle_type, values["year"], values["make"], values["model"],
             values["trim"], values["style_id"], values["fuel_type"], values["rated_mpg_combined"],
             values["equipment_json"], odometer_m, to_iso_utc(utc_now()), odometer_m,
             acquired_on.isoformat() if acquired_on else None,
             placed_in_service_on.isoformat() if placed_in_service_on else None,
             purchase_price_minor, currency.upper(), business_use_bp, deduction_method,
             int(severe_service), device_id),
        )

    if import_maintenance:
        from core.fleet.maintenance import generic_schedule, import_schedule

        if profile is not None and profile.maintenance:
            import_schedule(db, vehicle_id, profile.maintenance, source="dataone")
        elif vehicle_type in MOTORIZED:
            import_schedule(db, vehicle_id, generic_schedule(values["fuel_type"]), source="generic")
    return get_vehicle(db, vehicle_id), profile


def update_odometer(db: Any, vehicle_id: str, odometer_m: int, *, at: datetime | None = None,
                    allow_correction: bool = False) -> Vehicle:
    """Record a reading. Lower readings are refused unless `allow_correction`."""
    vehicle = get_vehicle(db, vehicle_id)
    if odometer_m < 0:
        raise FleetError("Odometer cannot be negative")
    if odometer_m < vehicle.odometer_m and not allow_correction:
        raise FleetError(f"Odometer {odometer_m} m is below the last reading ({vehicle.odometer_m} m)")
    with db.transaction() as conn:
        conn.execute("UPDATE vehicles SET odometer_m = ?, odometer_updated_at = ? WHERE id = ?",
                     (odometer_m, to_iso_utc(at or utc_now()), vehicle_id))
    return get_vehicle(db, vehicle_id)


def retire_vehicle(db: Any, vehicle_id: str, *, on: date) -> None:
    """Deactivate a vehicle and close its open assignments."""
    with db.transaction() as conn:
        conn.execute("UPDATE vehicles SET is_active = 0 WHERE id = ?", (vehicle_id,))
        conn.execute(
            "UPDATE vehicle_assignments SET ends_on = ? WHERE vehicle_id = ? AND ends_on IS NULL AND deleted_at IS NULL",
            (on.isoformat(), vehicle_id),
        )


# ---------------------------------------------------------------------------
# Fuel & energy
# ---------------------------------------------------------------------------


def log_fuel(
    db: Any,
    vehicle_id: str,
    *,
    filled_at: datetime,
    odometer_m: int,
    total_cost_minor: int,
    currency: str = "USD",
    volume_ml: int | None = None,
    energy_wh: int | None = None,
    is_full: bool = True,
    station: str | None = None,
    create_expense_row: bool = True,
    device_id: str | None = None,
) -> str:
    """Record a fill-up or charging session; optionally file it as a fuel expense.

    For vehicles on the standard mileage method the expense is kept for
    records but excluded from deductions by the tax engine (as the IRS
    requires).
    """
    if (volume_ml is None) == (energy_wh is None):
        raise FleetError("Provide exactly one of volume_ml (fuel) or energy_wh (charging)")
    for name, value in (("total_cost_minor", total_cost_minor), ("odometer_m", odometer_m)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise FleetError(f"{name} must be a non-negative int")
    vehicle = get_vehicle(db, vehicle_id)
    # Back-dated receipts may sit below the current odometer, but never below
    # the reading when tracking began.
    if odometer_m < vehicle.baseline_odometer_m:
        raise FleetError("Fuel log odometer is below the vehicle's baseline reading")

    expense_id = None
    if create_expense_row and total_cost_minor > 0:
        expense_id, _ = create_expense(
            db, amount_minor=total_cost_minor, currency=currency, occurred_at=to_iso_utc(filled_at),
            vendor=station, description=f"{'Charging' if energy_wh else 'Fuel'} — {vehicle.nickname}",
            category="fuel", business_use_bp=vehicle.business_use_bp, device_id=device_id,
        )
    log_id = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO fuel_logs (id, vehicle_id, filled_at, odometer_m, volume_ml, energy_wh, total_cost_minor,
                   currency, is_full, station, expense_id, last_modified_device)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (log_id, vehicle_id, to_iso_utc(filled_at), odometer_m, volume_ml, energy_wh, total_cost_minor,
             currency.upper(), int(is_full), station, expense_id, device_id),
        )
        if odometer_m > vehicle.odometer_m:
            conn.execute("UPDATE vehicles SET odometer_m = ?, odometer_updated_at = ? WHERE id = ?",
                         (odometer_m, to_iso_utc(filled_at), vehicle_id))
    return log_id


@dataclass(frozen=True)
class FuelEconomy:
    distance_m: int
    volume_ml: int
    energy_wh: int
    cost_minor: int
    currency: str
    segments: int

    @property
    def mpg_x10(self) -> int | None:
        if not self.volume_ml:
            return None
        gallons = Decimal(self.volume_ml) / ML_PER_US_GALLON
        return _q(Decimal(self.distance_m) / METERS_PER_MILE / gallons * 10)

    @property
    def l_per_100km_x10(self) -> int | None:
        if not self.volume_ml or not self.distance_m:
            return None
        return _q(Decimal(self.volume_ml) / 1000 / (Decimal(self.distance_m) / 100_000) * 10)

    @property
    def mi_per_kwh_x10(self) -> int | None:
        if not self.energy_wh:
            return None
        return _q(Decimal(self.distance_m) / METERS_PER_MILE / (Decimal(self.energy_wh) / 1000) * 10)

    @property
    def cost_per_mile_deci_minor(self) -> int | None:
        """Fuel cost per mile in tenths of a minor unit (123 = 12.3¢/mi)."""
        if not self.distance_m:
            return None
        return _q(Decimal(self.cost_minor) * 10 / (Decimal(self.distance_m) / METERS_PER_MILE))

    @property
    def cost_per_km_deci_minor(self) -> int | None:
        if not self.distance_m:
            return None
        return _q(Decimal(self.cost_minor) * 10 / (Decimal(self.distance_m) / METERS_PER_KM))


def fuel_economy(db: Any, vehicle_id: str, *, since: datetime | None = None, until: datetime | None = None) -> FuelEconomy | None:
    """Full-tank-to-full-tank economy.

    The first full fill is the anchor; every later fill's volume (partial
    ones included) counts toward the next full fill, whose odometer closes the
    segment. Fuel bought before the anchor is excluded because the distance
    it covered is unknown. Returns None until two full fills exist.
    """
    sql = ("SELECT odometer_m, volume_ml, energy_wh, total_cost_minor, currency, is_full FROM fuel_logs "
           "WHERE vehicle_id = ? AND deleted_at IS NULL")
    params: list[Any] = [vehicle_id]
    if since:
        sql += " AND filled_at >= ?"
        params.append(to_iso_utc(since))
    if until:
        sql += " AND filled_at < ?"
        params.append(to_iso_utc(until))
    with db.connection() as conn:
        rows = conn.execute(sql + " ORDER BY odometer_m, filled_at", params).fetchall()

    anchor = None
    pending_ml = pending_wh = pending_cost = 0
    dist = ml = wh = cost = segments = 0
    currencies = set()
    for r in rows:
        if anchor is None:
            if r["is_full"]:
                anchor = r["odometer_m"]
            continue
        pending_ml += r["volume_ml"] or 0
        pending_wh += r["energy_wh"] or 0
        pending_cost += r["total_cost_minor"]
        currencies.add(r["currency"])
        if r["is_full"]:
            dist += r["odometer_m"] - anchor
            ml, wh, cost = ml + pending_ml, wh + pending_wh, cost + pending_cost
            pending_ml = pending_wh = pending_cost = 0
            anchor = r["odometer_m"]
            segments += 1
    if not segments or dist <= 0:
        return None
    if len(currencies) > 1:
        raise FleetError("Fuel logs mix currencies; convert before computing cost per distance")
    return FuelEconomy(dist, ml, wh, cost, currencies.pop(), segments)


# ---------------------------------------------------------------------------
# Depreciation
# ---------------------------------------------------------------------------

# Rev. Proc. 87-57 Table A-1: 5-year property, 200% DB, half-year convention.
MACRS_5YR_HY_BP = (2000, 3200, 1920, 1152, 1152, 576)
# ADS straight-line 5-year, half-year (required when business use ≤ 50%).
ADS_5YR_SL_HY_BP = (1000, 2000, 2000, 2000, 2000, 1000)


@dataclass(frozen=True)
class DepreciationYear:
    tax_year: int
    rate_bp: int | None             # None once past the recovery table
    tentative_minor: int
    cap_minor: int                  # §280F limit × business use
    deduction_minor: int
    remaining_basis_minor: int


@dataclass
class DepreciationSchedule:
    method: str
    business_basis_minor: int
    years: list[DepreciationYear] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_minor(self) -> int:
        return sum(y.deduction_minor for y in self.years)


def auto_depreciation_limits(placed_in_service_year: int, registry: TaxTableRegistry | None = None) -> tuple[tuple[int, int, int, int], bool]:
    """(1st, 2nd, 3rd, succeeding) §280F limits without bonus, and a provisional flag."""
    registry = registry or TaxTableRegistry.load()
    table, provisional = registry.get("US", placed_in_service_year)
    limits = table.auto_depreciation_limits
    if not limits:
        raise FleetError(f"No passenger-auto depreciation limits for {placed_in_service_year}")
    return tuple(int(v) for v in limits["no_bonus"]), provisional  # type: ignore[return-value]


def macrs_schedule(
    cost_minor: int,
    placed_in_service_year: int,
    *,
    business_use_bp: int = 10_000,
    limits: Sequence[int] | None = None,
    registry: TaxTableRegistry | None = None,
    passenger_auto: bool = True,
    max_years: int = 40,
) -> DepreciationSchedule:
    """Actual-expense-method depreciation for a vehicle.

    MACRS 5-year 200% DB half-year when business use > 50%, else ADS
    straight-line (listed-property rule). Passenger autos are capped each year
    by the §280F limits × business-use share; basis left after year 6 is
    recovered at the "succeeding year" limit. Assumes constant business use,
    no bonus depreciation or §179, and the half-year convention (mid-quarter
    applies if >40% of the year's property is placed in service in Q4).
    """
    if isinstance(cost_minor, bool) or not isinstance(cost_minor, int) or cost_minor < 0:
        raise FleetError("cost_minor must be a non-negative int")
    if not 0 <= business_use_bp <= 10_000:
        raise FleetError("business_use_bp must be 0..10000")
    warnings: list[str] = []
    qualified = business_use_bp > 5_000
    table_bp = MACRS_5YR_HY_BP if qualified else ADS_5YR_SL_HY_BP
    method = "MACRS 5-yr 200DB HY" if qualified else "ADS 5-yr SL HY (business use ≤ 50%)"

    if passenger_auto and limits is None:
        limits, provisional = auto_depreciation_limits(placed_in_service_year, registry)
        if provisional:
            warnings.append(f"§280F limits for {placed_in_service_year} not in tables; using latest available (provisional)")

    basis = apply_bp(cost_minor, business_use_bp)
    remaining = basis
    schedule = DepreciationSchedule(method, basis, warnings=warnings)
    for i in range(max_years):
        if remaining <= 0:
            break
        rate = table_bp[i] if i < len(table_bp) else None
        tentative = apply_bp(basis, rate) if rate is not None else remaining
        if passenger_auto and limits is not None:
            limit = limits[min(i, 3)]
            cap = apply_bp(limit, business_use_bp)
        else:
            if rate is None:
                break
            cap = tentative
        deduction = min(tentative, cap, remaining)
        remaining -= deduction
        schedule.years.append(DepreciationYear(placed_in_service_year + i, rate, tentative, cap, deduction, remaining))
    return schedule


# ---------------------------------------------------------------------------
# Fleet assignment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Assignment:
    id: str
    vehicle_id: str
    platform_id: str | None
    starts_on: date
    ends_on: date | None

    def covers(self, day: date) -> bool:
        return self.starts_on <= day and (self.ends_on is None or day <= self.ends_on)


def _assignment(row: Any) -> Assignment:
    return Assignment(row["id"], row["vehicle_id"], row["platform_id"], date.fromisoformat(row["starts_on"]),
                      date.fromisoformat(row["ends_on"]) if row["ends_on"] else None)


def list_assignments(db: Any, *, platform_id: str | None = None, any_platform: bool = True) -> list[Assignment]:
    sql = "SELECT * FROM vehicle_assignments WHERE deleted_at IS NULL"
    params: list[Any] = []
    if not any_platform:
        sql += " AND ifnull(platform_id, '') = ifnull(?, '')"
        params.append(platform_id)
    with db.connection() as conn:
        return [_assignment(r) for r in conn.execute(sql + " ORDER BY starts_on", params)]


def assign_vehicle(
    db: Any,
    vehicle_id: str,
    *,
    starts_on: date,
    platform_id: str | None = None,
    ends_on: date | None = None,
    close_open: bool = True,
) -> str:
    """Assign a vehicle to a platform (or as the default when platform_id is None).

    Each platform slot has one vehicle at a time. With `close_open`, an
    open-ended earlier assignment is closed the day before `starts_on`; any
    other overlap is an error. One vehicle may serve several platforms at once
    (multi-apping).
    """
    if ends_on is not None and ends_on < starts_on:
        raise FleetError("ends_on is before starts_on")
    vehicle = get_vehicle(db, vehicle_id)
    if not vehicle.is_active:
        raise FleetError("Cannot assign an inactive vehicle")
    new_id = str(uuid.uuid4())
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM vehicle_assignments WHERE deleted_at IS NULL AND ifnull(platform_id, '') = ifnull(?, '')",
            (platform_id,),
        ).fetchall()
        for other in map(_assignment, rows):
            overlaps = other.starts_on <= (ends_on or date.max) and starts_on <= (other.ends_on or date.max)
            if not overlaps:
                continue
            if close_open and other.ends_on is None and other.starts_on < starts_on:
                conn.execute("UPDATE vehicle_assignments SET ends_on = ? WHERE id = ?",
                             ((starts_on - timedelta(days=1)).isoformat(), other.id))
                continue
            raise FleetError(
                f"Overlaps assignment {other.id} ({other.starts_on} – {other.ends_on or 'open'}) for this platform"
            )
        conn.execute(
            "INSERT INTO vehicle_assignments (id, vehicle_id, platform_id, starts_on, ends_on) VALUES (?, ?, ?, ?, ?)",
            (new_id, vehicle_id, platform_id, starts_on.isoformat(), ends_on.isoformat() if ends_on else None),
        )
    return new_id


def resolve_vehicle(db: Any, *, platform_id: str | None = None, on: date) -> str | None:
    """Vehicle to use for work on `on`: platform assignment → default
    assignment → the only active vehicle → None."""
    with db.connection() as conn:
        rows = conn.execute(
            """SELECT a.* FROM vehicle_assignments a JOIN vehicles v ON v.id = a.vehicle_id
               WHERE a.deleted_at IS NULL AND v.deleted_at IS NULL AND v.is_active = 1
                 AND a.starts_on <= ? AND (a.ends_on IS NULL OR a.ends_on >= ?)""",
            (on.isoformat(), on.isoformat()),
        ).fetchall()
        active = [r["id"] for r in conn.execute("SELECT id FROM vehicles WHERE deleted_at IS NULL AND is_active = 1")]
    assignments = [_assignment(r) for r in rows]
    if platform_id is not None:
        for a in assignments:
            if a.platform_id == platform_id:
                return a.vehicle_id
    for a in assignments:
        if a.platform_id is None:
            return a.vehicle_id
    return active[0] if len(active) == 1 else None


# ---------------------------------------------------------------------------
# Per-vehicle yearly cost summary (standard vs actual comparison)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VehicleYearSummary:
    vehicle_id: str
    tax_year: int
    business_meters: int
    total_meters: int
    fuel_cost_minor: int
    maintenance_cost_minor: int
    standard_mileage_deduction_minor: int
    standard_mileage_basis_reduction_minor: int
    actual_operating_deduction_minor: int     # fuel + maintenance × business share
    warnings: tuple[str, ...] = ()

    @property
    def business_share_bp(self) -> int:
        return self.business_meters * 10_000 // self.total_meters if self.total_meters else 0


def vehicle_year_summary(db: Any, vehicle_id: str, tax_year: int, *, timezone: str = "UTC",
                         registry: TaxTableRegistry | None = None) -> VehicleYearSummary:
    """Side-by-side numbers for choosing standard mileage vs actual expenses.

    The actual-method figure here covers operating costs only; add
    `macrs_schedule(...)` depreciation, insurance, registration, and interest
    for a full comparison.
    """
    from core.fleet.mileage import IRSMileageCalculator
    from core.timeutil import local_date

    calc = IRSMileageCalculator(registry)
    report = calc.report_for_year(db, tax_year, timezone=timezone, vehicle_id=vehicle_id)
    with db.connection() as conn:
        total = 0
        for row in conn.execute(
            "SELECT started_at, distance_m FROM mileage_logs WHERE vehicle_id = ? AND deleted_at IS NULL "
            "AND trip_state = 'completed' AND started_at >= ? AND started_at < ?",
            (vehicle_id, f"{tax_year - 1}-12-31", f"{tax_year + 1}-01-02"),
        ):
            if local_date(row["started_at"], timezone).year == tax_year:
                total += row["distance_m"]
        fuel = conn.execute(
            "SELECT ifnull(sum(total_cost_minor), 0) FROM fuel_logs WHERE vehicle_id = ? AND deleted_at IS NULL "
            "AND filled_at >= ? AND filled_at < ?", (vehicle_id, f"{tax_year}-01-01", f"{tax_year + 1}-01-01"),
        ).fetchone()[0]
        maint = conn.execute(
            "SELECT ifnull(sum(cost_minor), 0) FROM maintenance_records WHERE vehicle_id = ? AND deleted_at IS NULL "
            "AND performed_on >= ? AND performed_on < ?", (vehicle_id, f"{tax_year}-01-01", f"{tax_year + 1}-01-01"),
        ).fetchone()[0]
    share = report.business_meters * 10_000 // total if total else 0
    return VehicleYearSummary(
        vehicle_id, tax_year, report.business_meters, total, fuel, maint,
        report.total_deduction_minor, report.depreciation_minor, apply_bp(fuel + maint, share),
        tuple(report.warnings),
    )


def fleet_overview(db: Any, *, on: date) -> list[dict[str, Any]]:
    """Dashboard rows: vehicle, today's platform assignments, odometer, due maintenance."""
    from core.fleet.maintenance import due_for_vehicle

    rows = []
    assignments = list_assignments(db)
    for v in list_vehicles(db):
        due = due_for_vehicle(db, v.id, today=on)
        rows.append({
            "vehicle": v,
            "platforms": sorted({a.platform_id or "*" for a in assignments if a.vehicle_id == v.id and a.covers(on)}),
            "odometer_m": v.odometer_m,
            "overdue": [d.item.service for d in due if d.status == "overdue"],
            "due_soon": [d.item.service for d in due if d.status == "due_soon"],
        })
    return rows

