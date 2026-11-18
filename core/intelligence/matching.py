"""
Match earnings to the trips that produced them (locally, by time).

Statements don't say which trip an earning belongs to. An earning is matched
to the trip that ended closest to it, within 10 minutes before to 45 minutes
after the drop-off (payouts post a little after the trip), on the same
platform when both sides know it. Each earning is used at most once; a trip
may collect several (fare + tip).

Used by the hour-of-week grid (a trip's pay belongs to the hours it was
driven, not to the minute the payout posted, otherwise every shift's pay
spills into the hour after it ends) and by zone ranking.
"""

from __future__ import annotations

import bisect
from datetime import timedelta

from core.intelligence.models import Activity, TripRecord

MATCH_BEFORE = timedelta(minutes=10)
MATCH_AFTER = timedelta(minutes=45)


def match_earnings(activity: Activity) -> dict[int, TripRecord]:
    """Index into `activity.earnings` → matched trip (timed earnings, trips with an end time)."""
    ended = sorted((t for t in activity.trips if t.ended_at is not None), key=lambda t: t.ended_at)
    ends = [t.ended_at for t in ended]
    out: dict[int, TripRecord] = {}
    for i, e in enumerate(activity.earnings):
        if not e.time_known:
            continue
        lo = bisect.bisect_left(ends, e.at - MATCH_AFTER)
        hi = bisect.bisect_right(ends, e.at + MATCH_BEFORE)
        best: TripRecord | None = None
        best_gap: timedelta | None = None
        for t in ended[lo:hi]:
            if e.platform_id and t.platform_id and e.platform_id != t.platform_id:
                continue
            gap = abs(e.at - t.ended_at)          # type: ignore[operator]
            if best_gap is None or gap < best_gap:
                best, best_gap = t, gap
        if best is not None:
            out[i] = best
    return out


def earnings_by_trip(activity: Activity, matches: dict[int, TripRecord] | None = None) -> dict[str, int]:
    """trip id → net earnings matched to it."""
    matches = match_earnings(activity) if matches is None else matches
    out: dict[str, int] = {}
    for i, t in matches.items():
        out[t.id] = out.get(t.id, 0) + activity.earnings[i].net_minor
    return out
