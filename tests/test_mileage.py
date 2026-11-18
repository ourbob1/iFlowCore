"""Trip logging, distance resolution, geodesy, and the split-year IRS calculator."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from core.financial.tax import TaxTableRegistry, summarize_year
from core.fleet.geo import GeoPoint, GpsFix, geohash, haversine_m, nearest, route_distance, straight_line_m
from core.fleet.mileage import (
    IRSMileageCalculator,
    MileageError,
    cancel_trip,
    get_active_trip,
    log_trip,
    resolve_distance,
    start_trip,
    stop_trip,
)
from core.fleet.vehicle import assign_vehicle, get_vehicle, register_vehicle

UTC = timezone.utc
MILE = 1_609_344          # meters per 1,000 miles (exact) — use MILES() below
T0 = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)


def MILES(n: int) -> int:
    """Exact meters for n miles when n is a multiple of 1,000; rounded otherwise."""
    return (n * MILE + 500) // 1000


@pytest.fixture(scope="module")
def calc():
    return IRSMileageCalculator(TaxTableRegistry.load())


@pytest.fixture
def car(db):
    vehicle, _ = register_vehicle(db, nickname="Camry", odometer_m=MILES(40_000), acquired_on=date(2026, 1, 1))
    return vehicle


# --- geodesy -----------------------------------------------------------------------------


def test_haversine_one_degree_of_latitude():
    assert round(haversine_m(GeoPoint(0, 0), GeoPoint(1, 0))) == 111_195
    assert straight_line_m(GeoPoint(10, 20), GeoPoint(10, 20)) == 0


def test_invalid_coordinates():
    with pytest.raises(ValueError):
        GeoPoint(91, 0)
    with pytest.raises(ValueError):
        GeoPoint(0, -181)


def test_geohash_reference_value():
    assert geohash(GeoPoint(57.64911, 10.40744), 11) == "u4pruydqqvj"      # canonical example
    assert geohash(GeoPoint(40.7580, -73.9855), 5) == "dr5ru"
    with pytest.raises(ValueError):
        geohash(GeoPoint(0, 0), 0)


def test_nearest():
    idx, dist = nearest(GeoPoint(0, 0), [GeoPoint(0, 2), GeoPoint(0, 1)])
    assert idx == 1 and dist == 111_195


def _trace(n: int, step_deg: float = 0.001, seconds: int = 10, **kw):
    return [GpsFix(GeoPoint(0, i * step_deg), T0 + timedelta(seconds=i * seconds), **kw) for i in range(n)]


def test_route_distance_sums_segments():
    result = route_distance(_trace(11))                  # 10 steps of ~111.2 m
    assert result.distance_m == round(10 * haversine_m(GeoPoint(0, 0), GeoPoint(0, 0.001)))
    assert result.kept_fixes == 11


def test_route_filters_inaccurate_jitter_and_glitches():
    fixes = _trace(11)
    fixes.insert(3, GpsFix(GeoPoint(5, 5), T0 + timedelta(seconds=25)))                 # teleport glitch
    fixes.append(GpsFix(GeoPoint(0, 0.0101), T0 + timedelta(seconds=200), 200.0))     # poor accuracy
    fixes += [GpsFix(GeoPoint(0, 0.01 + 0.00001 * k), T0 + timedelta(seconds=300 + k)) for k in range(5)]  # ~1 m jitter
    result = route_distance(fixes)
    assert result.distance_m == round(10 * haversine_m(GeoPoint(0, 0), GeoPoint(0, 0.001)))
    assert result.dropped_speed == 1 and result.dropped_inaccurate == 1


def test_slow_traffic_still_accumulates():
    # 3 m steps are below the jitter threshold individually but add up.
    fixes = [GpsFix(GeoPoint(0, i * 0.000027), T0 + timedelta(seconds=i)) for i in range(100)]
    total = route_distance(fixes).distance_m
    true_distance = round(haversine_m(fixes[0].point, fixes[-1].point))
    assert true_distance - 10 <= total <= true_distance     # at most one sub-threshold step uncounted


# --- distance resolution -------------------------------------------------------------------


def test_resolution_priority_and_crosschecks():
    end = T0 + timedelta(hours=1)
    gps = _trace(11)
    meters, source, warnings = resolve_distance(started_at=T0, ended_at=end, odometer_start_m=1000,
                                                odometer_end_m=2112, gps=gps)
    assert (meters, source) == (1112, "odometer") and warnings == []
    meters, source, warnings = resolve_distance(started_at=T0, ended_at=end, distance_m=5000,
                                                odometer_start_m=1000, odometer_end_m=3000, gps=gps)
    assert (meters, source) == (5000, "manual")
    assert any("disagree" in w for w in warnings)         # odometer 2000 vs gps ~1112
    meters, source, _ = resolve_distance(started_at=T0, ended_at=end, gps=gps)
    assert source == "gps" and meters == 1112


def test_resolution_errors_and_sanity_warnings():
    end = T0 + timedelta(minutes=10)
    with pytest.raises(MileageError, match="lower"):
        resolve_distance(started_at=T0, ended_at=end, odometer_start_m=5, odometer_end_m=4)
    with pytest.raises(MileageError, match="No distance"):
        resolve_distance(started_at=T0, ended_at=end)
    with pytest.raises(MileageError, match="integer"):
        resolve_distance(started_at=T0, ended_at=end, distance_m=12.5)  # type: ignore[arg-type]
    _, _, warnings = resolve_distance(started_at=T0, ended_at=end, distance_m=100_000)   # 166 m/s
    assert any("implausibly" in w for w in warnings)
    _, _, warnings = resolve_distance(started_at=T0, ended_at=end, distance_m=10, odometer_end_m=10)
    assert any("without a start" in w for w in warnings)


# --- trip lifecycle ------------------------------------------------------------------------


def test_odometer_trip_updates_vehicle(db, car):
    start = car.odometer_m
    trip = start_trip(db, vehicle_id=car.id, odometer_start_m=start, started_at=T0)
    assert trip.trip_state == "active" and get_active_trip(db, car.id).id == trip.id
    result = stop_trip(db, trip.id, odometer_end_m=start + 16_093, ended_at=T0 + timedelta(minutes=30))
    assert result.trip.distance_m == 16_093 and result.trip.distance_source == "odometer"
    assert result.trip.trip_state == "completed"
    assert get_vehicle(db, car.id).odometer_m == start + 16_093
    assert get_active_trip(db, car.id) is None


def test_gps_trip_estimates_odometer(db, car):
    trip = start_trip(db, vehicle_id=car.id, started_at=T0)
    result = stop_trip(db, trip.id, gps=_trace(11), ended_at=T0 + timedelta(minutes=5))
    assert result.trip.distance_source == "gps"
    with db.connection() as conn:
        assert conn.execute("SELECT source FROM mileage_logs WHERE id = ?", (trip.id,)).fetchone()[0] == "gps"
    assert get_vehicle(db, car.id).odometer_m == car.odometer_m + result.trip.distance_m


def test_one_active_trip_per_vehicle(db, car):
    start_trip(db, vehicle_id=car.id, started_at=T0)
    with pytest.raises(MileageError, match="already active"):
        start_trip(db, vehicle_id=car.id, started_at=T0)


def test_odometer_rollback_refused(db, car):
    with pytest.raises(MileageError, match="below the last reading"):
        start_trip(db, vehicle_id=car.id, odometer_start_m=car.odometer_m - 1, started_at=T0)


def test_stop_requires_active_and_cancel(db, car):
    trip = start_trip(db, vehicle_id=car.id, started_at=T0)
    cancel_trip(db, trip.id)
    assert get_active_trip(db, car.id) is None
    with pytest.raises(MileageError):
        stop_trip(db, trip.id, distance_m=10)
    done = log_trip(db, started_at=T0, distance_m=100, vehicle_id=car.id)
    with pytest.raises(MileageError, match="not active"):
        stop_trip(db, done.id, distance_m=10)
    with pytest.raises(MileageError, match="Only active"):
        cancel_trip(db, done.id)


def test_start_trip_uses_fleet_assignment(db, car, platform_id):
    bike, _ = register_vehicle(db, nickname="E-bike", vehicle_type="ebike")
    assign_vehicle(db, car.id, starts_on=date(2026, 1, 1))                            # default
    assign_vehicle(db, bike.id, platform_id=platform_id, starts_on=date(2026, 8, 1))  # this platform
    assert start_trip(db, platform_id=platform_id, started_at=T0).vehicle_id == bike.id
    assert start_trip(db, started_at=T0).vehicle_id == car.id


def test_log_trip_validation(db):
    with pytest.raises(MileageError):
        log_trip(db, started_at=T0, distance_m=-1)
    with pytest.raises(MileageError):
        log_trip(db, started_at=T0, distance_m=1, ended_at=T0 - timedelta(seconds=1))
    with pytest.raises(MileageError):
        log_trip(db, started_at=T0, distance_m=1, purpose="leisure")  # type: ignore[arg-type]


# --- IRS calculator ------------------------------------------------------------------------


def test_rates_by_date(calc):
    assert calc.rate_on(date(2025, 12, 31)) == 700
    assert calc.rate_on(date(2026, 6, 30)) == 725
    assert calc.rate_on(date(2026, 7, 1)) == 760
    assert calc.deduction_for(MILES(100_000) // 1000, date(2026, 6, 30)) == 7_250  # 100 mi


def test_split_year_calculation(calc):
    trips = [("a", date(2026, 6, 30), MILES(100)), ("b", date(2026, 7, 1), MILES(100)),
             ("c", date(2026, 1, 15), MILES(200))]
    report = calc.calculate(2026, trips)
    assert report.total_deduction_minor == 7_250 + 7_600 + 14_500
    assert [(p.effective_from, p.rate_deci_minor, p.deduction_minor) for p in report.periods] == [
        (date(2026, 1, 1), 725, 21_750), (date(2026, 7, 1), 760, 7_600)]
    assert report.depreciation_minor == 400 * 35            # 35¢/mi basis reduction for 2026
    assert str(report.business_miles) == "400.0"
    assert [l.trip_id for l in report.lines] == ["c", "a", "b"]


def test_per_period_vs_per_trip_rounding(calc):
    trips = [(str(i), date(2026, 3, 1), 1_609) for i in range(3)]   # three ~1-mile trips
    report = calc.calculate(2026, trips)
    assert [l.deduction_minor for l in report.lines] == [72, 72, 72]
    assert report.per_trip_total_minor == 216
    assert report.total_deduction_minor == 217                      # 217.44¢ rounded once


def test_calculator_rejects_out_of_year_and_flags_provisional(calc):
    with pytest.raises(MileageError, match="outside"):
        calc.calculate(2026, [("x", date(2025, 12, 31), 100)])
    report = calc.calculate(2027, [("x", date(2027, 7, 1), MILES(100))])
    assert report.warnings and "provisional" in report.warnings[0]
    assert report.total_deduction_minor == 7_600                    # latest known (2026 H2) rate


def test_local_date_decides_rate_and_tax_year(db, calc):
    # 02:00 UTC July 1 is still June 30 in New York → H1 rate.
    log_trip(db, started_at=datetime(2026, 7, 1, 2, 0, tzinfo=UTC), distance_m=MILES(100))
    # 03:00 UTC Jan 1 2027 is Dec 31 2026 in New York → counts in 2026.
    log_trip(db, started_at=datetime(2027, 1, 1, 3, 0, tzinfo=UTC), distance_m=MILES(100))
    log_trip(db, started_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC), distance_m=MILES(100), purpose="commute")

    ny = calc.report_for_year(db, 2026, timezone="America/New_York")
    assert ny.total_deduction_minor == 7_250 + 7_600 and ny.business_meters == 2 * MILES(100)
    utc = calc.report_for_year(db, 2026, timezone="UTC")
    assert utc.total_deduction_minor == 7_600                       # the other trip is in 2027 (UTC)

    fed, _ = TaxTableRegistry.load().get("US", 2026)
    summary = summarize_year(db, 2026, base_currency="USD", table=fed, timezone="America/New_York")
    assert summary.mileage_deduction == ny.total_deduction_minor    # tax engine agrees


def test_active_trips_excluded_from_reports(db, calc, car):
    start_trip(db, vehicle_id=car.id, started_at=T0)
    assert calc.report_for_year(db, 2026).business_meters == 0


def test_report_filters_by_vehicle(db, calc, car):
    other, _ = register_vehicle(db, nickname="Van")
    log_trip(db, started_at=T0, distance_m=MILES(10), vehicle_id=car.id)
    log_trip(db, started_at=T0, distance_m=MILES(20), vehicle_id=other.id)
    assert calc.report_for_year(db, 2026, vehicle_id=car.id).business_meters == MILES(10)
