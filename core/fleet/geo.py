"""
Geodesy helpers: great-circle distance, GPS route distance, geohash.

Floats are fine here — these are measurements, not money — but every
distance that leaves this module is rounded to integer meters, matching the
storage convention.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

EARTH_RADIUS_M = 6_371_008.8          # IUGG mean Earth radius
_GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz"


@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lng: float

    def __post_init__(self) -> None:
        if not (-90.0 <= self.lat <= 90.0) or not (-180.0 <= self.lng <= 180.0):
            raise ValueError(f"Invalid coordinates ({self.lat}, {self.lng})")


@dataclass(frozen=True)
class GpsFix:
    """One location sample from the device."""

    point: GeoPoint
    at: datetime                       # timezone-aware
    accuracy_m: float | None = None    # horizontal accuracy reported by the OS


def haversine_m(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance in meters (float)."""
    phi1, phi2 = math.radians(a.lat), math.radians(b.lat)
    dphi = phi2 - phi1
    dlmb = math.radians(b.lng - a.lng)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def straight_line_m(a: GeoPoint, b: GeoPoint) -> int:
    return round(haversine_m(a, b))


@dataclass(frozen=True)
class RouteResult:
    distance_m: int
    kept_fixes: int
    dropped_inaccurate: int
    dropped_speed: int


def route_distance(
    fixes: Iterable[GpsFix],
    *,
    max_accuracy_m: float = 50.0,
    min_step_m: float = 10.0,
    max_speed_mps: float = 67.0,      # ~150 mph; faster means a GPS glitch
) -> RouteResult:
    """Distance along a GPS trace with basic noise filtering.

    * fixes worse than `max_accuracy_m` are ignored;
    * movements shorter than `min_step_m` from the last kept fix are not
      counted yet (stationary jitter would otherwise inflate the total — slow
      traffic still accumulates once the step threshold is crossed);
    * a jump implying more than `max_speed_mps` is treated as a glitch.
    """
    ordered = sorted(fixes, key=lambda f: f.at)
    total = 0.0
    last: GpsFix | None = None
    kept = bad_acc = bad_speed = 0
    for fix in ordered:
        if fix.accuracy_m is not None and fix.accuracy_m > max_accuracy_m:
            bad_acc += 1
            continue
        if last is None:
            last, kept = fix, 1
            continue
        step = haversine_m(last.point, fix.point)
        if step < min_step_m:
            continue
        seconds = (fix.at - last.at).total_seconds()
        if seconds <= 0 or step / seconds > max_speed_mps:
            bad_speed += 1
            continue
        total += step
        last = fix
        kept += 1
    return RouteResult(round(total), kept, bad_acc, bad_speed)


def geohash(point: GeoPoint, precision: int = 7) -> str:
    """Standard base-32 geohash (precision 7 ≈ 150 m cells)."""
    if not 1 <= precision <= 12:
        raise ValueError("precision must be 1..12")
    lat_rng, lng_rng = [-90.0, 90.0], [-180.0, 180.0]
    bit, ch, even = 0, 0, True
    out = []
    while len(out) < precision:
        rng, value = (lng_rng, point.lng) if even else (lat_rng, point.lat)
        mid = (rng[0] + rng[1]) / 2
        if value >= mid:
            ch = (ch << 1) | 1
            rng[0] = mid
        else:
            ch <<= 1
            rng[1] = mid
        even = not even
        bit += 1
        if bit == 5:
            out.append(_GEOHASH_ALPHABET[ch])
            bit, ch = 0, 0
    return "".join(out)


def geohash_bounds(cell: str) -> tuple[float, float, float, float]:
    """(lat_min, lat_max, lng_min, lng_max) of a geohash cell."""
    if not cell or len(cell) > 12:
        raise ValueError("Invalid geohash")
    lat_rng, lng_rng = [-90.0, 90.0], [-180.0, 180.0]
    even = True
    for ch in cell.lower():
        idx = _GEOHASH_ALPHABET.find(ch)
        if idx < 0:
            raise ValueError(f"Invalid geohash character {ch!r}")
        for shift in range(4, -1, -1):
            rng = lng_rng if even else lat_rng
            mid = (rng[0] + rng[1]) / 2
            if (idx >> shift) & 1:
                rng[0] = mid
            else:
                rng[1] = mid
            even = not even
    return lat_rng[0], lat_rng[1], lng_rng[0], lng_rng[1]


def geohash_center(cell: str) -> GeoPoint:
    lat0, lat1, lng0, lng1 = geohash_bounds(cell)
    return GeoPoint((lat0 + lat1) / 2, (lng0 + lng1) / 2)


def bearing_deg(a: GeoPoint, b: GeoPoint) -> float:
    """Initial great-circle bearing from a to b, 0–360° clockwise from north."""
    la1, la2 = math.radians(a.lat), math.radians(b.lat)
    dl = math.radians(b.lng - a.lng)
    y = math.sin(dl) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def compass(bearing: float) -> str:
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[int((bearing % 360) / 45 + 0.5) % 8]


def compass16(bearing: float) -> str:
    points = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
    return points[int((bearing % 360) / 22.5 + 0.5) % 16]


def nearest(origin: GeoPoint, candidates: Sequence[GeoPoint]) -> tuple[int, int]:
    """(index, distance_m) of the closest candidate."""
    if not candidates:
        raise ValueError("No candidates")
    dists = [haversine_m(origin, c) for c in candidates]
    i = min(range(len(dists)), key=dists.__getitem__)
    return i, round(dists[i])
