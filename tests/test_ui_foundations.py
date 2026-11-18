"""Routing, responsive layout, display formatting, device settings, motion-bridge helpers."""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest

from core.config import UnitSystem
from core.device_settings import DeviceSettings, SettingsStore, guess_timezone
from core.motion.sentry import IDLE_PROFILE, TRIP_PROFILE
from ui import router
from ui.app import VIEW_BUILDERS
from ui.format import (
    MINUS,
    days_until,
    distance,
    duration,
    local_time,
    money,
    money_compact,
    parse_distance,
    percent_bp,
    relative_minutes,
)
from ui.layout import layout_for, size_class_for
from ui.motion_bridge import context_from_settings, fix_from_position, geolocator_config

# --- routing ------------------------------------------------------------------------------


def test_every_route_has_a_view():
    assert set(VIEW_BUILDERS) == {r.path for r in router.ROUTES}
    assert [r.path for r in router.NAV_ROUTES] == ["/", "/finances", "/fleet", "/radar", "/insights", "/health",
                                                   "/settings"]
    # phones: 5-item bottom bar; Settings lives in the app bar
    assert [r.path for r in router.nav_routes(compact=True)] == ["/", "/finances", "/fleet", "/radar", "/insights"]


@pytest.mark.parametrize("route,path,redirect,params", [
    ("/fleet", "/unlock", "/unlock", {"next": "/fleet"}),
    ("/", "/unlock", "/unlock", {"next": "/"}),
    ("/unlock", "/unlock", None, {}),
    ("/does-not-exist", "/unlock", "/unlock", {"next": "/does-not-exist"}),
])
def test_locked_routes_go_to_unlock(route, path, redirect, params):
    res = router.resolve(route, unlocked=False)
    assert (res.spec.path, res.redirect_to, res.params) == (path, redirect, params)


def test_unlocked_resolution():
    assert router.resolve("/radar?refresh=1", unlocked=True).params == {"refresh": "1"}
    assert router.resolve("/radar/", unlocked=True).spec.path == "/radar"
    assert router.resolve("", unlocked=True).spec.path == "/"
    unknown = router.resolve("/nope", unlocked=True)
    assert (unknown.spec.path, unknown.redirect_to) == ("/", "/")
    back = router.resolve("/unlock?next=/fleet", unlocked=True)
    assert (back.spec.path, back.redirect_to) == ("/fleet", "/fleet")
    assert router.resolve("/unlock?next=/unlock", unlocked=True).spec.path == "/"
    assert router.resolve("/unlock?next=https://evil", unlocked=True).spec.path == "/"


def test_nav_index_roundtrip():
    for compact in (False, True):
        for i, spec in enumerate(router.nav_routes(compact)):
            assert router.nav_index(spec.path, compact=compact) == i
            assert router.path_for_index(i, compact=compact) == spec.path
    assert router.nav_index("/unlock") is None
    assert router.nav_index("/settings", compact=True) is None


# --- layout -------------------------------------------------------------------------------


@pytest.mark.parametrize("width,cls,nav,cols", [
    (360, "compact", "bar", 1), (599, "compact", "bar", 1), (600, "medium", "rail", 2),
    (839, "medium", "rail", 2), (840, "expanded", "rail_extended", 3), (1920, "expanded", "rail_extended", 4),
])
def test_breakpoints(width, cls, nav, cols):
    spec = layout_for(width, "linux")
    assert (spec.size_class, spec.nav, spec.columns) == (cls, nav, cols)


def test_touch_targets_and_defaults():
    assert layout_for(390, "android").min_target == 48
    assert layout_for(390, "ios").touch
    assert layout_for(1280, "windows").min_target == 36
    assert layout_for(None, "android").size_class == "compact"     # width unknown at startup
    assert layout_for(None, "macos").size_class == "expanded"
    assert size_class_for(0) == "compact"
    assert layout_for(390, "ios").kpi_col["xs"] == 6                # 2×2 KPI tiles on phones


# --- formatting ---------------------------------------------------------------------------


def test_money_formats():
    assert money(123456, "USD") == "$1,234.56"
    assert money(-500, "USD") == f"{MINUS}$5.00"
    assert money(123456, "USD", cents=False) == "$1,235"
    assert money(123449, "USD", cents=False) == "$1,234"
    assert money(1200, "JPY") == "¥1,200"
    assert money(1234, "KWD") == "KWD 1.234"


@pytest.mark.parametrize("minor,expected", [
    (0, "$0"), (95_000, "$950"), (99_999, "$1000"), (100_000, "$1k"), (1_250_000, "$12.5k"),
    (123_456_789, "$1.2M"), (-250_000, f"{MINUS}$2.5k"),
])
def test_money_compact(minor, expected):
    assert money_compact(minor, "USD") == expected


def test_percent_distance_and_times():
    assert percent_bp(2250) == "22.5%"
    assert percent_bp(7901) == "79%"
    assert percent_bp(10_000, decimals=0) == "100%"
    assert percent_bp(-150) == f"{MINUS}1.5%"
    assert distance(160_934, UnitSystem.IMPERIAL) == "100.0 mi"
    assert distance(1_234_567, UnitSystem.METRIC, decimals=0) == "1,235 km"
    assert relative_minutes(0) == "now" and relative_minutes(-5) == "now"
    assert relative_minutes(45) == "in 45 min" and relative_minutes(120) == "in 2 h"
    assert relative_minutes(135) == "in 2 h 15 min"
    assert days_until(date(2026, 9, 27), date(2026, 9, 26)) == "tomorrow"
    assert days_until(date(2026, 9, 20), date(2026, 9, 26)) == "6 days ago"
    assert duration(3_900) == "1 h 5 min" and duration(-5) == "0 min"
    # 21:05 UTC = 5:05 PM EDT; formatted without platform-specific strftime flags.
    assert local_time(datetime(2026, 9, 26, 21, 5, tzinfo=timezone.utc), "America/New_York") == "Sat Sep 26, 5:05 PM"
    assert local_time(datetime(2026, 9, 26, 4, 0, tzinfo=timezone.utc), "UTC") == "Sat Sep 26, 4:00 AM"


def test_parse_distance_uses_exact_decimal():
    assert parse_distance("100", UnitSystem.IMPERIAL) == 160_934
    assert parse_distance("12,345.6", UnitSystem.IMPERIAL) == 19_868_317      # 12,345.6 × 1609.344
    assert parse_distance("2.5", UnitSystem.METRIC) == 2_500
    assert parse_distance("  ", UnitSystem.METRIC) is None
    for bad in ("abc", "-5", "NaN", "inf"):
        with pytest.raises(ValueError):
            parse_distance(bad, UnitSystem.IMPERIAL)


# --- device settings -------------------------------------------------------------------------


def test_settings_store_roundtrip(db):
    store = SettingsStore(db)
    s = store.update(timezone="America/New_York", radar_airport_icao="KTPA",
                     home={"lat": 27.95, "lng": -82.46, "radius_m": 200},
                     ics_feeds=[{"name": "Bucs", "url": "https://example.com/bucs.ics"}])
    assert store.load() == s and store.load().home["radius_m"] == 200
    with pytest.raises(KeyError):
        store.update(nonsense=True)


def test_secrets_prefer_environment(db, monkeypatch):
    store = SettingsStore(db)
    store.update(ticketmaster_api_key="stored-key")
    assert store.secret("ticketmaster_api_key", "IFLOW_TICKETMASTER_KEY") == "stored-key"
    monkeypatch.setenv("IFLOW_TICKETMASTER_KEY", "env-key")
    assert store.secret("ticketmaster_api_key", "IFLOW_TICKETMASTER_KEY") == "env-key"


def test_guess_timezone(monkeypatch):
    monkeypatch.setenv("TZ", "America/Chicago")
    assert guess_timezone() == "America/Chicago"
    monkeypatch.setenv("TZ", "")
    assert "/" in guess_timezone() or guess_timezone() == "UTC"


# --- motion bridge helpers ------------------------------------------------------------------------


def test_context_from_settings():
    s = DeviceSettings(timezone="America/New_York", shift_active=True, shift_platform_id="p1",
                       home={"lat": 27.95, "lng": -82.46},
                       work_zones=[{"name": "TPA", "lat": 27.975, "lng": -82.533, "radius_m": 1500}],
                       work_hours=[{"weekdays": [4, 5], "start": "16:00", "end": "02:00"}])
    ctx = context_from_settings(s)
    assert ctx.shift_active and ctx.shift_platform_id == "p1" and ctx.home.radius_m == 300
    assert ctx.work_zones[0].name == "TPA" and ctx.work_hours[0].end == time(2, 0)


def test_geolocator_config_per_platform():
    import flet_geolocator as fg

    android_bg = geolocator_config("android", TRIP_PROFILE, background=True)
    assert isinstance(android_bg, fg.GeolocatorAndroidConfiguration)
    assert android_bg.foreground_notification_config.notification_set_ongoing is True
    assert android_bg.distance_filter == 10
    assert geolocator_config("android", IDLE_PROFILE, background=False).foreground_notification_config is None
    ios = geolocator_config("ios", IDLE_PROFILE, background=True)
    assert ios.allow_background_location_updates and ios.show_background_location_indicator
    assert ios.activity_type == fg.GeolocatorIosActivityType.AUTOMOTIVE_NAVIGATION
    assert not geolocator_config("ios", IDLE_PROFILE, background=False).allow_background_location_updates


def test_fix_from_position():
    import flet_geolocator as fg

    now = datetime(2026, 9, 26, tzinfo=timezone.utc)
    fx = fix_from_position(fg.GeolocatorPosition(latitude=27.9, longitude=-82.5, speed=-1.0, accuracy=8.0,
                                                 timestamp=datetime(2026, 9, 26, 1, 0)), now=now)
    assert fx.speed_mps is None and fx.at.tzinfo is not None and fx.accuracy_m == 8.0
    assert fix_from_position(fg.GeolocatorPosition(latitude=None, longitude=None), now=now) is None
