"""Motion sentry thresholds and state machine, classifier, persistence service, permission policy."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from core.fleet.geo import GeoPoint
from core.fleet.mileage import get_active_trip, start_trip
from core.motion import policy
from core.motion.classifier import ClassifierContext, WorkHours, Zone, classify
from core.motion.sentry import (
    IDLE_PROFILE,
    TRIP_PROFILE,
    DetectedTrip,
    MotionFix,
    MotionSentry,
    SentryConfig,
    SentryState,
    TripDiscarded,
    TripEnded,
    TripStarted,
    mph_to_mps,
)
from core.motion.service import (
    AutoTripDiscarded,
    AutoTripSaved,
    AutoTripStarted,
    MotionService,
    SentrySkipped,
    confirm_trip,
    dismiss_trip,
    trips_needing_review,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
TEN_MPH = float(mph_to_mps(10))                      # 4.4704 m/s exactly
DEG_PER_M = 1 / 111_195                              # ~1 m of longitude at the equator


def fix(t: float, x_m: float, speed: float | None = None, acc: float | None = 5.0) -> MotionFix:
    """Fix at t seconds, x_m meters east of (0, 0)."""
    return MotionFix(T0 + timedelta(seconds=t), 0.0, x_m * DEG_PER_M, speed, acc)


def drive(sentry: MotionSentry, *, start_t: float = 0, seconds: int = 300, mps: float = 13.4, step: int = 5,
          start_x: float = 0.0) -> tuple[list, float, float]:
    """Constant-speed drive; returns (events, end_t, end_x)."""
    events, x = [], start_x
    t = start_t
    while t <= start_t + seconds:
        events += sentry.feed(fix(t, x, mps))
        t += step
        x += mps * step
    return events, t - step, x - mps * step


def park(sentry: MotionSentry, *, t: float, x: float, seconds: int, step: int = 15) -> list:
    events = []
    for dt in range(step, seconds + step, step):
        events += sentry.feed(fix(t + dt, x, 0.0))
    return events


# --- thresholds --------------------------------------------------------------------------


def test_mph_conversion_is_exact():
    assert mph_to_mps(10) == Decimal("4.47040")
    with pytest.raises(TypeError):
        mph_to_mps(10.0)  # type: ignore[arg-type]


def test_exactly_ten_mph_does_not_arm_but_just_over_does():
    s = MotionSentry()
    assert not s.is_over_start_threshold(TEN_MPH)                  # strictly greater than
    assert s.is_over_start_threshold(TEN_MPH + 0.01)
    assert not s.is_over_start_threshold(None)
    s.feed(fix(0, 0, TEN_MPH))
    assert s.state is SentryState.IDLE
    s.feed(fix(5, 30, TEN_MPH + 0.05))
    assert s.state is SentryState.ARMING


def test_walking_and_cycling_never_arm():
    s = MotionSentry()
    for i in range(60):
        s.feed(fix(i * 5, i * 20, 4.0))                            # 9 mph
    assert s.state is SentryState.IDLE


def test_configurable_threshold():
    s = MotionSentry(SentryConfig(start_speed_mph=15, stop_speed_mph=5))
    assert not s.is_over_start_threshold(float(mph_to_mps(14)))
    assert s.is_over_start_threshold(float(mph_to_mps(16)))
    with pytest.raises(ValueError):
        SentryConfig(start_speed_mph=5, stop_speed_mph=5)


# --- state machine -----------------------------------------------------------------------


def test_sustained_motion_confirms_trip_start():
    s = MotionSentry()
    events = []
    for i, t in enumerate((0, 10, 20)):
        events += s.feed(fix(t, i * 130, 13.0))
    assert s.state is SentryState.ARMING and events == []          # 3 fixes but only 20 s
    events += s.feed(fix(30, 390, 13.0))
    assert s.state is SentryState.MOVING
    assert isinstance(events[0], TripStarted) and events[0].started_at == T0
    assert s.sampling_profile() == TRIP_PROFILE


def test_short_burst_resets():
    s = MotionSentry()
    s.feed(fix(0, 0, 8.0))
    s.feed(fix(5, 40, 8.0))
    s.feed(fix(10, 45, 1.0))                                        # stopped again
    assert s.state is SentryState.IDLE and s.sampling_profile() == IDLE_PROFILE


def test_full_trip_with_red_light_and_parking():
    s = MotionSentry()
    events, t, x = drive(s, seconds=300)
    events += park(s, t=t, x=x, seconds=60)                         # red light: 60 s < dwell
    assert s.state is SentryState.STOPPING
    more, t2, x2 = drive(s, start_t=t + 75, seconds=200, start_x=x)
    events += more
    assert s.state is SentryState.MOVING                            # resumed, same trip
    events += park(s, t=t2, x=x2, seconds=240)
    ended = [e for e in events if isinstance(e, TripEnded)]
    assert len(ended) == 1 and ended[0].reason == "stopped"
    trip = ended[0].trip
    assert trip.started_at == T0
    assert trip.ended_at == T0 + timedelta(seconds=t2 + 15)          # first slow fix
    assert abs(trip.distance_m - round(x2)) <= 20
    assert trip.max_speed_mps == pytest.approx(13.4)
    assert s.state is SentryState.IDLE


def test_parked_without_fixes_ends_via_tick():
    s = MotionSentry()
    _, t, _ = drive(s, seconds=120)
    assert s.tick(T0 + timedelta(seconds=t + 60)) == []
    events = s.tick(T0 + timedelta(seconds=t + 180))
    assert isinstance(events[0], TripEnded) and events[0].reason == "stopped"
    assert events[0].trip.ended_at == T0 + timedelta(seconds=t)


def test_signal_loss_ends_trip_at_last_fix():
    s = MotionSentry()
    _, t, x = drive(s, seconds=120)
    s.feed(fix(t + 5, x + 20, 1.0))                                  # starts stopping
    events = s.feed(fix(t + 900, x + 5_000, 13.0))                   # 15 min later
    ended = [e for e in events if isinstance(e, TripEnded)]
    assert ended and ended[0].reason == "signal_lost"


def test_short_trip_discarded():
    s = MotionSentry(SentryConfig(min_trip_meters=5_000))
    events, t, x = drive(s, seconds=60)
    events += park(s, t=t, x=x, seconds=240)
    assert any(isinstance(e, TripDiscarded) for e in events)


def test_inaccurate_duplicate_and_teleport_fixes_rejected():
    s = MotionSentry()
    s.feed(fix(0, 0, 0.0))
    assert s.feed(fix(5, 100, 13.0, acc=200.0)) == []                # poor accuracy
    s.feed(fix(10, 10, 0.0))
    s.feed(fix(10, 10, 0.0))                                         # duplicate timestamp
    s.feed(fix(15, 50_000, 13.0))                                    # 10 km in 5 s
    assert s.rejected == 3 and s.state is SentryState.IDLE


def test_speed_derived_when_os_reports_none_or_negative():
    s = MotionSentry()
    s.feed(fix(0, 0, None))
    assert s.speed_of(fix(10, 150, None)) == pytest.approx(15.0, rel=1e-3)
    assert s.speed_of(fix(10, 150, -1.0)) == pytest.approx(15.0, rel=1e-3)
    assert s.speed_of(fix(10, 150, 3.0)) == 3.0


def test_force_end_and_naive_datetime():
    s = MotionSentry()
    _, t, _ = drive(s, seconds=120)
    assert isinstance(s.force_end()[0], TripEnded)
    with pytest.raises(ValueError):
        s.feed(MotionFix(datetime(2026, 1, 1), 0, 0, 1.0))


# --- classifier ----------------------------------------------------------------------------

HOME = Zone("Home", GeoPoint(27.95, -82.46), 200)
AIRPORT = Zone("TPA", GeoPoint(27.975, -82.533), 1_500)


def trip(start: GeoPoint, end: GeoPoint, when: datetime) -> DetectedTrip:
    return DetectedTrip(when, when + timedelta(minutes=20), start, end, 12_000, 20.0, ())


# Saturday 2026-09-26 18:00 EDT = 22:00 UTC
SAT_EVENING = datetime(2026, 9, 26, 22, 0, tzinfo=UTC)
WEEKEND_EVENINGS = WorkHours(frozenset({4, 5, 6}), time(16, 0), time(2, 0))


def test_shift_wins():
    c = classify(trip(HOME.center, GeoPoint(28.0, -82.5), SAT_EVENING),
                 ClassifierContext(shift_active=True, shift_platform_id="uber"))
    assert (c.purpose, c.confidence_bp, c.platform_id) == ("business", 9_500, "uber")


def test_work_hours_and_zones():
    ctx = ClassifierContext(tz="America/New_York", home=HOME, work_zones=(AIRPORT,), work_hours=(WEEKEND_EVENINGS,))
    assert classify(trip(GeoPoint(28.0, -82.4), AIRPORT.center, SAT_EVENING), ctx).confidence_bp == 8_000
    assert classify(trip(GeoPoint(28.0, -82.4), GeoPoint(28.1, -82.4), SAT_EVENING), ctx).purpose == "business"
    tuesday_noon = datetime(2026, 9, 22, 16, 0, tzinfo=UTC)
    assert classify(trip(HOME.center, AIRPORT.center, tuesday_noon), ctx).purpose == "commute"
    assert classify(trip(GeoPoint(28.0, -82.4), GeoPoint(28.1, -82.4), tuesday_noon), ctx).purpose == "personal"
    assert classify(trip(GeoPoint(28.0, -82.4), AIRPORT.center, tuesday_noon), ctx).confidence_bp == 5_000


def test_overnight_work_hours():
    hours = WorkHours(frozenset({4}), time(20, 0), time(2, 0))       # Friday night shift
    assert hours.covers(datetime(2026, 9, 25, 23, 0))                # Fri 23:00
    assert hours.covers(datetime(2026, 9, 26, 1, 30))                # Sat 01:30 belongs to Friday
    assert not hours.covers(datetime(2026, 9, 26, 3, 0))
    assert not hours.covers(datetime(2026, 9, 24, 23, 0))            # Thursday


def test_no_work_hours_is_low_confidence_personal():
    c = classify(trip(GeoPoint(28.0, -82.4), GeoPoint(28.1, -82.4), SAT_EVENING), ClassifierContext())
    assert (c.purpose, c.confidence_bp) == ("personal", 5_000)


# --- service (DB) ----------------------------------------------------------------------------


def make_service(db, ctx=None):
    return MotionService(db, context=lambda: ctx or ClassifierContext(shift_active=True))


def run_trip(service, *, seconds=300):
    events = []
    x, t = 0.0, 0
    while t <= seconds:
        events += service.handle_fix(fix(t, x, 13.4))
        t += 5
        x += 67
    for dt in range(15, 255, 15):
        events += service.handle_fix(fix(t + dt, x, 0.0))
    return events


def test_auto_trip_persisted_start_to_finish(db):
    service = make_service(db)
    events = run_trip(service)
    started = next(e for e in events if isinstance(e, AutoTripStarted))
    saved = next(e for e in events if isinstance(e, AutoTripSaved))
    assert started.trip_id == saved.trip_id and saved.classification.purpose == "business"
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM mileage_logs WHERE id = ?", (saved.trip_id,)).fetchone()
    assert (row["trip_state"], row["detection"], row["review_status"], row["distance_source"]) == (
        "completed", "auto", "needs_review", "gps")
    assert abs(row["distance_m"] - saved.distance_m) == 0 and saved.distance_m > 3_500
    assert [t["id"] for t in trips_needing_review(db)] == [saved.trip_id]

    confirm_trip(db, saved.trip_id, purpose="personal")
    assert trips_needing_review(db) == []
    with db.connection() as conn:
        assert conn.execute("SELECT purpose FROM mileage_logs WHERE id = ?", (saved.trip_id,)).fetchone()[0] == "personal"


def test_trip_in_progress_survives_restart(db):
    service = make_service(db)
    for i, t in enumerate(range(0, 40, 5)):
        service.handle_fix(fix(t, i * 67, 13.4))
    assert get_active_trip(db) is not None                           # written at start, not at end


def test_discarded_detection_cancels_trip(db):
    service = MotionService(db, context=lambda: ClassifierContext(),
                            sentry=MotionSentry(SentryConfig(min_trip_meters=50_000)))
    events = run_trip(service, seconds=60)
    assert any(isinstance(e, AutoTripDiscarded) for e in events)
    assert get_active_trip(db) is None and trips_needing_review(db) == []


def test_sentry_stays_out_of_manual_trips(db):
    start_trip(db, started_at=T0 - timedelta(minutes=5))
    events = run_trip(make_service(db))
    assert any(isinstance(e, SentrySkipped) for e in events)
    assert not any(isinstance(e, AutoTripSaved) for e in events)


def test_dismiss_only_touches_auto_trips(db):
    service = make_service(db)
    saved = next(e for e in run_trip(service) if isinstance(e, AutoTripSaved))
    dismiss_trip(db, saved.trip_id)
    assert trips_needing_review(db) == []
    with pytest.raises(Exception):
        confirm_trip(db, saved.trip_id, purpose="business")          # deleted
    with pytest.raises(Exception):
        confirm_trip(db, saved.trip_id, purpose="vacation")


def test_listener_receives_events(db):
    got = []
    service = MotionService(db, context=lambda: ClassifierContext(), listener=got.append)
    run_trip(service)
    assert [type(e).__name__ for e in got] == ["AutoTripStarted", "AutoTripSaved"]


# --- permission policy -------------------------------------------------------------------------


@pytest.mark.parametrize("platform,status,bg,accepted,action", [
    ("windows", None, False, True, "unsupported"),
    ("web", "always", True, True, "unsupported"),
    ("android", None, False, False, "show_disclosure"),
    ("android", None, False, True, "request_permission"),
    ("android", "denied", True, True, "request_permission"),
    ("ios", "deniedForever", False, True, "open_app_settings"),
    ("android", "whileInUse", False, True, "run_foreground_only"),
    ("android", "whileInUse", True, True, "open_app_settings"),      # Android 11+: settings only
    ("ios", "whileInUse", True, True, "request_permission"),         # iOS upgrade prompt
    ("ios", "always", True, True, "run_background"),
    ("android", "always", False, True, "run_foreground_only"),
])
def test_permission_steps(platform, status, bg, accepted, action):
    assert policy.next_step(platform, status, want_background=bg, disclosure_accepted=accepted).action == action


def test_disclosure_mentions_background_collection():
    text = policy.DISCLOSURE_TEXT.lower()
    assert "location" in text and "closed or not in use" in text
