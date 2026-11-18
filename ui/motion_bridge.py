"""
Flet ↔ Motion Sentry bridge (mobile only).

* Consent first: the prominent disclosure (Google Play policy) is shown and
  accepted before any permission prompt; `core.motion.policy.next_step`
  decides every subsequent step.
* Foreground mode (default): location updates while the app is open.
* Background mode (opt-in): Android runs the geolocator as a location-type
  foreground service with a persistent notification; iOS uses background
  location updates with the system's blue indicator. Both are required by
  platform policy for continuous background location.
* Battery: the geolocator is re-configured from the sentry's sampling profile
  — sparse while idle, precise during a trip.
* A 30 s ticker lets the sentry close trips after parking, when the OS stops
  delivering fixes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timezone
from typing import Any, Callable

import flet as ft

from core.device_settings import DeviceSettings
from core.fleet.geo import GeoPoint
from core.motion import policy
from core.motion.classifier import ClassifierContext, WorkHours, Zone
from core.motion.sentry import MotionFix, MotionSentry, SamplingProfile, SentryConfig
from core.motion.service import AutoTripSaved, AutoTripStarted, MotionService, ServiceEvent

TICK_SECONDS = 30

try:  # optional dependency: only needed on phones
    import flet_geolocator as fg
except ImportError:  # pragma: no cover
    fg = None  # type: ignore[assignment]


def context_from_settings(s: DeviceSettings) -> ClassifierContext:
    def zone_of(d: dict[str, Any], name: str) -> Zone:
        return Zone(d.get("name", name), GeoPoint(float(d["lat"]), float(d["lng"])), int(d.get("radius_m", 300)))

    hours = []
    for h in s.work_hours:
        start = time.fromisoformat(h["start"])
        end = time.fromisoformat(h["end"])
        hours.append(WorkHours(frozenset(int(d) for d in h.get("weekdays", range(7))), start, end))
    return ClassifierContext(
        tz=s.timezone, shift_active=s.shift_active, shift_platform_id=s.shift_platform_id,
        home=zone_of(s.home, "Home") if s.home else None,
        work_zones=tuple(zone_of(z, f"Zone {i + 1}") for i, z in enumerate(s.work_zones)),
        work_hours=tuple(hours),
    )


def geolocator_config(platform: str, profile: SamplingProfile, *, background: bool) -> Any:
    """Platform-specific geolocator configuration for a sampling profile."""
    if fg is None:
        return None
    accuracy = fg.GeolocatorPositionAccuracy.HIGH if profile.accuracy == "high" else fg.GeolocatorPositionAccuracy.MEDIUM
    if platform == "android":
        notification = fg.ForegroundNotificationConfiguration(
            notification_title=policy.ANDROID_NOTIFICATION_TITLE,
            notification_text=policy.ANDROID_NOTIFICATION_TEXT,
            notification_channel_name="Trip detection",
            notification_set_ongoing=True,
        ) if background else None
        return fg.GeolocatorAndroidConfiguration(
            accuracy=accuracy, distance_filter=profile.distance_filter_m,
            interval_duration=ft.Duration(seconds=profile.interval_s),
            foreground_notification_config=notification,
        )
    if platform == "ios":
        return fg.GeolocatorIosConfiguration(
            accuracy=accuracy, distance_filter=profile.distance_filter_m,
            activity_type=fg.GeolocatorIosActivityType.AUTOMOTIVE_NAVIGATION,
            pause_location_updates_automatically=False,
            allow_background_location_updates=background,
            show_background_location_indicator=background,
        )
    return fg.GeolocatorConfiguration(accuracy=accuracy, distance_filter=profile.distance_filter_m)


def fix_from_position(pos: Any, *, now: datetime) -> MotionFix | None:
    if pos is None or pos.latitude is None or pos.longitude is None:
        return None
    at = pos.timestamp or now
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    speed = pos.speed if (pos.speed is not None and pos.speed >= 0) else None
    return MotionFix(at, float(pos.latitude), float(pos.longitude), speed, pos.accuracy)


class MotionBridge:
    def __init__(self, page: Any, state: Any, *, notify: Callable[[str], None],
                 on_trip: Callable[[ServiceEvent], None] | None = None,
                 show_dialog: Callable[[ft.DialogControl], None] | None = None,
                 close_dialog: Callable[[], None] | None = None) -> None:
        self.page = page
        self.state = state
        self.notify = notify
        self.on_trip = on_trip
        self.show_dialog = show_dialog
        self.close_dialog = close_dialog
        self.platform = str(getattr(page.platform, "value", page.platform) or "").lower()
        self.geo: Any = None
        self.service: MotionService | None = None
        self._ticker: asyncio.Task | None = None
        self._profile: SamplingProfile | None = None
        self.running = False
        self.background = False
        self.last_position: GeoPoint | None = None

    @property
    def available(self) -> bool:
        return policy.is_mobile(self.platform) and fg is not None

    @property
    def status_text(self) -> str:
        if not self.available:
            return "off (phone app only)"
        if not self.running:
            return "off"
        mode = "background" if self.background else "while open"
        state = self.service.sentry.state.value if self.service else "idle"
        return f"on ({mode}) · {state}"

    # -- enable / disable ----------------------------------------------------

    async def set_enabled(self, enabled: bool, *, background: bool) -> None:
        if not enabled:
            await self.stop()
            self.state.update_settings(sentry_enabled=False, sentry_background=False)
            return
        from core.licensing import Feature

        if not self.state.allows(Feature.MOTION_SENTRY):          # iFlow Pro
            self.state.update_settings(sentry_enabled=False, sentry_background=False)
            self.notify("Automatic trip detection is part of iFlow Pro. Manual trip logging stays free.")
            return
        step = await self._permission_step(background)
        if step.action == "show_disclosure":
            accepted = await self._ask_disclosure()
            if not accepted:
                self.state.update_settings(sentry_enabled=False)
                return
            self.state.update_settings(disclosure_accepted=True)
            step = await self._permission_step(background)
        if step.action == "request_permission":
            await self.geo_service().request_permission()
            step = await self._permission_step(background)
        if step.action == "open_app_settings":
            self.notify(step.message)
            await self.geo_service().open_app_settings()
            step = await self._permission_step(background)
        if step.action in ("run_foreground_only", "run_background"):
            await self.start(background=step.action == "run_background")
            self.state.update_settings(sentry_enabled=True, sentry_background=self.background)
            if background and not self.background:
                self.notify(step.message)
        else:
            self.notify(step.message)
            self.state.update_settings(sentry_enabled=False)

    async def _permission_step(self, background: bool) -> policy.PermissionStep:
        status = None
        if self.available and self.state.settings.disclosure_accepted:
            status = (await self.geo_service().get_permission_status()).value
        return policy.next_step(self.platform, status, want_background=background,
                                disclosure_accepted=self.state.settings.disclosure_accepted)

    async def _ask_disclosure(self) -> bool:
        if self.show_dialog is None:
            return False
        answer: asyncio.Future = asyncio.get_running_loop().create_future()

        def done(value: bool) -> Callable[[Any], None]:
            def handler(e: Any) -> None:
                if self.close_dialog:
                    self.close_dialog()
                if not answer.done():
                    answer.set_result(value)
            return handler

        self.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text(policy.DISCLOSURE_TITLE), content=ft.Text(policy.DISCLOSURE_TEXT),
            actions=[ft.TextButton("No thanks", on_click=done(False)), ft.FilledButton("Turn on", on_click=done(True))],
        ))
        return await answer

    def geo_service(self) -> Any:
        if self.geo is None:
            self.geo = fg.Geolocator(on_position_change=self._on_position,
                                     on_error=lambda e: self.notify(f"Location error: {e.data}"))
            self.page.services.append(self.geo)
        return self.geo

    async def start(self, *, background: bool) -> None:
        s: DeviceSettings = self.state.settings
        sentry = MotionSentry(SentryConfig(start_speed_mph=s.start_speed_mph))
        self.service = MotionService(self.state.db, context=lambda: context_from_settings(self.state.settings),
                                     sentry=sentry, listener=self._on_event, zone_recorder=self._record_zones)
        self.background = background
        self.running = True
        self._apply_profile(force=True)
        if self._ticker is None:
            self._ticker = asyncio.ensure_future(self._tick_loop())

    def _record_zones(self, trip_id: str, start: GeoPoint, end: GeoPoint) -> None:
        """Keep only coarse cells for Insights (home suppressed); the points themselves are dropped."""
        if not self.state.settings.insights_zones_enabled:
            return
        from core.intelligence.zones import private_zones_from_settings

        self.state.zone_store().record(trip_id, start, end, private=private_zones_from_settings(self.state.settings))

    async def stop(self) -> None:
        if self.service:
            self.service.stop_now()
        self.running = False
        if self._ticker:
            self._ticker.cancel()
            self._ticker = None
        if self.geo is not None:
            self.geo.on_position_change = None
            self.geo.update()

    # -- runtime --------------------------------------------------------------

    def _apply_profile(self, *, force: bool = False) -> None:
        if not self.service or (self.geo is None and not force):
            return
        profile = self.service.sentry.sampling_profile()
        if force or profile != self._profile:
            self._profile = profile
            geo = self.geo_service()
            geo.configuration = geolocator_config(self.platform, profile, background=self.background)
            geo.on_position_change = self._on_position
            geo.update()

    def _on_position(self, e: Any) -> None:
        fix = fix_from_position(e.position, now=self.state.now())
        if fix is None:
            return
        self.last_position = GeoPoint(fix.lat, fix.lng)
        if self.running and self.service:
            self.service.handle_fix(fix)
            self._apply_profile()

    async def _tick_loop(self) -> None:
        while self.running:
            await asyncio.sleep(TICK_SECONDS)
            if self.service:
                self.service.tick(self.state.now())
                self._apply_profile()

    def _on_event(self, event: ServiceEvent) -> None:
        if isinstance(event, AutoTripStarted):
            self.notify("Driving detected — logging this trip")
        elif isinstance(event, AutoTripSaved):
            self.notify(f"Trip saved ({event.classification.purpose}) — tap Fleet to confirm")
        if self.on_trip:
            self.on_trip(event)

    async def current_position(self) -> GeoPoint | None:
        if not self.available:
            return None
        try:
            pos = await self.geo_service().get_current_position()
        except Exception:
            return self.last_position
        fix = fix_from_position(pos, now=self.state.now())
        if fix:
            self.last_position = GeoPoint(fix.lat, fix.lng)
        return self.last_position
