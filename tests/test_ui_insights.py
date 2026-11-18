"""Insights view-model and view: heat levels, trends with gaps, peaks, zones opt-in, adaptive navigation."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from core.config import UnitSystem
from core.intelligence import ZoneStore, build_report
from core.intelligence.models import Activity
from core.intelligence.temporal import TrendPoint
from ui.app import IFlowCoreApp
from ui.viewmodels.intelligence import LEVELS, _levels, heatmap_view, insights_from_report, nice_ceiling, trend_view

from .test_intelligence import NY, TPA, seed_history
from .ui_helpers import FakePage, find_key, texts

NOW = datetime(2026, 9, 26, 21, 30, tzinfo=timezone.utc)
TODAY = date(2026, 9, 26)


def report(db, period="90d", **kw):
    return build_report(db, today=TODAY, tz=NY, currency="USD", period=period, zone_store=ZoneStore(db),
                        reference=("TPA", TPA), vehicle_cost=lambda d: 0, **kw)


# --- view-model -----------------------------------------------------------------------------


def test_levels_are_rank_based_and_ties_share_a_level():
    values = {i: v for i, v in enumerate([100, 100, 200, 300, 400, 500, 10_000, 1, 2, 3])}
    lv = _levels(values)
    assert set(lv.values()) <= set(range(LEVELS))
    assert lv[6] == LEVELS - 1 and lv[7] == 0                    # extremes
    assert lv[0] == lv[1]                                        # equal values, equal color
    assert lv[5] >= 3                                            # one outlier doesn't flatten the rest
    assert _levels({}) == {}


def test_nice_ceiling():
    assert [nice_ceiling(v) for v in (0, 7, 11, 21, 240, 260, 999, 1001)] == [0, 10, 20, 25, 250, 500, 1000, 2000]


def test_heatmap_view_from_history(db):
    seed_history(db)
    r = report(db)
    hv = heatmap_view(r, "per_hour")
    assert len(hv.cells) == 168 and hv.note is None
    fri9 = hv.cells[4 * 24 + 21]
    assert fri9.level == LEVELS - 1 and "Friday 9 PM" in fri9.tooltip and "/h" in fri9.tooltip
    assert hv.cells[0].level == -1 and "no activity" in hv.cells[0].tooltip          # Monday midnight
    assert [lvl for lvl, _ in hv.legend] == sorted(lvl for lvl, _ in hv.legend)
    assert hv.top[0][0].startswith("Fri")
    trips = heatmap_view(r, "trips")
    assert trips.cells[4 * 24 + 21].level >= 0 and "trip" in trips.cells[4 * 24 + 21].tooltip


def test_heatmap_without_time_data_explains():
    act = Activity(start=TODAY - timedelta(days=6), end=TODAY, tz=NY, currency="USD")
    from core.intelligence.temporal import build_grid

    class R:                                                          # minimal stand-in for a report
        grid = build_grid(act, [])
        currency = "USD"

    hv = heatmap_view(R, "per_hour")
    assert hv.note and all(c.level == -1 for c in hv.cells) and hv.legend == ()


def test_trend_view_breaks_on_missing_values(db):
    seed_history(db)
    r = report(db, period="30d")
    tv = trend_view(r, "per_hour", UnitSystem.IMPERIAL)
    assert len(tv.values) == 30 and None in tv.values                # days without work have no rate
    assert tv.texts[tv.values.index(None)] == "no data"
    assert tv.y_max >= max(v for v in tv.values if v is not None)
    assert tv.ticks[0][0] == 0 and tv.ticks[-1][0] == tv.y_max
    hours = trend_view(r, "hours", UnitSystem.IMPERIAL)
    assert not hours.is_money and hours.y_max % 3600 == 0
    km = trend_view(r, "per_distance", UnitSystem.METRIC)
    assert km.label == "Net per km"


def test_insights_data_kpis_and_peaks(db):
    seed_history(db)
    data = insights_from_report(report(db), UnitSystem.IMPERIAL, params={"trend": "bogus", "heat": "trips"},
                                zones_enabled=True)
    keys = [k.key for k in data.kpis]
    assert keys == ["net", "per_hour", "per_distance", "after_car", "hours", "trips"]
    assert data.trend.metric == "net" and data.heatmap.metric == "trips"          # bad param → default
    assert data.peaks and data.peaks[0].title == "Friday night" and "/h" in data.peaks[0].rate
    assert data.zones.status == "ok" and data.zones.rows[0].label == "TPA airport area"
    assert data.zones.rows[0].per_trip and data.zones.footer


def test_zones_view_off_when_not_enabled(db):
    data = insights_from_report(report(db), UnitSystem.IMPERIAL, zones_enabled=False)
    assert data.zones.enabled is False and data.zones.status == "off"


# --- view in the app shell ------------------------------------------------------------------


def make_app(tmp_path, width=1280, platform="linux", route="/"):
    page = FakePage(width, platform, route)
    app = IFlowCoreApp(page, env={"IFLOW_DATA_DIR": str(tmp_path)}, clock=lambda: NOW,
                      services={"motion": None})
    app.services.pop("motion")
    app.start()
    app.state.update_settings(timezone=NY, radar_airport_icao="KTPA")
    return app, page


def view(page):
    return page.views[-1]


@pytest.mark.parametrize("width,platform", [(390, "android"), (1440, "macos")])
def test_insights_renders_with_history(tmp_path, width, platform):
    app, page = make_app(tmp_path, width, platform)
    seed_history(app.state.db)
    app.navigate("/insights")
    assert app.last_error is None, app.last_error
    v = view(page)
    for key in ("insight-kpis", "peaks-card", "heatmap-card", "trend-card", "areas-card", "privacy-line", "peak-1",
                "trend-chart", "heat-legend"):
        assert find_key(v, key) is not None, key
    assert find_key(v, "heat-0-0") is not None and find_key(v, "heat-6-23") is not None
    assert "Friday night · 9 PM – 12 AM" in texts(find_key(v, "peak-1"))
    assert find_key(v, "btn-enable-zones") is not None                    # zones are opt-in


@pytest.mark.parametrize("width,platform", [(390, "android"), (700, "ios"), (1440, "macos")])
def test_no_expanding_children_inside_wrapping_rows(tmp_path, width, platform):
    """Flutter can't lay out an expanding child inside a wrap; it renders a grey error box on device."""
    import flet as ft

    from .ui_helpers import walk

    app, page = make_app(tmp_path, width, platform)
    seed_history(app.state.db)
    app.state.update_settings(insights_zones_enabled=True)
    app.navigate("/insights")
    bad = [c for row in walk(view(page)) if isinstance(row, ft.Row) and row.wrap
           for c in row.controls if getattr(c, "expand", None)]
    assert bad == []


def test_insights_empty_state(tmp_path):
    app, page = make_app(tmp_path)
    app.navigate("/insights")
    assert find_key(view(page), "insights-empty") is not None and app.last_error is None


def test_period_and_metric_chips_are_links(tmp_path):
    app, page = make_app(tmp_path)
    seed_history(app.state.db)
    app.navigate("/insights")
    find_key(view(page), "period-ytd").on_select(None)
    assert page.route.startswith("/insights?") and "period=ytd" in page.route
    assert find_key(view(page), "period-ytd").selected is True
    find_key(view(page), "heat-metric-earnings").on_select(None)
    assert "heat=earnings" in page.route and "period=ytd" in page.route        # other params kept
    assert "Typical weekly earnings by hour" in " ".join(texts(find_key(view(page), "heatmap-card")))


def test_enable_and_forget_zones(tmp_path):
    app, page = make_app(tmp_path)
    seed_history(app.state.db)                                              # records zones directly
    app.navigate("/insights")
    find_key(view(page), "btn-enable-zones").on_click(None)
    assert app.state.settings.insights_zones_enabled
    assert find_key(view(page), "zone-grid") is not None and find_key(view(page), "zone-row-1") is not None
    assert ZoneStore(app.state.db).count() > 0
    find_key(view(page), "btn-forget-zones").on_click(None)
    assert ZoneStore(app.state.db).count() == 0 and not app.state.settings.insights_zones_enabled
    assert find_key(view(page), "btn-enable-zones") is not None


def test_phone_navigation_settings_in_app_bar(tmp_path):
    app, page = make_app(tmp_path, 390, "android")
    app.navigate("/insights")
    bar = find_key(view(page), "nav-bar")
    assert [d.label for d in bar.destinations] == ["Dashboard", "Finances", "Fleet", "Radar", "Insights"]
    assert bar.selected_index == 4
    find_key(view(page), "appbar-settings").on_click(None)
    assert app.current_path == "/settings" and find_key(view(page), "nav-bar") is None
    find_key(view(page), "appbar-back").on_click(None)
    assert app.current_path == "/insights"                                 # back returns to the last tab


def test_desktop_rail_has_all_destinations(tmp_path):
    app, page = make_app(tmp_path, 1440, "macos")
    app.navigate("/insights")
    rail = find_key(view(page), "nav-rail")
    assert len(rail.destinations) == 7 and rail.selected_index == 4
    assert find_key(view(page), "appbar-settings") is None
    rail.on_change(type("E", (), {"control": type("C", (), {"selected_index": 6})()})())
    assert app.current_path == "/settings"


def test_trend_points_sum_to_kpis(db):
    seed_history(db)
    r = report(db, period="ytd")
    assert r.granularity == "month"
    assert sum(p.net_minor for p in r.trend) == r.efficiency.net_minor
    assert isinstance(r.trend[0], TrendPoint)
