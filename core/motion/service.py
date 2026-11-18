"""
MotionService — connects the sentry to the mileage log.

    fix ─► MotionSentry ─► TripStarted  ─► mileage.start_trip (persisted at once,
                                            so a killed app keeps the trip)
                        ─► TripEnded    ─► mileage.stop_trip(gps=...) + classify
                                            + optional coarse zone record (Insights)
                        ─► TripDiscarded ─► mileage.cancel_trip

Auto trips are flagged `detection='auto'` and `review_status='needs_review'`
until the user confirms them. If the user already has a manual trip running,
the sentry stays out of the way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Union

from core.fleet.geo import GeoPoint
from core.fleet.mileage import MileageError, cancel_trip, start_trip, stop_trip
from core.motion.classifier import Classification, ClassifierContext, classify
from core.motion.sentry import MotionFix, MotionSentry, TripDiscarded, TripEnded, TripStarted

log = logging.getLogger(__name__)

AUTO_DESCRIPTION = "Auto-detected trip"


@dataclass(frozen=True)
class AutoTripStarted:
    trip_id: str
    started_at: datetime


@dataclass(frozen=True)
class AutoTripSaved:
    trip_id: str
    distance_m: int
    classification: Classification
    maintenance_alerts: tuple[Any, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class AutoTripDiscarded:
    reason: str


@dataclass(frozen=True)
class SentrySkipped:
    reason: str


ServiceEvent = Union[AutoTripStarted, AutoTripSaved, AutoTripDiscarded, SentrySkipped]


class MotionService:
    def __init__(
        self,
        db: Any,
        *,
        context: Callable[[], ClassifierContext],
        sentry: MotionSentry | None = None,
        listener: Callable[[ServiceEvent], None] | None = None,
        device_id: str | None = None,
        zone_recorder: Callable[[str, GeoPoint, GeoPoint], Any] | None = None,
    ) -> None:
        self.db = db
        self._zone_recorder = zone_recorder
        self.sentry = sentry or MotionSentry()
        self._context = context
        self._listener = listener
        self._device_id = device_id
        self.trip_id: str | None = None

    def handle_fix(self, fix: MotionFix) -> list[ServiceEvent]:
        return self._dispatch(self.sentry.feed(fix))

    def tick(self, now: datetime) -> list[ServiceEvent]:
        return self._dispatch(self.sentry.tick(now))

    def stop_now(self) -> list[ServiceEvent]:
        return self._dispatch(self.sentry.force_end())

    def _dispatch(self, events: list[Any]) -> list[ServiceEvent]:
        out: list[ServiceEvent] = []
        for event in events:
            if isinstance(event, TripStarted):
                out.append(self._on_start(event))
            elif isinstance(event, TripEnded):
                result = self._on_end(event)
                if result:
                    out.append(result)
            elif isinstance(event, TripDiscarded):
                out.append(self._on_discard(event))
        if self._listener:
            for e in out:
                self._listener(e)
        return out

    def _on_start(self, event: TripStarted) -> ServiceEvent:
        ctx = self._context()
        try:
            trip = start_trip(
                self.db, platform_id=ctx.shift_platform_id, started_at=event.started_at,
                timezone=ctx.tz, description=AUTO_DESCRIPTION, device_id=self._device_id,
            )
        except MileageError as exc:
            self.trip_id = None
            return SentrySkipped(str(exc))
        with self.db.transaction() as conn:
            conn.execute("UPDATE mileage_logs SET detection = 'auto', review_status = 'needs_review' WHERE id = ?",
                         (trip.id,))
        self.trip_id = trip.id
        return AutoTripStarted(trip.id, event.started_at)

    def _on_end(self, event: TripEnded) -> ServiceEvent | None:
        if self.trip_id is None:
            return None
        trip_id, self.trip_id = self.trip_id, None
        classification = classify(event.trip, self._context())
        result = stop_trip(self.db, trip_id, ended_at=event.trip.ended_at, gps=event.trip.fixes)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE mileage_logs SET purpose = ?, platform_id = coalesce(?, platform_id), "
                "review_status = 'needs_review' WHERE id = ?",
                (classification.purpose, classification.platform_id, trip_id),
            )
        if self._zone_recorder is not None:
            try:                            # coarse zones for Insights; must never break trip saving
                self._zone_recorder(trip_id, event.trip.start, event.trip.end)
            except Exception:
                log.exception("Recording trip zones failed")
        return AutoTripSaved(trip_id, result.trip.distance_m, classification,
                             result.maintenance_alerts, result.warnings)

    def _on_discard(self, event: TripDiscarded) -> ServiceEvent:
        if self.trip_id is not None:
            cancel_trip(self.db, self.trip_id)
            self.trip_id = None
        return AutoTripDiscarded(event.reason)


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------


def trips_needing_review(db: Any) -> list[dict[str, Any]]:
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT id, started_at, ended_at, distance_m, purpose, platform_id, vehicle_id FROM mileage_logs "
            "WHERE review_status = 'needs_review' AND trip_state = 'completed' AND deleted_at IS NULL "
            "ORDER BY started_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def confirm_trip(db: Any, trip_id: str, *, purpose: str, platform_id: str | None = None,
                 description: str | None = None) -> None:
    """User confirmation (IRS: the taxpayer attests to purpose)."""
    if purpose not in ("business", "commute", "personal"):
        raise MileageError(f"Invalid purpose '{purpose}'")
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE mileage_logs SET purpose = ?, platform_id = coalesce(?, platform_id), "
            "description = coalesce(?, description), review_status = 'confirmed' "
            "WHERE id = ? AND deleted_at IS NULL AND trip_state = 'completed'",
            (purpose, platform_id, description, trip_id),
        )
        if cur.rowcount != 1:
            raise MileageError(f"No completed trip {trip_id}")


def dismiss_trip(db: Any, trip_id: str) -> None:
    """Delete a false detection (soft delete, so it syncs)."""
    with db.transaction() as conn:
        conn.execute("UPDATE mileage_logs SET deleted_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                     "WHERE id = ? AND detection = 'auto'", (trip_id,))
