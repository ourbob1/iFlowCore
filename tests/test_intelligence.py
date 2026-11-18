"""Personal Intelligence (Phase 6): local trend math, peak hours, zones & spatial aggregation, privacy."""

from __future__ import annotations

import ast
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core.crypto import FieldSealer
from core.fleet.geo import GeoPoint, geohash, geohash_bounds, geohash_center, haversine_m
from core.intelligence import build_report, period_bounds
from core.intelligence.data import classify_time, load_activity, to_base
from core.intelligence.engine import previous_bounds
from core.intelligence.metrics import efficiency, irs_vehicle_cost
from core.intelligence.models import Activity, EarningEvent, Interval, TripRecord
from core.intelligence.peaks import PRIOR_S, find_peaks, shrunk_rate
from core.intelligence.rates import change_bp, per_hour, per_km, per_mile
from core.intelligence.sessions import build_sessions, seconds_by_local_date, split_local
from core.intelligence.spatial import attribute_earnings, build_zones, heat_grid
from core.intelligence.temporal import build_grid, build_trend, pick_granularity, weeks_spanned
from core.intelligence.zones import PrivateZone, ZoneStore

NY = "America/New_York"
NYZ = ZoneInfo(NY)
TPA = GeoPoint(27.9756, -82.5333)
DOWNTOWN = GeoPoint(27.9506, -82.4572)
HOME = GeoPoint(28.0600, -82.4100)


def ny(y, mo, d, h=0, mi=0, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=NYZ).astimezone(timezone.utc)


def earning(at, net=1000, *, fee=0, timed=True, platform=None, day=None) -> EarningEvent:
    return EarningEvent(at=at, day=day or at.astimezone(NYZ).date(), gross_minor=net + fee, fee_minor=fee,
                        platform_id=platform, kind="earning", time_known=timed)


def trip(start, minutes=20, meters=8000, platform=None, tid=None) -> TripRecord:
    return TripRecord(tid or str(uuid.uuid4()), start, start + timedelta(minutes=minutes) if minutes is not None else None,
                      meters, platform)


def activity(earnings=(), trips=(), start=date(2026, 1, 1), end=date(2026, 12, 31)) -> Activity:
    return Activity(start=start, end=end, tz=NY, currency="USD", earnings=list(earnings), trips=list(trips))


# =============================================================================================
# rates
# =============================================================================================


def test_rates_are_integer_and_round_half_up():
    assert per_hour(1000, 1800) == 2000
    assert per_hour(1, 7200) == 1                     # 0.5 → 1 (half-up)
    assert per_hour(-1, 7200) == -1                   # away from zero
    assert per_hour(500, 0) is None
    assert per_mile(100, 1609) == 100                 # 100.02 → 100
    assert per_mile(100, 1609344 // 1000) == 100
    assert per_mile(1000, 0) is None
    assert per_km(1000, 2000) == 500
    assert change_bp(115, 100) == 1500 and change_bp(5, 0) is None
    assert all(isinstance(v, int) for v in (per_hour(1234, 777), per_mile(1234, 777), per_km(1234, 777)))


# =============================================================================================
# data loading
# =============================================================================================


def test_to_base():
    assert to_base(1000, 100, "USD", None, "USD") == (1000, 100)
    assert to_base(1000, 100, "EUR", 1100, "USD") == (1100, 110)
    assert to_base(1000, 100, "EUR", None, "USD") is None


@pytest.mark.parametrize("raw,expected", [
    ("2026-09-20", (date(2026, 9, 20), False)),                                # no time part
    ("2026-09-20T04:00:00.000Z", (date(2026, 9, 20), False)),                  # local midnight (EDT)
    ("2026-09-20T00:00:00.000Z", (date(2026, 9, 20), False)),                  # UTC midnight: written date kept
    ("2026-09-20T23:30:00.000Z", (date(2026, 9, 20), True)),                   # 7:30 PM local
    ("2026-09-21T01:30:00.000Z", (date(2026, 9, 20), True)),                   # 9:30 PM local, previous UTC day
])
def test_classify_time(raw, expected):
    from core.timeutil import parse_iso

    assert classify_time(raw, parse_iso(raw), NY) == expected


def _platform(db, name="Uber") -> str:
    pid = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute("INSERT INTO platforms (id, name, kind) VALUES (?, ?, 'rideshare')", (pid, name))
    return pid


def _txn(db, pid, at: str, gross=2000, fee=500, kind="earning", currency="USD", base=None, deleted=False):
    with db.transaction() as conn:
        conn.execute("INSERT INTO transactions (platform_id, kind, gross_amount_minor, platform_fee_minor, currency, "
                     "base_amount_minor, occurred_at, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (pid, kind, gross, fee, currency, base, at, "2026-09-01T00:00:00Z" if deleted else None))


def _trip(db, start: datetime, minutes=30, meters=10_000, purpose="business", review="confirmed",
          platform=None) -> str:
    from core.fleet.mileage import log_trip

    t = log_trip(db, started_at=start, ended_at=start + timedelta(minutes=minutes) if minutes else None,
                 distance_m=meters, purpose=purpose, platform_id=platform)
    if review != "confirmed":
        with db.transaction() as conn:
            conn.execute("UPDATE mileage_logs SET review_status = ?, detection = 'auto' WHERE id = ?", (review, t.id))
    return t.id


def test_load_activity_filters_and_converts(db):
    pid = _platform(db)
    _txn(db, pid, "2026-09-10T22:00:00.000Z")                                    # normal
    _txn(db, pid, "2026-09-10T22:05:00.000Z", kind="reimbursement")              # excluded kind
    _txn(db, pid, "2026-09-10T22:10:00.000Z", deleted=True)                       # soft-deleted
    _txn(db, pid, "2026-09-10T22:15:00.000Z", currency="EUR")                     # no base amount
    _txn(db, pid, "2026-09-10T22:20:00.000Z", currency="EUR", gross=1000, fee=100, base=1100)
    _txn(db, pid, "2026-09-11", gross=3000, fee=0)                                # date-only
    _trip(db, ny(2026, 9, 10, 18))
    _trip(db, ny(2026, 9, 10, 19), purpose="personal")
    _trip(db, ny(2026, 9, 10, 20), review="needs_review")
    act = load_activity(db, start=date(2026, 9, 1), end=date(2026, 9, 30), tz=NY, currency="usd")
    assert [e.net_minor for e in act.earnings] == [1500, 1100 - 110, 3000]
    assert act.excluded_unconverted == 1 and act.untimed_earnings == 1 and act.excluded_unreviewed == 1
    assert len(act.trips) == 1 and act.trips[0].meters == 10_000
    assert act.earnings[-1].day == date(2026, 9, 11) and not act.earnings[-1].time_known


def test_period_edges_follow_local_calendar(db):
    pid = _platform(db)
    _txn(db, pid, "2026-10-01T03:30:00.000Z")          # Sep 30, 11:30 PM in New York: inside September
    _txn(db, pid, "2026-10-01T04:30:00.000Z")          # Oct 1, 12:30 AM local: outside
    _txn(db, pid, "2026-09-01T00:00:00.000Z")          # date-only row stored at UTC midnight on Sep 1: inside
    _txn(db, pid, "2026-08-31T23:00:00.000Z")          # Aug 31, 7 PM local: outside
    act = load_activity(db, start=date(2026, 9, 1), end=date(2026, 9, 30), tz=NY, currency="USD")
    assert sorted(e.day for e in act.earnings) == [date(2026, 9, 1), date(2026, 9, 30)]


def test_load_activity_rejects_inverted_range(db):
    with pytest.raises(ValueError):
        load_activity(db, start=date(2026, 9, 2), end=date(2026, 9, 1), tz=NY, currency="USD")


# =============================================================================================
# sessions & local-time splitting
# =============================================================================================


def test_sessions_merge_within_gap_and_split_beyond():
    t0 = ny(2026, 9, 11, 18)
    act = activity(
        earnings=[earning(t0 + timedelta(minutes=35)), earning(t0 + timedelta(hours=5), timed=False)],
        trips=[trip(t0, 30), trip(t0 + timedelta(minutes=75), 15),               # 45 min gap: merges (inclusive)
               trip(t0 + timedelta(minutes=90 + 46), 10)],                        # 46 min gap: new session
    )
    sessions = build_sessions(act)
    assert [(s.trips, s.earnings) for s in sessions] == [(2, 1), (1, 0)]
    assert sessions[0].seconds == 90 * 60 and sessions[1].seconds == 10 * 60


def test_lone_earning_adds_no_hours_and_open_trip_is_instant():
    act = activity(earnings=[earning(ny(2026, 9, 11, 12))], trips=[trip(ny(2026, 9, 12, 12), minutes=None)])
    sessions = build_sessions(act)
    assert [s.seconds for s in sessions] == [0, 0]


def test_sessions_empty_and_negative_gap():
    assert build_sessions(activity()) == []
    with pytest.raises(ValueError):
        build_sessions(activity(), gap=timedelta(minutes=-1))


def _slices(start, end, tz=NY):
    return [(p.day, p.hour, p.seconds) for p in split_local(Interval(start, end), tz)]


def _by_hour(start, end, tz=NY):
    out: dict[tuple[date, int], int] = {}
    for d, h, s in _slices(start, end, tz):
        out[(d, h)] = out.get((d, h), 0) + s
    return out


def test_split_sums_exactly():
    start = datetime(2026, 9, 11, 21, 7, 13, tzinfo=timezone.utc)
    end = start + timedelta(hours=3, minutes=52, seconds=41)
    assert sum(s for *_, s in _slices(start, end)) == int((end - start).total_seconds())


def test_split_spring_forward():
    # 2026-03-08: New York clocks jump 2:00 → 3:00. 1:30 → 3:30 local is one real hour.
    got = _by_hour(ny(2026, 3, 8, 1, 30), ny(2026, 3, 8, 3, 30))
    assert got == {(date(2026, 3, 8), 1): 1800, (date(2026, 3, 8), 3): 1800}


def test_split_fall_back():
    # 2026-11-01: 1:00–2:00 local happens twice. 00:30 → second 01:30 is two real hours.
    start = ny(2026, 11, 1, 0, 30)
    end = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)          # 01:30 EST (second pass)
    got = _by_hour(start, end)
    assert got == {(date(2026, 11, 1), 0): 1800, (date(2026, 11, 1), 1): 5400}


@pytest.mark.parametrize("tz,first_hour", [("Asia/Kolkata", 15), ("Asia/Kathmandu", 15)])
def test_split_fractional_offsets(tz, first_hour):
    start = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)          # 15:30 IST / 15:45 NPT
    got = _by_hour(start, start + timedelta(hours=1), tz)
    offset_min = 30 if tz == "Asia/Kolkata" else 45
    assert got == {(date(2026, 9, 11), first_hour): (60 - offset_min) * 60,
                   (date(2026, 9, 11), first_hour + 1): offset_min * 60}


def test_split_year_boundary():
    iv = Interval(ny(2026, 12, 31, 23), ny(2027, 1, 1, 1))
    assert seconds_by_local_date([iv], NY) == {date(2026, 12, 31): 3600, date(2027, 1, 1): 3600}


def test_split_empty_interval():
    t = ny(2026, 9, 1, 12)
    assert list(split_local(Interval(t, t), NY)) == []


# =============================================================================================
# temporal: grid & trends
# =============================================================================================


def test_grid_buckets_by_local_hour_and_skips_untimed():
    fri_9pm = ny(2026, 9, 11, 21, 15)                           # Friday 9 PM local = Saturday 01:15 UTC
    act = activity(earnings=[earning(fri_9pm, 2500), earning(ny(2026, 9, 11), 9999, timed=False)],
                   trips=[trip(fri_9pm - timedelta(minutes=30), 60, meters=12_000)],
                   start=date(2026, 9, 7), end=date(2026, 9, 13))
    grid = build_grid(act, build_sessions(act))
    fri9 = grid.cell(4, 21)
    assert fri9.net_minor == 2500 and fri9.weeks == 1
    assert grid.cell(4, 20).seconds == 900 and fri9.seconds == 2700          # trip 8:45–9:45 PM
    assert grid.cell(4, 20).trips == 1 and grid.cell(4, 20).meters == 12_000
    assert grid.total_net_minor == 2500                         # the untimed 9999 is not in the hourly grid
    assert sum(c.net_minor for c in grid.cells) == 2500
    assert grid.weeks_in_period == 1 and grid.typical_net_minor(fri9) == 2500


def test_grid_credits_pay_to_the_hours_the_trip_was_driven():
    # Trip 12:40–1:10 AM Saturday, paid at 1:14 AM: the fare belongs 2/3 to midnight, 1/3 to 1 AM,
    # not all to 1 AM (which would invent a "1 AM peak" at the end of every shift).
    start = ny(2026, 9, 12, 0, 40)
    act = activity(earnings=[earning(start + timedelta(minutes=34), 3001)], trips=[trip(start, 30)],
                   start=date(2026, 9, 7), end=date(2026, 9, 13))
    grid = build_grid(act, build_sessions(act))
    assert (grid.cell(5, 0).net_minor, grid.cell(5, 1).net_minor) == (2001, 1000)       # lossless split
    assert grid.total_net_minor == 3001


def test_unmatched_pay_stays_at_its_timestamp():
    act = activity(earnings=[earning(ny(2026, 9, 11, 15, 10), 500)], trips=[trip(ny(2026, 9, 11, 9), 30)],
                   start=date(2026, 9, 7), end=date(2026, 9, 13))
    assert build_grid(act, []).cell(4, 15).net_minor == 500


def test_airport_cells_merge_into_one_zone():
    trips, cells = [], {}
    for i in range(6):                                   # scattered around TPA across different cells
        p = GeoPoint(TPA.lat + (i % 3 - 1) * 0.006, TPA.lng + (i // 3) * 0.012)
        t = trip(ny(2026, 9, 1 + i, 7), 20, tid=f"a{i}")
        trips.append(t)
        cells[t.id] = (None, geohash(p, 6))
    assert len({c[1] for c in cells.values()}) > 1
    res = build_zones(activity([], trips, start=date(2026, 9, 1), end=date(2026, 9, 30)), cells, min_trips=3)
    assert [(z.label, z.drop_offs) for z in res.zones] == [("TPA airport area", 6)]


def test_grid_weeks_counts_distinct_weeks():
    act = activity(earnings=[earning(ny(2026, 9, 4 + 7 * i, 21)) for i in range(4)] + [earning(ny(2026, 9, 4, 21, 30))],
                   start=date(2026, 8, 31), end=date(2026, 9, 27))
    grid = build_grid(act, [])
    assert grid.cell(4, 21).weeks == 4 and grid.weeks_in_period == 4
    assert grid.typical_net_minor(grid.cell(4, 21)) == 1250     # 5000 over 4 weeks


def test_weeks_spanned():
    assert weeks_spanned(date(2026, 9, 7), date(2026, 9, 13)) == 1
    assert weeks_spanned(date(2026, 9, 13), date(2026, 9, 14)) == 2       # Sunday → Monday


def test_trend_monthly_across_year_boundary():
    act = activity(
        earnings=[earning(ny(2026, 12, 31, 22), 4000), earning(ny(2027, 1, 1, 0, 30), 1000),
                  earning(ny(2027, 2, 1), 700, timed=False, day=date(2027, 2, 1))],
        trips=[trip(ny(2026, 12, 31, 23), 120, meters=30_000)],
        start=date(2026, 12, 1), end=date(2027, 2, 28))
    points = build_trend(act, build_sessions(act), "month")
    assert [p.label for p in points] == ["Dec 26", "Jan 27", "Feb 27"]
    dec, jan, feb = points
    assert (dec.net_minor, jan.net_minor, feb.net_minor) == (4000, 1000, 700)
    # the 10 PM earning is 60 min before the trip (> 45 min gap), so the session is the trip itself:
    # 23:00 → 01:00 local, one hour in each year
    assert (dec.seconds, jan.seconds) == (3600, 3600)
    assert dec.trips == 1 and dec.meters == 30_000 and jan.trips == 0
    assert feb.per_hour_minor is None and dec.per_hour_minor == 4000 and jan.per_hour_minor == 1000


def test_trend_weekly_has_every_bucket_starting_monday():
    act = activity(earnings=[earning(ny(2026, 9, 2, 12))], start=date(2026, 9, 2), end=date(2026, 9, 30))
    points = build_trend(act, [], "week")
    assert [p.start for p in points] == [date(2026, 8, 31), date(2026, 9, 7), date(2026, 9, 14), date(2026, 9, 21),
                                         date(2026, 9, 28)]
    assert [p.empty for p in points] == [False, True, True, True, True]


def test_trend_daily_excludes_session_time_outside_period():
    act = activity(trips=[trip(ny(2026, 9, 30, 23, 30), 60)], start=date(2026, 9, 30), end=date(2026, 9, 30))
    (p,) = build_trend(act, build_sessions(act), "day")
    assert p.seconds == 1800


def test_trend_rejects_unknown_granularity():
    with pytest.raises(ValueError):
        build_trend(activity(), [], "year")


def test_pick_granularity():
    assert [pick_granularity(d) for d in (1, 31, 32, 182, 183, 365)] == ["day", "day", "week", "week", "month",
                                                                           "month"]


# =============================================================================================
# peaks
# =============================================================================================


def week_pattern(weeks=8, *, start=date(2026, 6, 1), hot=((4, 21, 3, 9000),), base_per_hour=2000,
                 base_slots=((1, 12, 3), (2, 17, 3), (5, 14, 3))):
    """Synthetic history. hot/base entries: (weekday, start hour, hours, net for the whole block)."""
    earnings, trips = [], []
    for w in range(weeks):
        monday = start + timedelta(days=7 * w)
        blocks = [(d, h, n, net) for d, h, n, net in hot] + [(d, h, n, base_per_hour * n) for d, h, n in base_slots]
        for d, h, n, net in blocks:
            day = monday + timedelta(days=d)
            t0 = datetime(day.year, day.month, day.day, h, tzinfo=NYZ).astimezone(timezone.utc)
            per = net // n
            for k in range(n):                   # a 50-minute trip each hour, paid 5 minutes after drop-off
                trips.append(trip(t0 + timedelta(hours=k), 50))
                earnings.append(earning(t0 + timedelta(hours=k, minutes=55), per))
    return activity(earnings, trips, start=start, end=start + timedelta(days=7 * weeks - 1))


def test_peak_found_friday_night():
    act = week_pattern()
    res = find_peaks(build_grid(act, build_sessions(act)))
    assert res.status == "ok"
    top = res.windows[0]
    assert (top.weekday, top.start_hour, top.hours) == (4, 21, 3)
    assert top.label == "Friday night" and top.time_range == "9 PM – 12 AM"
    assert top.per_hour_minor > res.baseline_per_hour_minor and top.lift_bp > 11_500
    assert top.confidence == "high" and top.weeks == 8 and top.extra_minor > 0


def _grid(slot_rates: dict[tuple[int, int], int], *, hours=20, weeks=10, base_rate=2000):
    """Hand-built grid: every slot worked `hours` over `weeks` weeks at base_rate, some at other rates."""
    from core.intelligence.temporal import HourCell, HourGrid

    cells = []
    for d in range(7):
        for h in range(24):
            rate = slot_rates.get((d, h), base_rate)
            c = HourCell(d, h, net_minor=rate * hours, seconds=hours * 3600,
                         week_set={date(2026, 1, 5) + timedelta(days=7 * w) for w in range(weeks)})
            cells.append(c)
    return HourGrid(cells, weeks, sum(c.net_minor for c in cells), sum(c.seconds for c in cells))


def test_lift_threshold_is_applied_after_shrinkage():
    g = _grid({(4, 21): 2200, (5, 21): 2600})                  # +10% and +30%
    res = find_peaks(g)
    assert [(w.weekday, w.start_hour) for w in res.windows] == [(5, 21)]
    lone = find_peaks(_grid({(5, 21): 2600}, weeks=2))           # same lift, only 2 weeks of evidence
    assert lone.status == "not_enough_history"


def test_window_is_capped_at_twelve_hours():
    g = _grid({(0, h): 4000 for h in range(24)})                # all of Monday is hot
    lengths = sorted(w.hours for w in find_peaks(g, limit=10).windows if w.weekday == 0)
    assert lengths == [12, 12]


def test_peak_window_wraps_past_midnight_and_week_end():
    act = week_pattern(hot=((4, 22, 4, 16_000), (6, 22, 4, 16_000)))       # Fri 10 PM–2 AM, Sun 10 PM–Mon 2 AM
    res = find_peaks(build_grid(act, build_sessions(act)))
    windows = {(w.weekday, w.start_hour, w.hours) for w in res.windows}
    assert (4, 22, 4) in windows and (6, 22, 4) in windows
    sun = next(w for w in res.windows if w.weekday == 6)
    assert sun.slots == [(6, 22), (6, 23), (0, 0), (0, 1)] and sun.end_hour == 2


def test_one_lucky_order_is_not_a_peak():
    act = week_pattern(hot=())
    lucky = earning(ny(2026, 6, 3, 3, 10), 30_000)                             # one huge 3 AM Wednesday fare
    act.earnings.append(lucky)
    act.trips.append(trip(ny(2026, 6, 3, 2, 50), 20))
    grid = build_grid(act, build_sessions(act))
    res = find_peaks(grid)
    assert all(w.weekday != 2 or w.start_hour not in (2, 3) for w in res.windows)
    # shrinkage keeps the thin slot close to baseline evidence-wise; the weeks rule rejects it outright
    assert grid.cell(2, 3).weeks == 1


def test_shrunk_rate_formula():
    from core.intelligence.temporal import HourCell

    c = HourCell(0, 0, net_minor=6000, seconds=1800)
    assert shrunk_rate(c, 2000) == (6000 * 3600 + PRIOR_S * 2000) // (1800 + PRIOR_S)


def test_peaks_need_history_and_time():
    no_time = activity(earnings=[earning(ny(2026, 9, 4, 21))])
    assert find_peaks(build_grid(no_time, build_sessions(no_time))).status == "no_time_data"
    thin = week_pattern(weeks=2)
    res = find_peaks(build_grid(thin, build_sessions(thin)))
    assert res.status == "not_enough_history" and res.windows == ()


def test_flat_week_has_no_standouts():
    act = week_pattern(hot=())
    res = find_peaks(build_grid(act, build_sessions(act)))
    # every block pays 2000 an hour; the last hour of each is 55 minutes, hence a baseline a little above 2000
    assert res.status == "no_standouts" and 2000 <= res.baseline_per_hour_minor <= 2100


# =============================================================================================
# metrics
# =============================================================================================


def test_efficiency_totals_and_utilization():
    t0 = ny(2026, 9, 11, 18)
    act = activity(earnings=[earning(t0 + timedelta(minutes=20), 3000, fee=750), earning(t0 + timedelta(hours=1), 2000)],
                   trips=[trip(t0, 20, meters=16_093), trip(t0 + timedelta(minutes=40), 20, meters=16_093)])
    e = efficiency(act, build_sessions(act), vehicle_cost=lambda by_day: 1450)
    assert (e.net_minor, e.gross_minor, e.fee_minor) == (5000, 5750, 750)
    assert e.seconds == 3600 and e.trip_seconds == 2400 and e.utilization_bp == 6667
    assert e.per_hour_minor == 5000 and e.per_mile_minor == 250 and e.per_trip_minor == 2500   # 20 mi
    assert e.after_vehicle_minor == 3550 and e.after_vehicle_per_hour_minor == 3550
    assert e.fee_share_bp == 1304 and e.active_days == 1 and e.sessions == 1


def test_irs_vehicle_cost_uses_local_date_rate():
    cost = irs_vehicle_cost()
    mile = 1609
    # 2026: 72.5¢ through June 30, 76¢ from July 1 (Phase 3 tables); 100 miles each side
    before = cost({date(2026, 6, 30): 100 * mile})
    after = cost({date(2026, 7, 1): 100 * mile})
    assert before < after and abs(before - 7250) <= 2 and abs(after - 7600) <= 2


def test_efficiency_without_trips_has_no_hourly_or_car_figures():
    act = activity(earnings=[earning(ny(2026, 9, 11, 18), 1000)])
    e = efficiency(act, build_sessions(act), vehicle_cost=lambda d: 999)
    assert e.per_hour_minor is None and e.per_mile_minor is None and e.vehicle_cost_minor == 0


# =============================================================================================
# zones: storage & privacy
# =============================================================================================


@pytest.fixture
def sealer():
    return FieldSealer("ab" * 32)


def test_zone_record_and_load(db):
    tid = _trip(db, ny(2026, 9, 11, 18))
    store = ZoneStore(db)
    assert store.record(tid, DOWNTOWN, TPA)
    assert store.load() == {tid: (geohash(DOWNTOWN, 6), geohash(TPA, 6))}
    with db.connection() as conn:
        row = dict(conn.execute("SELECT * FROM trip_zones").fetchone())
    assert "27.9" not in json.dumps(row) and "-82" not in json.dumps(row)          # cells only, no coordinates


def test_home_is_never_recorded(db):
    home = [PrivateZone(HOME, 200)]
    store = ZoneStore(db)
    near = GeoPoint(HOME.lat + 0.004, HOME.lng)                                       # ~450 m away
    tid = _trip(db, ny(2026, 9, 11, 18))
    assert store.record(tid, near, DOWNTOWN, private=home)
    assert store.load()[tid] == (None, geohash(DOWNTOWN, 6))
    # outside radius + margin, but still inside home's own cell: still suppressed
    lat0, lat1, lng0, lng1 = geohash_bounds(geohash(HOME, 6))
    corner = GeoPoint(lat0 + 1e-6, lng0 + 1e-6)
    if haversine_m(corner, HOME) > 200 + 750:
        assert store.cell_for(corner, precision=6, private=home) is None
    assert store.cell_for(geohash_center(geohash(HOME, 6)), precision=6, private=home) is None
    tid2 = _trip(db, ny(2026, 9, 11, 19))
    assert not store.record(tid2, near, near, private=home)                          # nothing left to store
    assert tid2 not in store.load()


def test_zones_are_sealed_on_platform_storage(db, sealer):
    tid = _trip(db, ny(2026, 9, 11, 18))
    store = ZoneStore(db, sealer=sealer, require_sealing=True)
    assert store.record(tid, DOWNTOWN, TPA)
    with db.connection() as conn:
        raw = conn.execute("SELECT start_zone, end_zone FROM trip_zones").fetchone()
    assert geohash(DOWNTOWN, 6) not in raw[0] and json.loads(raw[0]).keys() == {"sealed"}
    assert store.load() == {tid: (geohash(DOWNTOWN, 6), geohash(TPA, 6))}
    # no key: nothing readable
    blind = ZoneStore(db)
    assert blind.load() == {tid: (None, None)} and blind.unreadable == 2


def test_sealed_values_are_bound_to_trip_and_column(db, sealer):
    a, b = _trip(db, ny(2026, 9, 11, 18)), _trip(db, ny(2026, 9, 11, 19))
    store = ZoneStore(db, sealer=sealer)
    store.record(a, DOWNTOWN, TPA)
    store.record(b, TPA, DOWNTOWN)
    with db.transaction() as conn:                 # swap a's sealed end into b, and start/end within a
        a_end = conn.execute("SELECT end_zone FROM trip_zones WHERE trip_id = ?", (a,)).fetchone()[0]
        conn.execute("UPDATE trip_zones SET start_zone = ? WHERE trip_id = ?", (a_end, b))
    got = store.load()
    assert got[b][0] is None and store.unreadable == 1


def test_no_plaintext_when_sealing_required_but_unavailable(db):
    tid = _trip(db, ny(2026, 9, 11, 18))
    assert not ZoneStore(db, require_sealing=True).record(tid, DOWNTOWN, TPA)
    assert ZoneStore(db).count() == 0


def test_zone_lifecycle(db):
    store = ZoneStore(db)
    tid = _trip(db, ny(2026, 9, 11, 18))
    assert not store.record(str(uuid.uuid4()), DOWNTOWN, TPA)          # unknown trip
    with pytest.raises(ValueError):
        store.record(tid, DOWNTOWN, TPA, precision=9)
    store.record(tid, DOWNTOWN, TPA)
    store.record(tid, TPA, TPA)                                          # re-record replaces
    assert store.load()[tid] == (geohash(TPA, 6), geohash(TPA, 6))
    with db.transaction() as conn:
        conn.execute("UPDATE mileage_logs SET deleted_at = '2026-09-12T00:00:00Z' WHERE id = ?", (tid,))
    assert store.load() == {}                                           # soft-deleted trips are ignored
    assert store.forget_all() == 1 and store.count() == 0


def test_trip_zones_is_local_only(db):
    with db.connection() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trip_zones)")}
    assert "sync_status" not in cols and "version" not in cols
    from ui.state import SYNCED_TABLES

    assert "trip_zones" not in SYNCED_TABLES


def test_hard_delete_cascades(db):
    store = ZoneStore(db)
    tid = _trip(db, ny(2026, 9, 11, 18))
    store.record(tid, DOWNTOWN, TPA)
    with db.transaction() as conn:
        conn.execute("DELETE FROM mileage_logs WHERE id = ?", (tid,))
    assert store.count() == 0


# =============================================================================================
# spatial
# =============================================================================================


def test_attribute_earnings_by_nearest_drop_off():
    t0 = ny(2026, 9, 11, 18)
    a = trip(t0, 20, tid="a", platform="uber")                        # ends 18:20
    b = trip(t0 + timedelta(minutes=30), 20, tid="b", platform="uber")  # ends 19:10
    c = trip(t0, 20, tid="c", platform="dash")
    earns = [
        earning(t0 + timedelta(minutes=25), 1000, platform="uber"),    # 5 min after a → a
        earning(t0 + timedelta(minutes=45), 700, platform="uber"),     # 25 after a, 5 before b → b
        earning(t0 + timedelta(minutes=21), 300, platform="dash"),     # platform picks c
        earning(t0 + timedelta(hours=3), 999, platform="uber"),        # too late for anything
        earning(t0 + timedelta(minutes=22), 50, timed=False),          # untimed: never matched
    ]
    got = attribute_earnings(activity(earns, [a, b, c]))
    assert got == {"a": 1000, "b": 700, "c": 300}


def _neighbor(point: GeoPoint, d_lat_cells=0, d_lng_cells=0) -> GeoPoint:
    lat0, lat1, lng0, lng1 = geohash_bounds(geohash(point, 6))
    c = geohash_center(geohash(point, 6))
    return GeoPoint(c.lat + d_lat_cells * (lat1 - lat0), c.lng + d_lng_cells * (lng1 - lng0))


def _zone_setup():
    east = _neighbor(DOWNTOWN, 0, 1)
    north = _neighbor(DOWNTOWN, 1, 0)
    trips, earns, cells = [], [], {}
    plan = [(DOWNTOWN, 5, 1500), (east, 3, 2500), (north, 3, 800), (TPA, 4, 3000), (_neighbor(DOWNTOWN, 0, 3), 1, 9000)]
    k = 0
    for point, n, net in plan:
        for _ in range(n):
            start = ny(2026, 9, 1 + k % 28, 8 if point == TPA else 21)
            t = trip(start, 20, tid=f"t{k}")
            trips.append(t)
            earns.append(earning(t.ended_at + timedelta(minutes=5), net))
            cells[t.id] = (None, geohash(point, 6))
            k += 1
    return activity(earns, trips, start=date(2026, 9, 1), end=date(2026, 9, 30)), cells, east, north


def test_build_zones_ranks_hides_and_labels():
    act, cells, east, north = _zone_setup()
    res = build_zones(act, cells, reference=("TPA", TPA), min_trips=3)
    assert res.status == "ok"
    assert [z.rank for z in res.zones] == [1, 2, 3, 4]
    top = res.zones[0]
    assert top.near_airport == "TPA" and top.label == "TPA airport area" and top.drop_offs == 4
    assert top.per_trip_minor == 3000 and top.best_time.endswith("mornings")
    # shrunk per-trip ranking: TPA 3000/trip (4) > east 2500 (3) > downtown 1500 (5) > north 800 (3)
    assert [z.per_trip_minor for z in res.zones] == [3000, 2500, 1500, 800]
    assert res.hidden_areas == 1 and res.hidden_drop_offs == 1                    # the single 9000 drop-off
    down = next(z for z in res.zones if z.cell == geohash(DOWNTOWN, 6))
    assert down.direction == "ESE" and down.label == "ESE of TPA" and 7_000 < down.distance_m < 9_000
    assert res.trips_with_zones == len(act.trips) and res.coverage_bp == 10_000


def test_heat_grid_is_relative_and_coordinate_free():
    act, cells, east, north = _zone_setup()
    res = build_zones(act, cells, reference=("TPA", TPA), min_trips=3)
    grid = res.grid
    assert sorted(c.rank for c in grid.cells) == [1, 2, 3, 4]          # TPA is ~8 cells west: all fit in 12
    pos = {z.cell: next((c.row, c.col) for c in grid.cells if c.rank == z.rank) for z in res.zones}
    d = pos[geohash(DOWNTOWN, 6)]
    assert pos[geohash(east, 6)] == (d[0], d[1] + 1)            # east → next column
    assert pos[geohash(north, 6)] == (d[0] - 1, d[1])           # north → row above
    assert pos[geohash(TPA, 6)][1] < d[1]                       # the airport is to the west
    for c in grid.cells:
        assert set(vars(c)) == {"row", "col", "value_minor", "drop_offs", "rank"}      # no coordinates


def test_heat_grid_window_keeps_the_most_zones():
    act, cells, east, north = _zone_setup()
    zones = build_zones(act, cells, reference=("TPA", TPA), min_trips=3).zones
    small = heat_grid(zones, max_size=3)                        # TPA (#1) alone vs the 3-zone downtown cluster
    assert sorted(c.rank for c in small.cells) == [2, 3, 4] and (small.rows, small.cols) <= (3, 3)


def test_build_zones_sparse_and_empty():
    act, cells, *_ = _zone_setup()
    res = build_zones(act, cells, min_trips=50)
    assert res.status == "too_sparse" and res.zones == [] and res.hidden_areas == 5
    assert build_zones(act, {}).status == "no_zones"
    with pytest.raises(ValueError):
        build_zones(act, cells, min_trips=0)


def test_heat_grid_empty():
    g = heat_grid([])
    assert (g.rows, g.cols, g.cells) == (0, 0, [])


# =============================================================================================
# engine & periods
# =============================================================================================


def test_period_bounds():
    today = date(2026, 9, 26)
    assert period_bounds("30d", today) == (date(2026, 8, 28), today)
    assert period_bounds("ytd", today) == (date(2026, 1, 1), today)
    assert period_bounds("365d", today) == (date(2025, 9, 27), today)
    with pytest.raises(ValueError):
        period_bounds("forever", today)


def test_previous_bounds():
    assert previous_bounds(date(2026, 8, 28), date(2026, 9, 26)) == (date(2026, 7, 29), date(2026, 8, 27))
    assert previous_bounds(date(2026, 1, 1), date(2026, 9, 26)) == (date(2025, 1, 1), date(2025, 9, 26))
    assert previous_bounds(date(2028, 1, 1), date(2028, 2, 29)) == (date(2027, 1, 1), date(2027, 2, 28))


def seed_history(db, *, weeks=10, today=date(2026, 9, 26), zones=True):
    """Friday nights pay double; weekday lunches are average. Returns platform id."""
    pid = _platform(db)
    store = ZoneStore(db)
    monday = today - timedelta(days=today.weekday()) - timedelta(days=7 * weeks)
    for w in range(weeks):
        base = monday + timedelta(days=7 * w)
        for dow, hour, n, net in ((4, 21, 3, 4000), (1, 12, 3, 2000), (2, 12, 3, 2000), (5, 14, 3, 2000)):
            day = base + timedelta(days=dow)
            for k in range(n):
                start = datetime(day.year, day.month, day.day, hour, tzinfo=NYZ) + timedelta(hours=k)
                tid = _trip(db, start, minutes=40, meters=12_000, platform=pid)
                end = start + timedelta(minutes=45)
                _txn(db, pid, end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"), gross=net + 500, fee=500)
                if zones:
                    store.record(tid, DOWNTOWN, TPA if dow == 4 else DOWNTOWN)
    return pid


def test_build_report_end_to_end(db):
    seed_history(db)
    r = build_report(db, today=date(2026, 9, 26), tz=NY, currency="USD", period="90d", zone_store=ZoneStore(db),
                     reference=("TPA", TPA), vehicle_cost=lambda by_day: 0)
    assert not r.empty and r.granularity == "week"
    assert r.peaks.status == "ok"
    top = r.peaks.windows[0]
    assert (top.weekday, top.start_hour) == (4, 21) and top.hours >= 3
    assert r.spatial.status == "ok" and r.spatial.zones[0].label == "TPA airport area"
    assert r.efficiency.trips == r.activity.trips.__len__() and r.efficiency.per_hour_minor > 0
    assert sum(p.net_minor for p in r.trend) == r.efficiency.net_minor
    assert sum(p.seconds for p in r.trend) == r.efficiency.seconds == r.grid.total_seconds
    assert r.notes == []


def test_build_report_empty_database(db):
    r = build_report(db, today=date(2026, 9, 26), tz=NY, currency="USD", period="30d", zone_store=ZoneStore(db))
    assert r.empty and r.peaks.status == "no_time_data" and r.spatial.status == "no_zones"
    assert len(r.trend) == 30 and all(p.empty for p in r.trend)
    assert r.change("net_minor") is None


def test_report_notes_explain_exclusions(db):
    pid = _platform(db)
    _txn(db, pid, "2026-09-20")
    _txn(db, pid, "2026-09-20T12:00:00.000Z", currency="EUR")
    _trip(db, ny(2026, 9, 20, 9), review="needs_review")
    r = build_report(db, today=date(2026, 9, 26), tz=NY, currency="USD", period="30d", vehicle_cost=lambda d: 0)
    text = " ".join(r.notes)
    assert "no time of day" in text and "foreign-currency" in text and "waiting for review" in text
    assert "per-hour figures aren't available" in text


# =============================================================================================
# motion integration
# =============================================================================================


def _motion_run(db, recorder):
    from core.motion.classifier import ClassifierContext
    from core.motion.sentry import DetectedTrip, TripEnded, TripStarted
    from core.motion.service import AutoTripSaved, MotionService

    from core.fleet.geo import GpsFix

    svc = MotionService(db, context=lambda: ClassifierContext(tz=NY), zone_recorder=recorder)
    t0 = datetime(2026, 9, 11, 22, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=18)
    fixes = tuple(GpsFix(GeoPoint(DOWNTOWN.lat + (TPA.lat - DOWNTOWN.lat) * i / 18,
                                  DOWNTOWN.lng + (TPA.lng - DOWNTOWN.lng) * i / 18), t0 + timedelta(minutes=i), 8.0)
                  for i in range(19))
    svc._dispatch([TripStarted(t0, DOWNTOWN, 8.0)])
    out = svc._dispatch([TripEnded(DetectedTrip(t0, t1, DOWNTOWN, TPA, 8000, 20.0, fixes), "stopped")])
    assert isinstance(out[0], AutoTripSaved)
    return out[0].trip_id


def test_motion_service_records_zones(db):
    store = ZoneStore(db)
    tid = _motion_run(db, lambda trip_id, s, e: store.record(trip_id, s, e))
    assert store.load()[tid] == (geohash(DOWNTOWN, 6), geohash(TPA, 6))


def test_zone_failure_never_breaks_trip_saving(db, caplog):
    def boom(*a):
        raise RuntimeError("disk full")

    tid = _motion_run(db, boom)
    with db.connection() as conn:
        assert conn.execute("SELECT trip_state FROM mileage_logs WHERE id = ?", (tid,)).fetchone()[0] == "completed"
    assert "Recording trip zones failed" in caplog.text


# =============================================================================================
# privacy: the package cannot reach the network
# =============================================================================================


FORBIDDEN_IMPORTS = ("urllib", "http", "socket", "requests", "httpx", "aiohttp", "ssl", "core.ai", "core.fleet.http",
                     "core.telemetry", "ftplib", "smtplib")


def test_intelligence_package_imports_no_network_code():
    pkg = Path(__file__).resolve().parents[1] / "core" / "intelligence"
    offenders = []
    for path in pkg.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                if any(n == f or n.startswith(f + ".") for f in FORBIDDEN_IMPORTS):
                    offenders.append(f"{path.name}: {n}")
    assert offenders == []
