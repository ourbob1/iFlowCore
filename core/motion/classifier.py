"""
Auto-classification of detected trips (business / commute / personal).

Every rule is explainable — the result carries human-readable reasons — and
every auto-detected trip is still saved as `needs_review`: the IRS expects a
contemporaneous log the taxpayer stands behind, so the app suggests and the
user confirms. Confidence (basis points) only decides what the prompt
pre-selects.

Rules, first match wins:
  1. You were on shift (toggled "working" in the app)    → business, 95%
  2. Work hours + trip starts/ends in a work zone         → business, 80%
  3. Work hours                                           → business, 60%
  4. Home → work zone outside work hours                  → commute, 55%
  5. Touches a work zone outside work hours               → business, 50%
  6. Otherwise                                            → personal, 70%
     (50% when no work hours are configured — nothing to judge by)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Literal

from core.fleet.geo import GeoPoint, haversine_m
from core.motion.sentry import DetectedTrip
from core.timeutil import zone

Purpose = Literal["business", "commute", "personal"]


@dataclass(frozen=True)
class Zone:
    name: str
    center: GeoPoint
    radius_m: int = 300

    def contains(self, point: GeoPoint) -> bool:
        return haversine_m(self.center, point) <= self.radius_m


@dataclass(frozen=True)
class WorkHours:
    weekdays: frozenset[int]          # 0 = Monday
    start: time
    end: time                         # may be earlier than start (overnight shift)

    def covers(self, local: datetime) -> bool:
        t = local.time()
        if self.start <= self.end:
            return local.weekday() in self.weekdays and self.start <= t < self.end
        # Overnight: e.g. Fri 20:00 → Sat 02:00 belongs to Friday's shift.
        if t >= self.start:
            return local.weekday() in self.weekdays
        return t < self.end and (local.weekday() - 1) % 7 in self.weekdays


@dataclass(frozen=True)
class ClassifierContext:
    tz: str = "UTC"
    shift_active: bool = False
    shift_platform_id: str | None = None
    home: Zone | None = None
    work_zones: tuple[Zone, ...] = ()
    work_hours: tuple[WorkHours, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Classification:
    purpose: Purpose
    confidence_bp: int
    reasons: tuple[str, ...]
    platform_id: str | None = None


def classify(trip: DetectedTrip, ctx: ClassifierContext) -> Classification:
    if ctx.shift_active:
        return Classification("business", 9_500, ("You were on shift",), ctx.shift_platform_id)

    local_start = trip.started_at.astimezone(zone(ctx.tz))
    in_hours = any(h.covers(local_start) for h in ctx.work_hours)
    zone_hit = next((z for z in ctx.work_zones if z.contains(trip.start) or z.contains(trip.end)), None)
    from_home = ctx.home is not None and ctx.home.contains(trip.start)

    if in_hours and zone_hit:
        return Classification("business", 8_000, ("During work hours", f"Touches {zone_hit.name}"))
    if in_hours:
        return Classification("business", 6_000, ("During work hours",))
    if from_home and zone_hit and zone_hit.contains(trip.end):
        return Classification("commute", 5_500, ("Home to a work zone outside work hours",))
    if zone_hit:
        return Classification("business", 5_000, (f"Touches {zone_hit.name}",))
    if not ctx.work_hours:
        return Classification("personal", 5_000, ("No shift or work hours set",))
    return Classification("personal", 7_000, ("Outside work hours",))
