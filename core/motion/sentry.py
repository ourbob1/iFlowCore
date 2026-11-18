"""
Motion Sentry — automatic trip detection from location fixes.

Pure logic, no platform code: feed it `MotionFix`es from any source (the Flet
geolocator bridge on phones, a GPX replay on desktop, a test list) and it
returns events. That keeps the rules testable and identical on every
platform.

State machine
-------------

    IDLE ──(fix faster than start threshold)──► ARMING
    ARMING ──(sustained: ≥ confirm_fixes fixes over threshold across
              ≥ confirm_seconds)──► MOVING   → emits TripStarted
    ARMING ──(drops below threshold)──► IDLE  (a burst, e.g. running for a bus)
    MOVING ──(speed ≤ stop threshold)──► STOPPING
    STOPPING ──(moving again)──► MOVING      (red light, drive-thru)
    STOPPING ──(still for stop_dwell_seconds)──► IDLE → emits TripEnded
                                                   or TripDiscarded (too short)
    MOVING/STOPPING ──(no fix for gap_timeout_seconds)──► IDLE → TripEnded
                                                   at the last fix

Thresholds
----------
* Start: speed strictly greater than 10 mph (4.4704 m/s) — walking and
  cycling at a stroll never arm the sentry.
* Stop: at or below 3 mph for 3 minutes.
* Fixes with poor horizontal accuracy are ignored; a fix implying more than
  ~150 mph from the previous one is treated as a GPS glitch.
* Speed comes from the OS when it reports one (≥ 0; iOS/Android report -1
  or None when unknown), otherwise from distance/time between fixes.

Battery policy
--------------
`sampling_profile()` tells the platform bridge how often to sample: coarse
and sparse while idle, precise while a trip is in progress.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal, Union

from core.fleet.geo import GeoPoint, GpsFix, haversine_m, route_distance

MPS_PER_MPH = Decimal("0.44704")     # exact by definition (1 mi = 1609.344 m)


def mph_to_mps(mph: int | Decimal | str) -> Decimal:
    if isinstance(mph, float):
        raise TypeError("Pass mph as int/str/Decimal")
    return Decimal(mph) * MPS_PER_MPH


class SentryState(str, Enum):
    IDLE = "idle"
    ARMING = "arming"
    MOVING = "moving"
    STOPPING = "stopping"


@dataclass(frozen=True)
class MotionFix:
    at: datetime                         # timezone-aware
    lat: float
    lng: float
    speed_mps: float | None = None       # OS-reported ground speed; None/negative = unknown
    accuracy_m: float | None = None

    @property
    def point(self) -> GeoPoint:
        return GeoPoint(self.lat, self.lng)


@dataclass(frozen=True)
class SentryConfig:
    start_speed_mph: int = 10            # trigger when speed is strictly above this
    stop_speed_mph: int = 3
    confirm_fixes: int = 3
    confirm_seconds: int = 30
    stop_dwell_seconds: int = 180
    gap_timeout_seconds: int = 600
    min_trip_meters: int = 400
    max_accuracy_m: float = 65.0
    max_plausible_mps: float = 67.0

    def __post_init__(self) -> None:
        if self.stop_speed_mph >= self.start_speed_mph:
            raise ValueError("stop_speed_mph must be below start_speed_mph")
        if self.confirm_fixes < 1 or self.confirm_seconds < 0 or self.stop_dwell_seconds <= 0:
            raise ValueError("Invalid sentry timing configuration")

    @property
    def start_mps(self) -> float:
        return float(mph_to_mps(self.start_speed_mph))

    @property
    def stop_mps(self) -> float:
        return float(mph_to_mps(self.stop_speed_mph))


@dataclass(frozen=True)
class DetectedTrip:
    started_at: datetime
    ended_at: datetime
    start: GeoPoint
    end: GeoPoint
    distance_m: int
    max_speed_mps: float
    fixes: tuple[GpsFix, ...]

    @property
    def duration_s(self) -> int:
        return int((self.ended_at - self.started_at).total_seconds())


@dataclass(frozen=True)
class TripStarted:
    started_at: datetime
    start: GeoPoint
    speed_mps: float


@dataclass(frozen=True)
class TripEnded:
    trip: DetectedTrip
    reason: Literal["stopped", "signal_lost", "forced"]


@dataclass(frozen=True)
class TripDiscarded:
    started_at: datetime
    distance_m: int
    reason: str


SentryEvent = Union[TripStarted, TripEnded, TripDiscarded]


@dataclass(frozen=True)
class SamplingProfile:
    accuracy: Literal["medium", "high"]
    distance_filter_m: int
    interval_s: int


IDLE_PROFILE = SamplingProfile("medium", 50, 30)
TRIP_PROFILE = SamplingProfile("high", 10, 5)


@dataclass
class _Track:
    fixes: list[MotionFix] = field(default_factory=list)
    max_speed: float = 0.0


class MotionSentry:
    def __init__(self, config: SentryConfig | None = None) -> None:
        self.config = config or SentryConfig()
        self.state = SentryState.IDLE
        self._last: MotionFix | None = None       # last accepted fix
        self._arming: list[MotionFix] = []
        self._track = _Track()
        self._slow_since: datetime | None = None
        self.rejected = 0

    # -- public ------------------------------------------------------------

    def sampling_profile(self) -> SamplingProfile:
        return IDLE_PROFILE if self.state is SentryState.IDLE else TRIP_PROFILE

    def speed_of(self, fix: MotionFix) -> float | None:
        """Best-known speed for a fix in m/s (OS value, else derived)."""
        if fix.speed_mps is not None and fix.speed_mps >= 0:
            return float(fix.speed_mps)
        if self._last is None:
            return None
        seconds = (fix.at - self._last.at).total_seconds()
        if seconds <= 0:
            return None
        return haversine_m(self._last.point, fix.point) / seconds

    def is_over_start_threshold(self, speed_mps: float | None) -> bool:
        return speed_mps is not None and speed_mps > self.config.start_mps

    def feed(self, fix: MotionFix) -> list[SentryEvent]:
        """Process one fix; returns any events it caused."""
        if fix.at.tzinfo is None:
            raise ValueError("MotionFix.at must be timezone-aware")
        events: list[SentryEvent] = []
        cfg = self.config

        if fix.accuracy_m is not None and fix.accuracy_m > cfg.max_accuracy_m:
            self.rejected += 1
            return events
        if self._last is not None:
            if fix.at <= self._last.at:
                self.rejected += 1                      # duplicate / out of order
                return events
            gap = (fix.at - self._last.at).total_seconds()
            if gap > cfg.gap_timeout_seconds and self.state in (SentryState.MOVING, SentryState.STOPPING):
                events.extend(self._finish(self._last.at, "signal_lost"))
            elif gap > 0 and haversine_m(self._last.point, fix.point) / gap > cfg.max_plausible_mps:
                self.rejected += 1
                return events

        speed = self.speed_of(fix)
        self._last = fix

        if self.state is SentryState.IDLE:
            if self.is_over_start_threshold(speed):
                self.state = SentryState.ARMING
                self._arming = [fix]
                events.extend(self._maybe_confirm(fix, speed))
        elif self.state is SentryState.ARMING:
            if self.is_over_start_threshold(speed):
                self._arming.append(fix)
                events.extend(self._maybe_confirm(fix, speed))
            else:
                self._reset()
        elif self.state is SentryState.MOVING:
            self._record(fix, speed)
            if speed is not None and speed <= cfg.stop_mps:
                self.state = SentryState.STOPPING
                self._slow_since = fix.at
        elif self.state is SentryState.STOPPING:
            self._record(fix, speed)
            if speed is not None and speed > cfg.stop_mps:
                self.state = SentryState.MOVING
                self._slow_since = None
            elif self._slow_since and (fix.at - self._slow_since).total_seconds() >= cfg.stop_dwell_seconds:
                events.extend(self._finish(self._slow_since, "stopped"))
        return events

    def tick(self, now: datetime) -> list[SentryEvent]:
        """Advance time without a fix.

        Phones stop delivering fixes once the car is parked (the OS distance
        filter suppresses them), so the platform bridge calls this on a timer
        to let the stop dwell and signal-loss rules complete.
        """
        cfg = self.config
        if self._last is None or self.state in (SentryState.IDLE, SentryState.ARMING):
            if self.state is SentryState.ARMING and self._last is not None and \
                    (now - self._last.at).total_seconds() >= cfg.confirm_seconds * 2:
                self._reset()
            return []
        silent = (now - self._last.at).total_seconds()
        if self.state is SentryState.STOPPING and self._slow_since and \
                (now - self._slow_since).total_seconds() >= cfg.stop_dwell_seconds:
            return self._finish(self._slow_since, "stopped")
        if silent >= cfg.gap_timeout_seconds:
            return self._finish(self._last.at, "signal_lost")
        if self.state is SentryState.MOVING and silent >= cfg.stop_dwell_seconds:
            # No updates for a full dwell period while "moving": the distance
            # filter went quiet because the vehicle stopped.
            return self._finish(self._last.at, "stopped")
        return []

    def force_end(self) -> list[SentryEvent]:
        """End an in-progress trip now (user tapped Stop, or the app is closing)."""
        if self.state in (SentryState.MOVING, SentryState.STOPPING) and self._last is not None:
            return self._finish(self._last.at, "forced")
        self._reset()
        return []

    # -- internals ---------------------------------------------------------

    def _maybe_confirm(self, fix: MotionFix, speed: float | None) -> list[SentryEvent]:
        cfg = self.config
        span = (fix.at - self._arming[0].at).total_seconds()
        if len(self._arming) >= cfg.confirm_fixes and span >= cfg.confirm_seconds:
            self.state = SentryState.MOVING
            self._track = _Track()
            for f in self._arming:
                self._record(f, self.speed_of(f) if f is not fix else speed)
            first = self._arming[0]
            self._arming = []
            return [TripStarted(first.at, first.point, speed or 0.0)]
        return []

    def _record(self, fix: MotionFix, speed: float | None) -> None:
        self._track.fixes.append(fix)
        if speed is not None:
            self._track.max_speed = max(self._track.max_speed, speed)

    def _finish(self, ended_at: datetime, reason: Literal["stopped", "signal_lost", "forced"]) -> list[SentryEvent]:
        fixes = [f for f in self._track.fixes if f.at <= ended_at]
        max_speed = self._track.max_speed
        self._reset()
        if len(fixes) < 2:
            return [TripDiscarded(fixes[0].at if fixes else ended_at, 0, "not enough fixes")]
        gps = tuple(GpsFix(f.point, f.at, f.accuracy_m) for f in fixes)
        distance = route_distance(gps, max_accuracy_m=self.config.max_accuracy_m).distance_m
        if distance < self.config.min_trip_meters:
            return [TripDiscarded(fixes[0].at, distance, f"shorter than {self.config.min_trip_meters} m")]
        trip = DetectedTrip(fixes[0].at, ended_at, fixes[0].point, fixes[-1].point, distance, max_speed, gps)
        return [TripEnded(trip, reason)]

    def _reset(self) -> None:
        self.state = SentryState.IDLE
        self._arming = []
        self._slow_since = None
        self._track = _Track()
