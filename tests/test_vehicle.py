"""Vehicle assets, fuel economy, depreciation, fleet assignment, maintenance triggers."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from core.financial.tax import TaxTableRegistry, summarize_year
from core.fleet.cache import DecodeCache
from core.fleet.dataone import DataOneClient
from core.fleet.decoder import VehicleDecoder
from core.fleet.maintenance import (
    MaintenanceError,
    due_for_vehicle,
    fleet_alerts,
    generic_schedule,
    import_schedule,
    last_services,
    load_schedule,
    record_service,
)
from core.fleet.mileage import start_trip, stop_trip
from core.fleet.models import MaintenanceItem, miles_to_meters
from core.fleet.nhtsa import NHTSADecoder
from core.fleet.vehicle import (
    FleetError,
    assign_vehicle,
    auto_depreciation_limits,
    fleet_overview,
    fuel_economy,
    gallons_to_ml,
    get_vehicle,
    list_assignments,
    liters_to_ml,
    log_fuel,
    macrs_schedule,
    register_vehicle,
    resolve_vehicle,
    retire_vehicle,
    update_odometer,
    vehicle_year_summary,
)
from core.fleet.vin import VinError

from .test_fleet import CAMRY, CAMRY_TYPO, CREDS, FakeTransport, dataone_payload, no_sleep, ok, vpic_payload

UTC = timezone.utc
D = 100
mi = miles_to_meters


def car(db, **kw):
    kw.setdefault("nickname", "Camry")
    kw.setdefault("odometer_m", mi(40_000))
    kw.setdefault("acquired_on", date(2026, 1, 1))
    return register_vehicle(db, **kw)[0]


# --- registration & odometer ------------------------------------------------------------


def test_register_manual_installs_generic_schedule(db):
    v = car(db, year=2019, make="Toyota", model="Camry")
    assert v.label == "Camry (2019 Toyota Camry)"
    assert v.baseline_odometer_m == v.odometer_m == mi(40_000)
    services = {i.service for i in load_schedule(db, v.id)}
    assert "Engine oil & filter" in services and len(services) == len(generic_schedule())
    with db.connection() as conn:
        assert {r[0] for r in conn.execute("SELECT DISTINCT source FROM maintenance_schedules")} == {"generic"}


def test_ev_and_bicycle_schedules(db):
    ev = car(db, nickname="Model 3", fuel_type="electric")
    assert "Engine oil & filter" not in {i.service for i in load_schedule(db, ev.id)}
    bike = register_vehicle(db, nickname="Bike", vehicle_type="bicycle")[0]
    assert load_schedule(db, bike.id) == [] and bike.fuel_type == "none"


def test_register_with_decoder_imports_trim_equipment_and_oem_schedule(db):
    decoder = VehicleDecoder(
        nhtsa=NHTSADecoder(FakeTransport([ok(vpic_payload())]), sleep=no_sleep),
        dataone=DataOneClient(CREDS, transport=FakeTransport([ok(dataone_payload())]), sleep=no_sleep),
        cache=DecodeCache(db),
    )
    v, profile = register_vehicle(db, nickname="Work car", vin=CAMRY, decoder=decoder, trim_hint="XLE",
                                  odometer_m=mi(12_000))
    assert (v.year, v.make, v.model, v.trim, v.style_id) == (2019, "Toyota", "Camry", "XLE", "401738775")
    assert v.rated_mpg_combined == 32 and "heated_seats" in v.equipment
    schedule = {i.service: i for i in load_schedule(db, v.id)}
    assert set(schedule) == {"Engine oil & filter", "Rotate tires", "Cabin air filter"}
    assert schedule["Engine oil & filter"].severe_interval_meters == mi(5_000)
    assert profile is not None and profile.sources == ("nhtsa", "dataone")


def test_register_rejects_bad_vin_and_duplicates(db):
    with pytest.raises(VinError):
        car(db, vin=CAMRY_TYPO)
    car(db, vin=CAMRY)
    with pytest.raises(Exception):
        car(db, nickname="Other", vin=CAMRY)            # VIN unique among live vehicles
    with pytest.raises(Exception):
        car(db, nickname="camry")                        # nickname unique (case-insensitive)


def test_odometer_is_monotonic(db):
    v = car(db)
    assert update_odometer(db, v.id, v.odometer_m + 100).odometer_m == v.odometer_m + 100
    with pytest.raises(FleetError, match="below"):
        update_odometer(db, v.id, v.odometer_m)
    assert update_odometer(db, v.id, 5, allow_correction=True).odometer_m == 5


# --- fuel ---------------------------------------------------------------------------------


def test_volume_conversions():
    assert gallons_to_ml("1") == 3785
    assert gallons_to_ml("10.5") == 39747
    assert liters_to_ml("40.25") == 40250
    with pytest.raises(FleetError):
        gallons_to_ml(1.5)  # type: ignore[arg-type]


def test_fuel_economy_full_to_full(db):
    v = car(db, odometer_m=mi(10_000))
    t = datetime(2026, 9, 1, tzinfo=UTC)
    log_fuel(db, v.id, filled_at=t, odometer_m=mi(10_000), volume_ml=gallons_to_ml("12"), total_cost_minor=4_200)
    assert fuel_economy(db, v.id) is None                               # need two full fills
    log_fuel(db, v.id, filled_at=t + timedelta(days=2), odometer_m=mi(10_000) + miles_to_meters(150),
             volume_ml=gallons_to_ml("5"), total_cost_minor=3_500, is_full=False)
    log_fuel(db, v.id, filled_at=t + timedelta(days=4), odometer_m=mi(10_000) + miles_to_meters(300),
             volume_ml=gallons_to_ml("5"), total_cost_minor=3_500)
    fe = fuel_economy(db, v.id)
    assert fe.distance_m == miles_to_meters(300) and fe.segments == 1
    assert fe.volume_ml == 2 * gallons_to_ml("5")                       # anchor fill excluded
    assert fe.mpg_x10 == 300                                            # 30.0 mpg
    assert fe.l_per_100km_x10 == 78                                     # 7.8 L/100 km
    assert fe.cost_per_mile_deci_minor == 233                           # 23.3¢/mi
    assert get_vehicle(db, v.id).odometer_m == mi(10_000) + miles_to_meters(300)


def test_ev_charging_efficiency(db):
    v = car(db, nickname="EV", fuel_type="electric", odometer_m=0)
    t = datetime(2026, 9, 1, tzinfo=UTC)
    log_fuel(db, v.id, filled_at=t, odometer_m=0, energy_wh=10_000, total_cost_minor=300)
    log_fuel(db, v.id, filled_at=t + timedelta(days=1), odometer_m=miles_to_meters(120), energy_wh=30_000,
             total_cost_minor=900)
    fe = fuel_economy(db, v.id)
    assert fe.mi_per_kwh_x10 == 40 and fe.mpg_x10 is None                # 4.0 mi/kWh


def test_fuel_log_creates_linked_expense_excluded_under_standard_mileage(db):
    v = car(db)
    log_id = log_fuel(db, v.id, filled_at=datetime(2026, 9, 1, tzinfo=UTC), odometer_m=v.odometer_m,
                      volume_ml=gallons_to_ml("10"), total_cost_minor=4_000, station="Shell")
    with db.connection() as conn:
        row = conn.execute("SELECT e.category, e.amount_minor FROM fuel_logs f JOIN expenses e ON e.id = f.expense_id "
                           "WHERE f.id = ?", (log_id,)).fetchone()
    assert (row["category"], row["amount_minor"]) == ("fuel", 4_000)
    fed, _ = TaxTableRegistry.load().get("US", 2026)
    assert summarize_year(db, 2026, base_currency="USD", table=fed).deductible_expenses == 0
    assert summarize_year(db, 2026, base_currency="USD", table=fed, vehicle_method="actual").deductible_expenses == 4_000


def test_fuel_log_validation(db):
    v = car(db)
    when = datetime(2026, 9, 1, tzinfo=UTC)
    with pytest.raises(FleetError, match="exactly one"):
        log_fuel(db, v.id, filled_at=when, odometer_m=v.odometer_m, total_cost_minor=1)
    with pytest.raises(FleetError, match="baseline"):
        log_fuel(db, v.id, filled_at=when, odometer_m=1, volume_ml=1, total_cost_minor=1)
    with pytest.raises(FleetError):
        log_fuel(db, v.id, filled_at=when, odometer_m=v.odometer_m, volume_ml=1, total_cost_minor=10.5)  # type: ignore[arg-type]


# --- depreciation -------------------------------------------------------------------------


def test_280f_limits_from_tables():
    assert auto_depreciation_limits(2026)[0] == (12_300 * D, 19_800 * D, 11_900 * D, 7_160 * D)
    assert auto_depreciation_limits(2025)[0] == (12_200 * D, 19_600 * D, 11_800 * D, 7_060 * D)
    assert auto_depreciation_limits(2027)[1] is True                    # provisional fallback


def test_macrs_under_the_caps():
    s = macrs_schedule(60_000 * D, 2026)
    assert [y.deduction_minor for y in s.years] == [12_000 * D, 19_200 * D, 11_520 * D, 6_912 * D, 6_912 * D, 3_456 * D]
    assert s.total_minor == 60_000 * D and s.method.startswith("MACRS")


def test_macrs_capped_luxury_vehicle_recovers_after_year_six():
    s = macrs_schedule(80_000 * D, 2026)
    assert [y.deduction_minor // D for y in s.years] == [12_300, 19_800, 11_900, 7_160, 7_160, 4_608, 7_160, 7_160, 2_752]
    assert s.total_minor == 80_000 * D and s.years[-1].remaining_basis_minor == 0
    assert s.years[6].rate_bp is None


def test_macrs_partial_business_use_scales_basis_and_caps():
    s = macrs_schedule(80_000 * D, 2026, business_use_bp=6_000)
    assert s.business_basis_minor == 48_000 * D
    assert s.years[0].cap_minor == 7_380 * D and s.years[0].deduction_minor == 7_380 * D


def test_ads_required_at_or_below_50_percent():
    s = macrs_schedule(20_000 * D, 2026, business_use_bp=4_000)
    assert s.method.startswith("ADS")
    assert [y.rate_bp for y in s.years[:6]] == [1000, 2000, 2000, 2000, 2000, 1000]
    assert s.years[0].deduction_minor == 800 * D                        # 10% of $8,000


def test_macrs_validation():
    with pytest.raises(FleetError):
        macrs_schedule(-1, 2026)
    with pytest.raises(FleetError):
        macrs_schedule(100, 2026, business_use_bp=10_001)
    assert macrs_schedule(10_000 * D, 2026, passenger_auto=False).total_minor == 10_000 * D


# --- fleet assignment ----------------------------------------------------------------------


def test_assignment_resolution_order(db, platform_id):
    a, b = car(db, nickname="A"), car(db, nickname="B")
    assert resolve_vehicle(db, on=date(2026, 9, 1)) is None              # two vehicles, no assignments
    assign_vehicle(db, a.id, starts_on=date(2026, 1, 1))
    assign_vehicle(db, b.id, platform_id=platform_id, starts_on=date(2026, 9, 1), ends_on=date(2026, 9, 30))
    assert resolve_vehicle(db, platform_id=platform_id, on=date(2026, 9, 15)) == b.id
    assert resolve_vehicle(db, platform_id=platform_id, on=date(2026, 10, 1)) == a.id   # falls back to default
    assert resolve_vehicle(db, on=date(2026, 9, 15)) == a.id


def test_single_vehicle_is_implicit_default(db):
    v = car(db)
    assert resolve_vehicle(db, on=date(2026, 9, 1)) == v.id


def test_assignment_overlap_rules(db, platform_id):
    a, b = car(db, nickname="A"), car(db, nickname="B")
    first = assign_vehicle(db, a.id, platform_id=platform_id, starts_on=date(2026, 1, 1))
    assign_vehicle(db, b.id, platform_id=platform_id, starts_on=date(2026, 6, 1))       # closes the open one
    closed = next(x for x in list_assignments(db) if x.id == first)
    assert closed.ends_on == date(2026, 5, 31)
    with pytest.raises(FleetError, match="Overlaps"):
        assign_vehicle(db, a.id, platform_id=platform_id, starts_on=date(2026, 3, 1), ends_on=date(2026, 3, 31))
    # The same vehicle may cover a different platform at the same time (multi-apping).
    assign_vehicle(db, b.id, starts_on=date(2026, 6, 1))
    with pytest.raises(FleetError):
        assign_vehicle(db, a.id, starts_on=date(2026, 7, 1), ends_on=date(2026, 6, 1))


def test_retire_vehicle_closes_assignments_and_blocks_use(db):
    a, b = car(db, nickname="A"), car(db, nickname="B")
    assign_vehicle(db, a.id, starts_on=date(2026, 1, 1))
    retire_vehicle(db, a.id, on=date(2026, 8, 31))
    assert list_assignments(db)[0].ends_on == date(2026, 8, 31)
    assert resolve_vehicle(db, on=date(2026, 9, 1)) == b.id              # only active vehicle left
    with pytest.raises(FleetError, match="inactive"):
        assign_vehicle(db, a.id, starts_on=date(2026, 9, 1))


# --- maintenance triggers -------------------------------------------------------------------

SCHEDULE = [
    MaintenanceItem("Oil change", "engine", mi(10_000), 12, mi(5_000), 6),
    MaintenanceItem("Tire rotation", "tires", mi(7_500)),
]


def fleet_car(db, **kw):
    v = register_vehicle(db, nickname=kw.pop("nickname", "Fleet"), odometer_m=mi(60_000), acquired_on=date(2026, 3, 1),
                         import_maintenance=False, **kw)[0]
    import_schedule(db, v.id, SCHEDULE, source="user")
    return v


def status(db, vid, today=date(2026, 4, 1), **kw):
    return {d.item.service: d.status for d in due_for_vehicle(db, vid, today=today, **kw)}


def test_used_car_baseline_prevents_false_overdue(db):
    v = fleet_car(db)
    assert status(db, v.id) == {"Oil change": "ok", "Tire rotation": "ok"}


def test_distance_triggers_severe_and_normal(db):
    v = fleet_car(db)
    update_odometer(db, v.id, mi(64_600))                          # 4,600 mi since baseline
    assert status(db, v.id) == {"Oil change": "due_soon", "Tire rotation": "ok"}          # severe 5k
    assert status(db, v.id, severe=False) == {"Oil change": "ok", "Tire rotation": "ok"}
    update_odometer(db, v.id, mi(67_600))
    assert status(db, v.id) == {"Oil change": "overdue", "Tire rotation": "overdue"}


def test_time_triggers(db):
    v = fleet_car(db)
    assert status(db, v.id, today=date(2026, 8, 5))["Oil change"] == "due_soon"   # severe 6 months → Sep 1
    assert status(db, v.id, today=date(2026, 9, 1))["Oil change"] == "overdue"
    assert status(db, v.id, today=date(2026, 9, 1), severe=False)["Oil change"] == "ok"


def test_record_service_resets_interval_and_files_expense(db):
    v = fleet_car(db)
    update_odometer(db, v.id, mi(66_000))
    assert status(db, v.id)["Oil change"] == "overdue"
    rid = record_service(db, v.id, service="oil CHANGE", performed_on=date(2026, 4, 1), odometer_m=mi(66_000),
                         cost_minor=7_999, vendor="Jiffy Lube")
    assert status(db, v.id)["Oil change"] == "ok"
    assert last_services(db, v.id)["oil change"] == (mi(66_000), date(2026, 4, 1))
    with db.connection() as conn:
        rec = conn.execute("SELECT r.service, r.category, e.category AS ecat, e.amount_minor FROM maintenance_records r "
                           "JOIN expenses e ON e.id = r.expense_id WHERE r.id = ?", (rid,)).fetchone()
    assert (rec["service"], rec["category"], rec["ecat"], rec["amount_minor"]) == ("Oil change", "engine", "maintenance", 7_999)


def test_record_service_validation(db):
    v = fleet_car(db)
    with pytest.raises(MaintenanceError):
        record_service(db, v.id, service=" ", performed_on=date(2026, 4, 1), odometer_m=1)
    with pytest.raises(MaintenanceError):
        record_service(db, v.id, service="x", performed_on=date(2026, 4, 1), odometer_m=1, cost_minor=9.99)  # type: ignore[arg-type]
    with pytest.raises(MaintenanceError):
        record_service(db, "nope", service="x", performed_on=date(2026, 4, 1), odometer_m=1)


def test_imports_never_overwrite_user_items(db):
    v = fleet_car(db)
    import_schedule(db, v.id, [MaintenanceItem("Oil change", "engine", mi(3_000))], source="dataone")
    assert {i.service: i.interval_meters for i in load_schedule(db, v.id)}["Oil change"] == mi(10_000)
    import_schedule(db, v.id, [MaintenanceItem("Oil change", "engine", mi(8_000))], source="user")
    assert {i.service: i.interval_meters for i in load_schedule(db, v.id)}["Oil change"] == mi(8_000)


def test_stop_trip_fires_maintenance_alerts(db):
    v = fleet_car(db)
    trip = start_trip(db, vehicle_id=v.id, odometer_start_m=mi(60_000), started_at=datetime(2026, 4, 1, 12, tzinfo=UTC))
    quiet = stop_trip(db, trip.id, odometer_end_m=mi(60_100), ended_at=datetime(2026, 4, 1, 14, tzinfo=UTC))
    assert quiet.maintenance_alerts == ()
    trip = start_trip(db, vehicle_id=v.id, odometer_start_m=mi(60_100), started_at=datetime(2026, 4, 2, 12, tzinfo=UTC))
    loud = stop_trip(db, trip.id, odometer_end_m=mi(64_700), ended_at=datetime(2026, 4, 3, 12, tzinfo=UTC))
    assert [(a.item.service, a.status) for a in loud.maintenance_alerts] == [("Oil change", "due_soon")]
    assert fleet_alerts(db, today=date(2026, 4, 3))[v.id][0].item.service == "Oil change"


def test_fleet_overview_and_year_summary(db, platform_id):
    v = fleet_car(db)
    assign_vehicle(db, v.id, platform_id=platform_id, starts_on=date(2026, 1, 1))
    update_odometer(db, v.id, mi(66_000))
    row = fleet_overview(db, on=date(2026, 9, 1))[0]
    assert row["platforms"] == [platform_id] and "Oil change" in row["overdue"]

    from core.fleet.mileage import log_trip
    when = datetime(2026, 5, 1, 12, tzinfo=UTC)
    log_trip(db, started_at=when, distance_m=miles_to_meters(300), vehicle_id=v.id)
    log_trip(db, started_at=when, distance_m=miles_to_meters(100), vehicle_id=v.id, purpose="personal")
    log_fuel(db, v.id, filled_at=when, odometer_m=mi(66_000), volume_ml=gallons_to_ml("10"), total_cost_minor=4_000)
    record_service(db, v.id, service="Oil change", performed_on=date(2026, 5, 2), odometer_m=mi(66_100), cost_minor=6_000)
    s = vehicle_year_summary(db, v.id, 2026)
    assert s.business_share_bp == 7_500
    assert s.standard_mileage_deduction_minor == 300 * 725 // 10          # 72.5¢ × 300 mi
    assert s.standard_mileage_basis_reduction_minor == 300 * 35
    assert s.actual_operating_deduction_minor == (4_000 + 6_000) * 3 // 4
