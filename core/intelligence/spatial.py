"""
Spatial aggregation over coarse trip zones (see zones.py): where the user's
drop-offs happen and which areas pay best, with no coordinates anywhere in
the output.

Earnings → trips
----------------
Earnings are matched to drop-offs by time (see matching.py).

Airports
--------
GPS scatter spreads one airport over several cells, so every cell within
3 km of a known airport folds into a single "TPA airport area" zone.

Ranking
-------
"Pays best" means matched earnings *per trip*, pulled toward the user's
overall per-trip average by `PRIOR_TRIPS` trips of average evidence (the same
idea as peak detection), so three lucky fares can't top a zone with sixty
solid ones. Zones with no matched earnings rank last, by drop-offs.

What the output contains
------------------------
Zones with at least `min_trips` drop-offs, each with counts, matched
earnings, a best time-of-week profile, and a *relative* label ("TPA airport
area", "4.2 km NE of TPA"). Smaller areas are only counted ("3 other
areas"), so a one-off drop-off at a friend's address is never singled out.

The heat grid places those zones by their position relative to each other
(row/column offsets); it carries no latitude/longitude. Cell ids stay inside
this module's callers on the device.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from core.fleet.demand import AIRPORTS
from core.fleet.geo import GeoPoint, bearing_deg, compass16, geohash_bounds, geohash_center, haversine_m
from core.intelligence.matching import earnings_by_trip
from core.intelligence.models import Activity, TripRecord
from core.intelligence.peaks import part_of_day
from core.intelligence.rates import div_round
from core.timeutil import zone

DEFAULT_MIN_TRIPS = 3
AIRPORT_RADIUS_M = 3_000
MAX_GRID = 12                 # most rows/cols shown (~12 km at precision 6)
PRIOR_TRIPS = 3

# Kept as a name for callers/tests: trip id → matched net earnings.
attribute_earnings = earnings_by_trip


@dataclass
class ZoneStat:
    cell: str                         # representative cell; stays on device; never shown or sent
    drop_offs: int = 0
    pickups: int = 0
    matched_trips: int = 0            # drop-offs with earnings matched
    net_minor: int = 0                # matched earnings for drop-offs here
    meters: int = 0                   # distance of trips ending here
    label: str = ""
    near_airport: str | None = None   # IATA code when inside an airport area
    distance_m: int | None = None     # from the reference point
    direction: str | None = None
    best_time: str | None = None      # e.g. "weekday mornings"
    best_time_share_bp: int = 0
    rank: int = 0
    score_minor: int | None = None    # shrunk per-trip earnings used for ranking

    @property
    def per_trip_minor(self) -> int | None:
        return div_round(self.net_minor, self.matched_trips) if self.matched_trips else None


@dataclass
class HeatCell:
    row: int
    col: int
    value_minor: int                   # matched earnings per trip (0 when none matched)
    drop_offs: int
    rank: int                          # 1-based zone rank


@dataclass
class HeatGrid:
    rows: int
    cols: int
    cells: list[HeatCell] = field(default_factory=list)

    @property
    def max_value(self) -> int:
        return max((c.value_minor for c in self.cells), default=0)

    @property
    def max_drop_offs(self) -> int:
        return max((c.drop_offs for c in self.cells), default=0)


@dataclass
class SpatialResult:
    zones: list[ZoneStat]
    grid: HeatGrid
    hidden_areas: int                  # areas below the min-trips threshold
    hidden_drop_offs: int
    trips_with_zones: int
    trips_total: int
    unreadable: int = 0
    status: str = "ok"                 # ok | no_zones | too_sparse
    message: str = ""

    @property
    def coverage_bp(self) -> int:
        return div_round(self.trips_with_zones * 10_000, self.trips_total) if self.trips_total else 0


def _time_profile(trips: list[TripRecord], tz: str) -> tuple[str | None, int]:
    z = zone(tz)
    counts: dict[str, int] = {}
    for t in trips:
        when = (t.ended_at or t.started_at).astimezone(z)
        key = f"{'weekend' if when.weekday() >= 5 else 'weekday'} {part_of_day(when.hour)}s"
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None, 0
    key = max(counts, key=lambda k: (counts[k], k))
    return key, div_round(counts[key] * 10_000, len(trips))


def airport_for(cell: str) -> str | None:
    center = geohash_center(cell)
    for a in AIRPORTS.values():
        if haversine_m(center, a.location) <= AIRPORT_RADIUS_M:
            return a.iata
    return None


def _describe(stat: ZoneStat, reference: tuple[str, GeoPoint] | None) -> None:
    center = geohash_center(stat.cell)
    iata = airport_for(stat.cell)
    if iata:
        stat.near_airport = iata
        stat.label = f"{iata} airport area"
        return
    if reference is not None:
        name, point = reference
        stat.distance_m = round(haversine_m(point, center))
        stat.direction = compass16(bearing_deg(point, center))
        stat.label = f"{stat.direction} of {name}"
    else:
        stat.label = "Area"


def build_zones(activity: Activity, cells: Mapping[str, tuple[str | None, str | None]], *,
                reference: tuple[str, GeoPoint] | None = None, min_trips: int = DEFAULT_MIN_TRIPS,
                limit: int = 8, unreadable: int = 0) -> SpatialResult:
    if min_trips < 1:
        raise ValueError("min_trips must be >= 1")
    matched = earnings_by_trip(activity)
    airports: dict[str, str | None] = {}

    def key_for(cell: str) -> str:
        if cell not in airports:
            airports[cell] = airport_for(cell)
        return f"airport:{airports[cell]}" if airports[cell] else cell

    stats: dict[str, ZoneStat] = {}
    trips_by_key: dict[str, list[TripRecord]] = {}
    cell_counts: dict[str, dict[str, int]] = {}
    with_zone = 0
    for t in activity.trips:
        start_cell, end_cell = cells.get(t.id, (None, None))
        if start_cell or end_cell:
            with_zone += 1
        if start_cell:
            stats.setdefault(key_for(start_cell), ZoneStat(start_cell)).pickups += 1
        if end_cell:
            k = key_for(end_cell)
            s = stats.setdefault(k, ZoneStat(end_cell))
            s.drop_offs += 1
            s.meters += t.meters
            trips_by_key.setdefault(k, []).append(t)
            counts = cell_counts.setdefault(k, {})
            counts[end_cell] = counts.get(end_cell, 0) + 1
            if t.id in matched:
                s.matched_trips += 1
                s.net_minor += matched[t.id]
    for k, counts in cell_counts.items():         # an airport zone sits at its busiest cell
        stats[k].cell = max(counts, key=lambda c: (counts[c], c))

    empty_grid = HeatGrid(0, 0)
    if not stats:
        return SpatialResult([], empty_grid, 0, 0, with_zone, len(activity.trips), unreadable, "no_zones",
                             "Zones appear once trip detection has recorded some drop-offs.")
    keyed = list(stats.items())
    shown_keys = {k for k, s in keyed if s.drop_offs >= min_trips}
    shown = [s for k, s in keyed if k in shown_keys]
    hidden = [s for k, s in keyed if k not in shown_keys]
    trips_by_cell = {stats[k].cell: v for k, v in trips_by_key.items()}
    hidden_drop = sum(s.drop_offs for s in hidden)
    if not shown:
        return SpatialResult([], empty_grid, len(hidden), hidden_drop, with_zone, len(activity.trips), unreadable,
                             "too_sparse", f"Areas show up after {min_trips} or more drop-offs in the same place.")

    matched_total = sum(s.net_minor for s in stats.values())
    matched_trips = sum(s.matched_trips for s in stats.values())
    mean = div_round(matched_total, matched_trips) if matched_trips else 0
    for s in shown:
        if s.matched_trips:
            s.score_minor = div_round(s.net_minor + PRIOR_TRIPS * mean, s.matched_trips + PRIOR_TRIPS)
    shown.sort(key=lambda s: (s.score_minor is not None, s.score_minor or 0, s.drop_offs, s.cell), reverse=True)
    for i, s in enumerate(shown, start=1):
        s.rank = i
        _describe(s, reference)
        s.best_time, s.best_time_share_bp = _time_profile(trips_by_cell.get(s.cell, []), activity.tz)
    extra = shown[limit:]
    shown = shown[:limit]
    return SpatialResult(shown, heat_grid(shown), len(hidden) + len(extra),
                         hidden_drop + sum(s.drop_offs for s in extra), with_zone, len(activity.trips), unreadable,
                         "ok", "Areas ranked by what a drop-off there typically earns you.")


def heat_grid(zones: list[ZoneStat], max_size: int = MAX_GRID) -> HeatGrid:
    """Relative row/column layout of zones (north up). When they don't all fit, the
    max_size × max_size window holding the most zones is shown (ties: the better-ranked)."""
    if not zones:
        return HeatGrid(0, 0)
    lat0, lat1, lng0, lng1 = geohash_bounds(zones[0].cell)
    h, w = lat1 - lat0, lng1 - lng0
    origin = geohash_center(zones[0].cell)
    pos = []
    for z in zones:
        c = geohash_center(z.cell)
        pos.append((round((origin.lat - c.lat) / h), round((c.lng - origin.lng) / w), z))   # rows grow southward

    def window(r0: int, c0: int) -> list[tuple[int, int, ZoneStat]]:
        return [p for p in pos if r0 <= p[0] < r0 + max_size and c0 <= p[1] < c0 + max_size]

    best: list[tuple[int, int, ZoneStat]] = []
    best_key: tuple[int, int] | None = None
    for r0 in {p[0] - i for p in pos for i in range(max_size)}:
        for c0 in {p[1] - j for p in pos for j in range(max_size)}:
            inside = window(r0, c0)
            key = (len(inside), -sum(p[2].rank for p in inside))
            if best_key is None or key > best_key:
                best, best_key = inside, key
    min_r = min(p[0] for p in best)
    min_c = min(p[1] for p in best)
    cells = [HeatCell(dr - min_r, dc - min_c, z.per_trip_minor or 0, z.drop_offs, z.rank) for dr, dc, z in best]
    return HeatGrid(max(c.row for c in cells) + 1, max(c.col for c in cells) + 1, cells)
