"""App shell: secure startup, routing integrity, adaptive navigation, error boundary; radar feeds offline-first."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.fleet.demand import FlightArrival, VenueEvent
from core.fleet.geo import GeoPoint
from core.fleet.http import HttpResponse, ProviderError
from ui import app as app_module
from ui.app import IFlowCoreApp
from ui.state import LOCKOUT_AFTER, UnlockError, Unlocker
from ui.viewmodels.radar import (
    FeedCache,

    RadarService,
    build_radar,
    event_from_json,
    event_to_json,
    normalize_feed_url,
    size_bucket,
    urgency_for,
)
from core.config import UnitSystem, load_config
from core.device_settings import DeviceSettings

from .conftest import requires_sqlcipher
from .ui_helpers import FakePage, find_key, texts, walk

NOW = datetime(2026, 9, 26, 21, 30, tzinfo=timezone.utc)
PASS = "correct horse battery staple"


def make_app(tmp_path, *, width=1280, platform="linux", route="/", extra_env=None, services=None):
    env = {"IFLOW_DATA_DIR": str(tmp_path), **(extra_env or {})}
    page = FakePage(width, platform, route)
    app = IFlowCoreApp(page, env=env, clock=lambda: NOW, services={"motion": None, **(services or {})})
    app.services.pop("motion")
    return app, page


def view(page):
    return page.views[-1]


# --- routing integrity & adaptive shell ------------------------------------------------------


@pytest.mark.parametrize("width,platform,nav_key", [(390, "android", "nav-bar"), (700, "ios", "nav-rail"),
                                                    (1440, "macos", "nav-rail")])
def test_every_route_renders_without_errors(tmp_path, width, platform, nav_key):
    app, page = make_app(tmp_path, width=width, platform=platform)
    app.start()                                                   # dev mode: opens unencrypted
    for route, view_key in (("/", "view-dashboard"), ("/finances", "view-finances"), ("/fleet", "view-fleet"),
                            ("/radar", "view-radar"), ("/insights", "view-insights"), ("/settings", "view-settings")):
        app.navigate(route)
        assert app.last_error is None, (route, app.last_error)
        assert app.current_path == route and page.route == route
        assert find_key(view(page), view_key) is not None
        if nav_key == "nav-bar" and route == "/settings":      # phones: Settings is a full page with a back arrow
            assert find_key(view(page), "nav-bar") is None and find_key(view(page), "appbar-back") is not None
        else:
            assert find_key(view(page), nav_key) is not None
    rail = find_key(view(page), "nav-rail")
    if rail is not None:
        assert rail.extended == (width >= 840)


def test_nav_selection_follows_route(tmp_path):
    app, page = make_app(tmp_path, width=390, platform="android")
    app.start()
    app.navigate("/radar")
    assert find_key(view(page), "nav-bar").selected_index == 3


def test_unknown_route_redirects_home(tmp_path):
    app, page = make_app(tmp_path)
    app.start()
    app.navigate("/wat")
    assert page.route == "/" and app.current_path == "/"


def test_resize_switches_navigation(tmp_path):
    app, page = make_app(tmp_path, width=1400)
    app.start()
    assert find_key(view(page), "nav-rail") is not None
    page.width = 400
    page.on_resize(None)
    assert find_key(view(page), "nav-bar") is not None and find_key(view(page), "nav-rail") is None


def test_dev_mode_banner_and_sync_chip(tmp_path):
    app, page = make_app(tmp_path)
    app.start()
    assert "Development database is not encrypted" in texts(view(page))
    assert "Sync off" in texts(find_key(view(page), "sync-chip"))           # no iFlow account on this device


def test_error_boundary(tmp_path, monkeypatch):
    app, page = make_app(tmp_path)
    app.start()

    def boom(ctx):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(app_module.VIEW_BUILDERS, "/radar", boom)
    app.navigate("/radar")
    assert find_key(view(page), "view-error") is not None
    assert "kaboom" in app.last_error
    app.navigate("/")                                             # the rest of the app still works
    assert find_key(view(page), "view-dashboard") is not None


# --- secure startup ------------------------------------------------------------------------------


@requires_sqlcipher
def test_encrypted_first_run_create_then_unlock(tmp_path):
    env = {"IFLOW_REQUIRE_ENCRYPTION": "true"}
    app, page = make_app(tmp_path, route="/fleet", extra_env=env)
    app.start()
    assert page.route == "/unlock" and find_key(view(page), "view-unlock") is not None
    assert find_key(view(page), "confirm").visible                  # first run: confirm field shown
    assert find_key(view(page), "nav-bar") is None and find_key(view(page), "nav-rail") is None

    with pytest.raises(UnlockError, match="match"):
        app.unlocker.open(PASS, confirm="different passphrase")
    with pytest.raises(UnlockError, match="at least"):
        app.unlocker.open("short", confirm="short")
    db = app.unlocker.open(PASS, confirm=PASS)
    app.services["on_unlocked"](db)
    assert page.route == "/fleet"                                   # lands where the user was headed
    assert app.state.db.encrypted and app.state.banner is None

    app.lock()
    assert page.route == "/unlock" and app.state.db is None
    assert app.unlocker.plan().mode == "prompt"                     # existing DB: passphrase only
    with pytest.raises(UnlockError, match="didn't unlock"):
        app.unlocker.open("wrong passphrase value")
    app.services["on_unlocked"](app.unlocker.open(PASS))
    assert app.state.unlocked


@requires_sqlcipher
def test_env_key_opens_silently(tmp_path):
    env = {"IFLOW_REQUIRE_ENCRYPTION": "true", "IFLOW_DB_KEY": PASS}
    app, page = make_app(tmp_path, extra_env=env)
    app.start()
    assert app.state.unlocked and app.state.db.encrypted and page.route == "/"


@requires_sqlcipher
def test_lockout_after_repeated_failures(tmp_path):
    cfg = load_config({"IFLOW_DATA_DIR": str(tmp_path), "IFLOW_REQUIRE_ENCRYPTION": "true"})
    t = [1000.0]
    unlocker = Unlocker(cfg, env={}, clock=lambda: t[0])
    unlocker.open(PASS, confirm=PASS).close()
    for _ in range(LOCKOUT_AFTER):
        with pytest.raises(UnlockError, match="didn't unlock"):
            unlocker.open("nope nope nope nope")
    assert unlocker.lockout_remaining() == 30
    with pytest.raises(UnlockError, match="Too many attempts"):
        unlocker.open(PASS)                                         # even the right one waits
    t[0] += 31
    unlocker.open(PASS).close()
    assert unlocker.failures == 0


@requires_sqlcipher
def test_plaintext_database_is_encrypted_on_first_production_unlock(tmp_path):
    """A plaintext DB (e.g. from a development run) can't be opened by any passphrase; the desktop
    unlock offers to encrypt it in place instead, keeping the data."""
    from database.connection import Database
    from database.encryption import is_plaintext_sqlite

    cfg = load_config({"IFLOW_DATA_DIR": str(tmp_path), "IFLOW_REQUIRE_ENCRYPTION": "true"})
    dev = Database(cfg.db_path, driver="sqlite")
    dev.initialize()
    with dev.transaction() as conn:
        conn.execute("INSERT INTO platforms (id, name, kind) VALUES "
                     "('33333333-3333-4333-8333-333333333333', 'Dev platform', 'other')")
    dev.close()
    assert is_plaintext_sqlite(cfg.db_path)

    unlocker = Unlocker(cfg, env={})
    assert unlocker.plan().mode == "encrypt_existing"
    with pytest.raises(UnlockError, match="don't match"):
        unlocker.open(PASS, confirm=PASS + "x")
    assert is_plaintext_sqlite(cfg.db_path)                            # nothing touched on a typo
    db = unlocker.open(PASS, confirm=PASS)
    try:
        assert db.encrypted
        with db.connection() as conn:
            names = [r[0] for r in conn.execute("SELECT name FROM platforms")]
        assert "Dev platform" in names
    finally:
        db.close()
    assert not is_plaintext_sqlite(cfg.db_path)
    assert not list(tmp_path.glob("*.encrypting"))

    again = Unlocker(cfg, env={})
    assert again.plan().mode == "prompt"
    with pytest.raises(UnlockError, match="didn't unlock"):
        again.open("some other passphrase")
    again.open(PASS).close()


def test_encrypt_existing_screen(tmp_path):
    from database.connection import Database

    env = {"IFLOW_REQUIRE_ENCRYPTION": "true"}
    dev = Database(tmp_path / "iflowcore.db", driver="sqlite")
    dev.initialize()
    dev.close()
    app, page = make_app(tmp_path, extra_env=env)
    app.start()
    assert app.unlocker.plan().mode == "encrypt_existing"
    keys = {getattr(c, "key", None) for c in walk(page.views[-1])}
    assert {"encrypt-existing", "passphrase", "confirm", "btn-unlock"} <= keys


def test_unlock_screen_blocks_every_route(tmp_path):
    env = {"IFLOW_REQUIRE_ENCRYPTION": "true"}
    app, page = make_app(tmp_path, extra_env=env)
    app.start()
    for route in ("/", "/settings", "/radar"):
        app.navigate(route)
        assert page.route == "/unlock" and app.current_path == "/unlock"


# --- radar: offline-first feeds ---------------------------------------------------------------------

ICS = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:g1\nSUMMARY:Home game\nDTSTART:20260926T233000Z\n"
       "DTEND:20260927T023000Z\nGEO:27.9759;-82.5033\nCATEGORIES:Sports\nEND:VEVENT\nEND:VCALENDAR\n")


class Transport:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    def request(self, method, url, **kw):
        self.calls += 1
        if self.fail:
            raise ProviderError("offline")
        return HttpResponse(200, ICS.encode())


class Sky:
    def arrivals(self, icao, begin, end):
        return [FlightArrival(icao, NOW - timedelta(days=7) + timedelta(minutes=10), "DAL1")]


SETTINGS = DeviceSettings(timezone="America/New_York", radar_airport_icao="KTPA",
                          ics_feeds=[{"name": "Stadium", "url": "webcal://example.com/s.ics"}])


def test_cache_only_pass_then_network(db):
    t = Transport()
    svc = RadarService(db, SETTINGS, transport=t, opensky=Sky(), sleep=lambda s: None)
    first = svc.refresh(now=NOW, network=False)
    assert svc.needs_network and first.events == [] and t.calls == 0 and not first.offline
    full = svc.refresh(now=NOW, network=True)
    assert [e.name for e in full.events] == ["Home game"] and t.calls == 1
    assert full.arrivals[0].arrives_at == NOW + timedelta(minutes=10)     # last week projected forward
    assert full.arrivals[0].source == "opensky-pattern"
    again = svc.refresh(now=NOW + timedelta(hours=1), network=False)       # fresh cache, no fetch needed
    assert not svc.needs_network and len(again.events) == 1


def test_offline_falls_back_to_stale_cache(db):
    RadarService(db, SETTINGS, transport=Transport(), sleep=lambda s: None).refresh(now=NOW)
    later = NOW + timedelta(hours=8)                                       # ICS TTL (6 h) expired
    snap = RadarService(db, SETTINGS, transport=Transport(fail=True), sleep=lambda s: None).refresh(now=later)
    assert snap.offline and len(snap.events) == 1
    assert snap.feeds[0].stale and snap.feeds[0].error == "offline"


def test_no_cache_and_offline(db):
    snap = RadarService(db, SETTINGS, transport=Transport(fail=True), sleep=lambda s: None).refresh(now=NOW)
    assert snap.offline and snap.events == []


def test_build_radar_rows(db):
    svc = RadarService(db, SETTINGS, transport=Transport(), opensky=Sky(), sleep=lambda s: None)
    snap = svc.refresh(now=NOW)
    data = build_radar(snap, now=NOW, origin=GeoPoint(27.95, -82.46), units=UnitSystem.IMPERIAL,
                       airport_icao="KTPA", origin_label="your location")
    kinds = {r.kind for r in data.rows}
    assert kinds == {"event_arrival", "event_egress", "airport_arrivals"}
    arrival = next(r for r in data.rows if r.kind == "event_arrival")
    assert arrival.urgency == "soon" and arrival.when == "in 30 min"      # wave opens 90 min before 7:30 pm
    assert arrival.distance_text.endswith("mi")
    assert data.configured and not data.offline


def test_radar_helpers():
    assert normalize_feed_url("webcal://x.com/a.ics") == "https://x.com/a.ics"
    with pytest.raises(ValueError):
        normalize_feed_url("http://x.com/a.ics")
    assert (size_bucket(100), size_bucket(300), size_bucket(1_500)) == ("small", "medium", "large")
    assert (urgency_for(0), urgency_for(30), urgency_for(61)) == ("now", "soon", "later")
    e = VenueEvent("id", "Show", NOW, None, "Hall", GeoPoint(1, 2), "music", 500)
    assert event_from_json(event_to_json(e)) == VenueEvent("id", "Show", NOW, None, "Hall", GeoPoint(1, 2), "music",
                                                           500, source="manual")


def test_feed_cache_expiry(db):
    cache = FeedCache(db)
    cache.put("k", [1, 2], now=NOW, ttl=timedelta(hours=1))
    assert cache.get("k", NOW + timedelta(minutes=30))[2] is True
    assert cache.get("k", NOW + timedelta(hours=2))[2] is False
    assert cache.get("missing", NOW) is None


def test_radar_view_renders_offline_banner(tmp_path):
    app, page = make_app(tmp_path)
    app.start()
    app.state.update_settings(ics_feeds=[{"name": "Stadium", "url": "https://example.com/s.ics"}])
    app.services["radar_service"] = lambda ctx: RadarService(
        ctx.db, ctx.state.settings, transport=Transport(fail=True), sleep=lambda s: None,
        failures=ctx.services.setdefault("radar_failures", {}))
    app.navigate("/radar")                        # background refresh fails → cached-nothing, offline banner
    app.render()
    assert find_key(view(page), "radar-sources") is not None
    assert find_key(view(page), "radar-offline") is not None


def test_radar_force_refresh_does_not_loop(tmp_path):
    app, page = make_app(tmp_path)
    app.start()
    t = Transport()
    app.state.update_settings(ics_feeds=[{"name": "Stadium", "url": "https://example.com/s.ics"}])
    app.services["radar_service"] = lambda ctx: RadarService(ctx.db, ctx.state.settings, transport=t,
                                                             sleep=lambda s: None)
    app.navigate("/radar?refresh=1")
    assert page.route == "/radar" and t.calls == 1
    assert find_key(view(page), "radar-rows") is not None


def test_settings_saves_tax_profile_bound_to_db(tmp_path):
    from ui.viewmodels.settings import current_tax_profile, save_tax_profile

    app, page = make_app(tmp_path)
    app.start()
    save_tax_profile(app.state.db, 2026, filing_status="head_of_household", region_code="us-fl")
    save_tax_profile(app.state.db, 2026, filing_status="single", region_code="US-FL")
    prof = current_tax_profile(app.state.db, 2026)
    assert (prof["filing_status"], prof["region_code"]) == ("single", "US-FL")
    with pytest.raises(ValueError):
        save_tax_profile(app.state.db, 2026, filing_status="married", region_code=None)
    app.navigate("/settings")
    assert find_key(view(page), "tax-card") is not None


# --- brand ----------------------------------------------------------------------------------------


def test_brand_assets_exist_and_are_transparent():
    from PIL import Image

    from ui import brand

    for name in (brand.LOGO, brand.LOGO_DARK, brand.EMBLEM):
        img = Image.open(brand.ASSETS_DIR / name)
        assert img.mode == "RGBA"
        alpha = img.getchannel("A")
        assert alpha.getextrema() == (0, 255)                        # real transparency, not a painted checkerboard
        corners = [alpha.getpixel(p) for p in ((0, 0), (img.width - 1, 0), (0, img.height - 1))]
        assert corners == [0, 0, 0]
    assert (brand.ASSETS_DIR / "icon.png").is_file()                  # `flet build` app icon


def test_unlock_shows_theme_aware_logo(tmp_path):
    from ui import brand
    from ui.theme import DARK, LIGHT

    assert brand.logo(LIGHT).src == brand.LOGO and brand.logo(DARK).src == brand.LOGO_DARK
    app, page = make_app(tmp_path, extra_env={"IFLOW_REQUIRE_ENCRYPTION": "true"})
    app.start()
    logo = find_key(page.views[-1], "brand-logo")
    assert logo is not None and logo.semantics_label.startswith("iFlow Core")


def test_rail_shows_emblem(tmp_path):
    app, page = make_app(tmp_path, width=1280)
    app.start()
    assert find_key(page.views[-1], "brand-emblem") is not None


# --- layout guard for every screen ----------------------------------------------------------------------


def _wrap_expand_violations(root):
    """Flutter can't lay out an expanding child inside a wrapping Row: it draws a grey error box."""
    import flet as ft

    return [f"{type(c).__name__}(key={getattr(c, 'key', None)})" for row in walk(root)
            if isinstance(row, ft.Row) and row.wrap for c in row.controls if getattr(c, "expand", None)]


@pytest.mark.parametrize("width", [390, 1280])
def test_no_expanding_children_inside_wrapping_rows_anywhere(tmp_path, width):
    from ui import router

    app, page = make_app(tmp_path, width=width)
    app.start()
    for spec in router.ROUTES:
        if spec.path == router.UNLOCK:
            continue
        app.navigate(spec.path)
        assert _wrap_expand_violations(page.views[-1]) == [], spec.path
