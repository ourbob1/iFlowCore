"""
Work sessions and exact local-time splitting.

Hours worked
------------
Gig apps rarely export "online time", so it is reconstructed locally: every
timed trip is an interval, every timed earning is an instant, and anything
closer together than `gap` (default 45 minutes: a slow stretch between
orders still counts as working) joins one session. Session length is the
time from its first to its last event. A lone earning with no trips around
it is a zero-length session: it adds money but no hours, so per-hour rates
are never inflated by invented time.

Local-time splitting
--------------------
`split_local` cuts an interval into pieces that each sit inside one local
clock hour. It walks a 15-minute UTC grid: every real timezone offset (and
every DST jump) is a multiple of 15 minutes, so each slice lies inside a
single local hour, whatever the zone: America/New_York on DST days,
Asia/Kolkata (+5:30), Asia/Kathmandu (+5:45). Times are whole seconds, so the
pieces always add up exactly to the interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Iterator

from core.intelligence.models import Activity, Interval, Session
from core.timeutil import zone

DEFAULT_GAP = timedelta(minutes=45)
_GRID = timedelta(minutes=15)


def _whole(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(microsecond=0)


@dataclass(frozen=True)
class _Event:
    start: datetime
    end: datetime
    trip: bool


def build_sessions(activity: Activity, *, gap: timedelta = DEFAULT_GAP) -> list[Session]:
    if gap < timedelta(0):
        raise ValueError("gap must be >= 0")
    events: list[_Event] = []
    for t in activity.trips:
        start = _whole(t.started_at)
        end = _whole(t.ended_at) if t.ended_at is not None else start
        events.append(_Event(start, max(start, end), True))
    for e in activity.earnings:
        if e.time_known:
            at = _whole(e.at)
            events.append(_Event(at, at, False))
    events.sort(key=lambda ev: (ev.start, ev.end))

    sessions: list[Session] = []
    cur_start = cur_end = None
    trips = earns = 0
    for ev in events:
        if cur_start is not None and ev.start - cur_end <= gap:
            cur_end = max(cur_end, ev.end)
        else:
            if cur_start is not None:
                sessions.append(Session(cur_start, cur_end, trips, earns))
            cur_start, cur_end, trips, earns = ev.start, ev.end, 0, 0
        trips += ev.trip
        earns += not ev.trip
    if cur_start is not None:
        sessions.append(Session(cur_start, cur_end, trips, earns))
    return sessions


@dataclass(frozen=True)
class LocalSlice:
    day: date          # local calendar date
    weekday: int       # 0 = Monday
    hour: int          # 0–23 local
    seconds: int


def split_local(interval: Interval | Session, tz: str) -> Iterator[LocalSlice]:
    """Pieces of the interval, each inside one local clock hour (see module doc)."""
    z = zone(tz)
    cur, end = _whole(interval.start), _whole(interval.end)
    while cur < end:
        floored = cur.replace(minute=cur.minute - cur.minute % 15, second=0)
        nxt = min(floored + _GRID, end)
        local = cur.astimezone(z)
        yield LocalSlice(local.date(), local.weekday(), local.hour, int((nxt - cur).total_seconds()))
        cur = nxt


def seconds_by_local_date(intervals: Iterable[Interval | Session], tz: str) -> dict[date, int]:
    out: dict[date, int] = {}
    for iv in intervals:
        for piece in split_local(iv, tz):
            out[piece.day] = out.get(piece.day, 0) + piece.seconds
    return out
