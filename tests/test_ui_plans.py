"""Phase 7 UI & gates: plans page, simulated upgrade/downgrade, license keys, and every app-level feature gate."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from core.licensing import Feature, FeatureLocked, LicenseError, Period, Status, Tier, plan
from core.licensing.tokens import ENV_PUBKEYS, LicenseClaims, public_key_hex, sign
from ui.app import IFlowCoreApp
from ui.viewmodels.licensing import EXPORT_FORMATS, comparison, run_export, status_view, tier_cards

from .test_licensing import VENDOR
from .ui_helpers import FakePage, find_key, texts, walk

NOW = datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc)


def make_app(tmp_path, width=1280, platform="linux", extra_env=None, clock=None):
    page = FakePage(width, platform, "/")
    env = {"IFLOW_DATA_DIR": str(tmp_path), **(extra_env or {})}
    app = IFlowCoreApp(page, env=env, clock=clock or (lambda: NOW), services={"motion": None})
    app.services.pop("motion")
    app.start()
    return app, page


def v(page):
    return page.views[-1]


def upgrade(app, tier=Tier.PRO, period=Period.MONTHLY):
    return app.state.license_manager().simulate_purchase(plan(tier, period), app.state.today())


# --- view-model --------------------------------------------------------------------------------------


def test_tier_cards_and_comparison():
    from core.licensing import FREE_STATE

    cards = tier_cards(FREE_STATE)
    assert [c.name for c in cards] == ["Solo Core", "iFlow Pro", "iFlow Health & Guild"]
    solo, pro, guild = cards
    assert solo.current and solo.price == "Free"
    assert pro.price == "$6.99 / month" and pro.price_note == "or $60 / year (save 28%)"
    assert guild.price == "$14.99 / month" and guild.price_note is None
    assert "AI receipt scanning" in pro.features and "Association Health Plan tracking" in guild.features
    assert guild.includes_below == "Everything in iFlow Pro, plus:"
    rows = {r.label: r.tiers for r in comparison()}
    assert rows["Automatic trip detection"] == (False, True, True)
    assert rows["CSV export"] == (True, True, True)
    assert rows["Premium withholding"] == (False, False, True)


# --- plans page -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("width,platform", [(390, "android"), (1440, "macos")])
def test_plans_page_renders(tmp_path, width, platform):
    app, page = make_app(tmp_path, width, platform)
    app.navigate("/plans?focus=pro")
    assert app.last_error is None, app.last_error
    view = v(page)
    for key in ("plan-status", "tier-solo", "tier-pro", "tier-guild", "comparison-card", "license-card",
                "how-it-works", "cta-pro_monthly", "cta-pro_annual", "cta-guild_monthly"):
        assert find_key(view, key) is not None, key
    assert find_key(view, "cta-solo").disabled is True                       # current plan
    if platform == "android":
        assert find_key(view, "appbar-back") is not None and find_key(view, "nav-bar") is None
        find_key(view, "appbar-back").on_click(None)
        assert app.current_path == "/settings"                              # back goes to the parent
    else:
        assert find_key(view, "nav-rail").selected_index == 6              # Settings highlighted


def test_simulated_upgrade_flow_shows_price_and_activates(tmp_path):
    app, page = make_app(tmp_path)
    app.navigate("/plans")
    find_key(v(page), "cta-pro_annual").on_click(None)
    dialog = page.dialogs[-1]
    body = " ".join(texts(dialog))
    assert "Due today: $6,000.00" not in body and "Due today: $60.00" in body
    assert "Renews Sep 27, 2027 at $60.00" in body and "No payment is taken" in body
    find_key(dialog, "upgrade-confirm").on_click(None)
    st = app.state.license
    assert (st.status, st.tier, st.claims.period) == (Status.ACTIVE, Tier.PRO, Period.ANNUAL)
    assert find_key(v(page), "cta-pro").disabled is True
    assert "Simulated license valid until Sep 27, 2027" in " ".join(texts(find_key(v(page), "plan-status")))


def test_switching_plans_itemizes_credit(tmp_path):
    t = [NOW]
    app, page = make_app(tmp_path, clock=lambda: t[0])
    upgrade(app, Tier.PRO, Period.MONTHLY)
    t[0] = NOW + timedelta(days=15)
    app.navigate("/plans")
    find_key(v(page), "cta-guild_monthly").on_click(None)
    body = " ".join(texts(page.dialogs[-1]))
    assert "Credit for unused time on your current plan: −$3.49" in body and "Due today: $11.50" in body


def test_downgrade_keeps_data(tmp_path):
    app, page = make_app(tmp_path)
    upgrade(app, Tier.GUILD)
    from core.guild import save_plan

    save_plan(app.state.db, association="Guild", plan_name="PPO", monthly_premium_minor=45_000,
              coverage_start=date(2026, 9, 1))
    app.navigate("/plans")
    find_key(v(page), "cta-solo").on_click(None)
    assert "Nothing you've recorded is deleted" in " ".join(texts(page.dialogs[-1]))
    find_key(page.dialogs[-1], "downgrade-confirm").on_click(None)
    assert app.state.license.status is Status.FREE
    app.navigate("/finances")
    card = find_key(v(page), "guild-card")                                # plan still shown, read-only
    assert "Health: PPO" in " ".join(texts(card)) and find_key(card, "guild-locked") is not None
    app.navigate("/health")
    assert find_key(v(page), "coverage-health") is not None                 # still visible after downgrade
    assert find_key(v(page), "btn-policy") is None and find_key(v(page), "end-health") is None


def test_production_build_offers_no_simulation(tmp_path):
    env = {"IFLOW_ENV": "production", "IFLOW_DB_KEY": "correct horse battery staple x"}
    try:
        app, page = make_app(tmp_path, extra_env=env)
    except Exception as exc:                                               # no SQLCipher driver on this machine
        pytest.skip(f"production mode needs SQLCipher: {exc}")
    app.navigate("/plans")
    find_key(v(page), "cta-pro_monthly").on_click(None)
    assert "aren't available in this build" in " ".join(texts(page.dialogs[-1]))
    with pytest.raises(LicenseError):
        upgrade(app)


def test_license_key_activation(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_PUBKEYS, public_key_hex(VENDOR.public_key()))
    app, page = make_app(tmp_path)
    import uuid

    tok = sign(LicenseClaims(str(uuid.uuid4()), "vendor", Tier.GUILD, Period.MONTHLY, NOW - timedelta(days=1),
                             NOW + timedelta(days=29), 7, "USD", 1499), VENDOR)
    app.navigate("/plans")
    find_key(v(page), "license-key").value = tok
    find_key(v(page), "btn-activate").on_click(None)
    assert app.state.license.tier is Tier.GUILD
    assert "issued by iFlow Core" in " ".join(texts(find_key(v(page), "license-card")))
    find_key(v(page), "license-key").value = "RSL1.bogus.key"
    find_key(v(page), "btn-activate").on_click(None)
    assert app.state.license.tier is Tier.GUILD                            # a bad key doesn't clobber a good one


def test_settings_plan_card(tmp_path):
    app, page = make_app(tmp_path)
    app.navigate("/settings")
    assert "Solo Core" in " ".join(texts(find_key(v(page), "plan-card")))
    find_key(v(page), "btn-plans").on_click(None)
    assert app.current_path == "/plans"


# --- gates ---------------------------------------------------------------------------------------------------


def test_motion_sentry_gate(tmp_path):
    app, page = make_app(tmp_path)
    app.navigate("/settings")
    card = find_key(v(page), "motion-card")
    assert find_key(card, "motion-locked") is not None and find_key(card, "sentry-switch").disabled

    from ui.motion_bridge import MotionBridge

    notes = []
    bridge = MotionBridge(page, app.state, notify=notes.append, show_dialog=lambda d: None, close_dialog=lambda: None,
                          on_trip=lambda e: None)
    asyncio.run(bridge.set_enabled(True, background=False))
    assert not app.state.settings.sentry_enabled and "iFlow Pro" in notes[-1]


def test_lapsed_pro_turns_sentry_off_at_unlock_but_keeps_trips(tmp_path):
    t = [NOW]
    app, page = make_app(tmp_path, clock=lambda: t[0])
    upgrade(app, Tier.PRO)
    app.state.update_settings(sentry_enabled=True)
    from core.fleet.mileage import log_trip

    log_trip(app.state.db, started_at=NOW, distance_m=5000)
    t[0] = NOW + timedelta(days=45)                                       # past expiry + grace
    app.lock()
    app.unlocker.config = app.config
    app._on_unlocked(app.unlocker.open())
    assert not app.state.settings.sentry_enabled
    assert find_key(v(page), "license-notice") is not None
    with app.state.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM mileage_logs").fetchone()[0] == 1
    dismiss = next(c for c in walk(find_key(v(page), "license-notice")) if getattr(c, "tooltip", None) == "Dismiss")
    dismiss.on_click(None)
    assert find_key(v(page), "license-notice") is None


def test_grace_banner(tmp_path):
    t = [NOW]
    app, page = make_app(tmp_path, clock=lambda: t[0])
    upgrade(app, Tier.PRO)
    t[0] = NOW + timedelta(days=32)                                       # expired 2 days ago, in grace
    app.navigate("/")
    assert app.state.license.status is Status.GRACE and app.state.allows(Feature.MOTION_SENTRY)
    assert "5 more day(s)" in " ".join(texts(find_key(v(page), "license-grace")))


class StubDecoder:
    def __init__(self):
        self.calls = 0

    def decode(self, vin, **kw):
        self.calls += 1
        from core.fleet.http import ProviderError

        raise ProviderError("offline")


def _add_vehicle(app, page, nickname, vin=""):
    from ui.views import fleet

    fleet.open_vehicle_dialog(fleet.ViewContext(app.state, app.layout, app.pal, app.navigate, app.refresh, app.notify,
                                                app.show_dialog, app.close_dialog, services=app.services))
    dialog = page.dialogs[-1]
    if find_key(dialog, "locked-dialog") is not None or getattr(dialog, "key", None) == "locked-dialog":
        return dialog
    fields = [c for c in walk(dialog) if c.__class__.__name__ == "TextField"]
    fields[0].value, fields[1].value, fields[2].value = nickname, vin, "1000"
    save = next(c for c in walk(dialog) if c.__class__.__name__ == "FilledButton")
    save.on_click(None)
    return dialog


def test_free_tier_has_one_vehicle_and_no_vin_decoding(tmp_path):
    app, page = make_app(tmp_path)
    decoder = StubDecoder()
    app.services["decoder"] = decoder
    _add_vehicle(app, page, "Camry", "1HGCM82633A004352")
    from ui.viewmodels.licensing import vehicle_count

    assert vehicle_count(app.state.db) == 1 and decoder.calls == 0         # VIN kept, not decoded
    dialog = _add_vehicle(app, page, "Prius")
    assert dialog.key == "locked-dialog" and vehicle_count(app.state.db) == 1


def test_pro_decodes_vins_and_survives_offline_providers(tmp_path):
    app, page = make_app(tmp_path)
    upgrade(app, Tier.PRO)
    decoder = StubDecoder()
    app.services["decoder"] = decoder
    _add_vehicle(app, page, "Camry", "1HGCM82633A004352")
    _add_vehicle(app, page, "Prius")
    from ui.viewmodels.licensing import vehicle_count

    assert decoder.calls == 1 and vehicle_count(app.state.db) == 2          # offline decode: vehicle still saved


def test_export_gates(tmp_path):
    app, page = make_app(tmp_path)
    s = app.state
    start, end = date(2026, 1, 1), date(2026, 9, 27)
    assert run_export(s, "csv", tmp_path / "a.csv", start=start, end=end).path.exists()        # free forever
    assert run_export(s, "mileage_csv", tmp_path / "m.csv", start=start, end=end).path.exists()
    for key in ("qfx", "ofx", "qif", "quickbooks"):
        with pytest.raises(FeatureLocked):
            run_export(s, key, tmp_path / f"x.{key}", start=start, end=end)
        assert not (tmp_path / f"x.{key}").exists()
    upgrade(app, Tier.PRO)
    for key in ("qfx", "ofx", "qif", "quickbooks"):
        assert run_export(s, key, tmp_path / f"y.{key}", start=start, end=end).path.exists()
    with pytest.raises(ValueError):
        run_export(s, "pdf", tmp_path / "z.pdf", start=start, end=end)


def test_export_card_labels_and_locked_dialog(tmp_path):
    app, page = make_app(tmp_path)
    app.navigate("/finances")
    fmt = find_key(v(page), "export-format")
    labels = {o.key: o.text for o in fmt.options}
    assert labels["csv"] == EXPORT_FORMATS["csv"].label and labels["qfx"].endswith("(Pro)")
    fmt.value = "qfx"
    asyncio.run(find_key(v(page), "btn-export").on_click(None))
    assert page.dialogs[-1].key == "locked-dialog"
    assert "Plain CSV exports stay free" in " ".join(texts(page.dialogs[-1]))


def test_guild_gate(tmp_path):
    app, page = make_app(tmp_path)
    app.navigate("/finances")
    assert find_key(v(page), "guild-card-lock") is not None
    upgrade(app, Tier.PRO)
    app.navigate("/finances")
    assert find_key(v(page), "guild-card-lock") is not None                 # Pro isn't enough
    upgrade(app, Tier.GUILD)
    app.navigate("/finances")
    find_key(v(page), "btn-open-health").on_click(None)
    assert app.current_path == "/health" and find_key(v(page), "reserve-card") is not None


def test_status_view_messages():
    from core.licensing import FREE_STATE, evaluate

    assert status_view(FREE_STATE).title == "Solo Core"
    bad = evaluate("RSL1.x.y", NOW)
    assert status_view(bad).level == "critical" and "Solo Core until it's fixed" in status_view(bad).message


def test_every_route_still_renders_on_every_tier(tmp_path):
    app, page = make_app(tmp_path, 390, "android")
    for tier in (None, Tier.PRO, Tier.GUILD):
        if tier:
            upgrade(app, tier)
        for route in ("/", "/finances", "/fleet", "/radar", "/insights", "/settings", "/plans"):
            app.navigate(route)
            assert app.last_error is None, (tier, route, app.last_error)


def test_close_dialog_closes_our_dialog_not_a_snackbar(tmp_path):
    """Regression: Flet's pop_dialog() closes the topmost *open* overlay, which after any notify() is the
    SnackBar, so the upgrade dialog stayed on screen. The shell must close the dialog it opened."""
    import flet as ft

    app, page = make_app(tmp_path)
    dialog = ft.AlertDialog(title=ft.Text("x"))
    app.show_dialog(dialog)
    app.notify("something happened")                 # a SnackBar is now the topmost open overlay
    app.close_dialog()
    assert dialog.open is False and dialog not in page.dialogs
    assert any(isinstance(d, ft.SnackBar) and d.open for d in page.dialogs)      # the notification is untouched
    app.close_dialog()                               # nothing left to close: harmless
