"""
Temporal aggregation: the hour-of-week grid and trend series.

Hour-of-week grid (7 × 24, local time)
  earnings  net earnings in that local hour. Pay matched to a trip is spread
            over the minutes that trip was driven (exactly, with lossless
            integer allocation); unmatched pay uses its own timestamp.
            Without this, a payout posting a few minutes after the last trip
            of a shift would credit the hour *after* the shift and invent a
            fake peak. Date-only imports are left out; see data.py.
  seconds   worked time from sessions, split exactly by local hour
  trips     business trips that started in that hour, with their meters
  weeks     how many different weeks had any activity in that slot: the
            evidence count peak detection relies on

Trends (day / week / month buckets)
  Every bucket in the period is present, empty ones included, so lines stay
  continuous. Weeks start on Monday (ISO). Buckets are local calendar
  periods, so a session from 23:00 on Dec 31 to 01:00 on Jan 1 puts one hour
  in each year.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from core.financial.money import allocate
from core.intelligence.matching import match_earnings
from core.intelligence.models import WEEKDAYS, Activity, Interval, Session
from core.intelligence.rates import div_round, per_hour, per_mile
from core.intelligence.sessions import split_local
from core.timeutil import zone

Granularity = Literal["day", "week", "month"]
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


# --- hour-of-week grid ------------------------------------------------------------------


@dataclass
class HourCell:
    weekday: int
    hour: int
    net_minor: int = 0
    gross_minor: int = 0
    seconds: int = 0
    trips: int = 0
    meters: int = 0
    week_set: set[date] = field(default_factory=set)

    @property
    def weeks(self) -> int:
        return len(self.week_set)

    @property
    def index(self) -> int:
        return self.weekday * 24 + self.hour

    @property
    def per_hour_minor(self) -> int | None:
        return per_hour(self.net_minor, self.seconds)

    @property
    def label(self) -> str:
        return f"{WEEKDAYS[self.weekday]} {hour_label(self.hour)}"


def hour_label(hour: int) -> str:
    h = hour % 24
    return "12 AM" if h == 0 else "12 PM" if h == 12 else f"{h % 12} {'AM' if h < 12 else 'PM'}"


@dataclass
class HourGrid:
    cells: list[HourCell]                  # 168, index = weekday * 24 + hour
    weeks_in_period: int
    total_net_minor: int                   # timed earnings only
    total_seconds: int

    def cell(self, weekday: int, hour: int) -> HourCell:
        return self.cells[weekday * 24 + hour]

    @property
    def per_hour_minor(self) -> int | None:
        return per_hour(self.total_net_minor, self.total_seconds)

    def typical_net_minor(self, cell: HourCell) -> int:
        """Average net for this slot per week of the period (0 when nothing happened)."""
        return per_week(cell.net_minor, self.weeks_in_period)


def per_week(amount_minor: int, weeks: int) -> int:
    return div_round(amount_minor, weeks) if weeks > 0 else 0


def weeks_spanned(start: date, end: date) -> int:
    return (week_start(end) - week_start(start)).days // 7 + 1


def build_grid(activity: Activity, sessions: list[Session]) -> HourGrid:
    tz = activity.tz
    z = zone(tz)
    cells = [HourCell(d, h) for d in range(7) for h in range(24)]
    total_net = total_s = 0
    matches = match_earnings(activity)
    for i, e in enumerate(activity.earnings):
        if not e.time_known:
            continue
        trip = matches.get(i)
        pieces = list(split_local(Interval(trip.started_at, trip.ended_at), tz)) if trip is not None else []
        if pieces:
            nets = allocate(e.net_minor, [p.seconds for p in pieces])
            grosses = allocate(e.gross_minor, [p.seconds for p in pieces])
            for p, net, gross in zip(pieces, nets, grosses):
                c = cells[p.weekday * 24 + p.hour]
                c.net_minor += net
                c.gross_minor += gross
                c.week_set.add(week_start(p.day))
        else:
            local = e.at.astimezone(z)
            c = cells[local.weekday() * 24 + local.hour]
            c.net_minor += e.net_minor
            c.gross_minor += e.gross_minor
            c.week_set.add(week_start(local.date()))
        total_net += e.net_minor
    for s in sessions:
        for piece in split_local(s, tz):
            if not activity.start <= piece.day <= activity.end:
                continue                    # a session running past the period edge
            c = cells[piece.weekday * 24 + piece.hour]
            c.seconds += piece.seconds
            c.week_set.add(week_start(piece.day))
            total_s += piece.seconds
    for t in activity.trips:
        local = t.started_at.astimezone(z)
        c = cells[local.weekday() * 24 + local.hour]
        c.trips += 1
        c.meters += t.meters
        c.week_set.add(week_start(local.date()))
    return HourGrid(cells, weeks_spanned(activity.start, activity.end), total_net, total_s)


# --- trends --------------------------------------------------------------------------------


@dataclass
class TrendPoint:
    start: date
    end: date                              # inclusive
    label: str
    net_minor: int = 0
    gross_minor: int = 0
    fee_minor: int = 0
    seconds: int = 0
    meters: int = 0
    trips: int = 0

    @property
    def per_hour_minor(self) -> int | None:
        return per_hour(self.net_minor, self.seconds)

    @property
    def per_mile_minor(self) -> int | None:
        return per_mile(self.net_minor, self.meters)

    @property
    def empty(self) -> bool:
        return not (self.net_minor or self.gross_minor or self.seconds or self.meters or self.trips)


def _bucket_start(d: date, g: Granularity) -> date:
    if g == "day":
        return d
    if g == "week":
        return week_start(d)
    return d.replace(day=1)


def _next(d: date, g: Granularity) -> date:
    if g == "day":
        return d + timedelta(days=1)
    if g == "week":
        return d + timedelta(days=7)
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def _label(d: date, g: Granularity, multi_year: bool) -> str:
    if g == "month":
        return f"{MONTHS[d.month - 1]} {d.year % 100:02d}" if multi_year else MONTHS[d.month - 1]
    return f"{MONTHS[d.month - 1]} {d.day}"


def build_trend(activity: Activity, sessions: list[Session], granularity: Granularity) -> list[TrendPoint]:
    if granularity not in ("day", "week", "month"):
        raise ValueError(f"Unknown granularity {granularity!r}")
    z = zone(activity.tz)
    multi_year = activity.start.year != activity.end.year
    points: dict[date, TrendPoint] = {}
    cur = _bucket_start(activity.start, granularity)
    while cur <= activity.end:
        nxt = _next(cur, granularity)
        points[cur] = TrendPoint(cur, nxt - timedelta(days=1), _label(cur, granularity, multi_year))
        cur = nxt

    def bucket(d: date) -> TrendPoint | None:
        if not activity.start <= d <= activity.end:
            return None
        return points.get(_bucket_start(d, granularity))

    for e in activity.earnings:
        if (p := bucket(e.day)) is not None:
            p.net_minor += e.net_minor
            p.gross_minor += e.gross_minor
            p.fee_minor += e.fee_minor
    for s in sessions:
        for piece in split_local(s, activity.tz):
            if (p := bucket(piece.day)) is not None:
                p.seconds += piece.seconds
    for t in activity.trips:
        if (p := bucket(t.started_at.astimezone(z).date())) is not None:
            p.meters += t.meters
            p.trips += 1
    return list(points.values())


def pick_granularity(days: int) -> Granularity:
    """Readable bucket counts: ≤ 31 days → daily, ≤ 26 weeks → weekly, else monthly."""
    if days <= 31:
        return "day"
    return "week" if days <= 182 else "month"
