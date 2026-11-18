"""Phase 8 UI: /health gating, navigation, enrollment with consent, withholding settings, reserve & splits."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from core.guild import active_plans, save_plan
from core.licensing import Period, Tier, plan
from ui.app import IFlowCoreApp

from .test_intelligence import _platform, _trip, _txn
from .ui_helpers import FakePage, find_key, texts, walk

NOW = datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc)


def make_app(tmp_path, width=1280, platform="linux", tier=None):
    page = FakePage(width, platform, "/")
    app = IFlowCoreApp(page, env={"IFLOW_DATA_DIR": str(tmp_path)}, clock=lambda: NOW, services={"motion": None})
    app.services.pop("motion")
    app.start()
    app.state.update_settings(timezone="America/New_York")
    if tier:
        app.state.license_manager().simulate_purchase(plan(tier, Period.MONTHLY), app.state.today())
    return app, page


def v(page):
    return page.views[-1]


def all_text(control):
    return " ".join(texts(control))


def seed_work(db, weeks=13):
    """Enough recent earnings and hours (averaged over the 90-day evidence window) to pass the sandbox checks."""
    uber = _platform(db, "Uber")
    dash = _platform(db, "DoorDash")
    start = date(2026, 9, 27) - timedelta(days=7 * weeks)
    for d in range(7 * weeks):
        day = start + timedelta(days=d)
        if day.weekday() >= 5:
            continue
        for h in range(3):
            t = datetime(day.year, day.month, day.day, 20 + h, tzinfo=timezone.utc)
            _trip(db, t, minutes=40, meters=10_000)
            pid = uber if h < 2 else dash
            _txn(db, pid, (t + timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%S.000Z"), gross=2_500, fee=500)
    return uber, dash


# --- gating & navigation ------------------------------------------------------------------------------


def test_locked_for_solo_and_pro_but_catalog_is_browsable(tmp_path):
    for tier in (None, Tier.PRO):
        app, page = make_app(tmp_path / str(tier), tier=tier)
        app.navigate("/health")
        assert app.last_error is None, app.last_error
        view = v(page)
        assert find_key(view, "health-locked") is not None and find_key(view, "catalog-card") is not None
        assert "Sample prices" in all_text(find_key(view, "catalog-card"))
        assert not [c for c in walk(view) if str(getattr(c, "key", "")).startswith("enroll-")]    # no enrolling
        assert find_key(view, "reserve-card") is None and find_key(view, "profile-card") is None


@pytest.mark.parametrize("width,platform", [(390, "android"), (1440, "macos")])
def test_navigation(tmp_path, width, platform):
    app, page = make_app(tmp_path, width, platform, tier=Tier.GUILD)
    app.navigate("/")
    if platform == "android":
        find_key(v(page), "appbar-health").on_click(None)
        assert app.current_path == "/health" and find_key(v(page), "nav-bar") is None
        assert find_key(v(page), "appbar-health") is None and find_key(v(page), "appbar-back") is not None
        find_key(v(page), "appbar-back").on_click(None)
        assert app.current_path == "/"
    else:
        rail = find_key(v(page), "nav-rail")
        assert [d.label for d in rail.destinations][5] == "iFlow Health"
        app.navigate("/health")
        assert find_key(v(page), "nav-rail").selected_index == 5
    assert app.last_error is None


# --- Guild member workflow ---------------------------------------------------------------------------------


def _save_profile(app, page, **overrides):
    values = {"pf-state": "FL", "pf-year": "1990", "pf-since": "2026-06-01", "pf-member": "GLD-448812", **overrides}
    for key, value in values.items():
        find_key(v(page), key).value = value
    find_key(v(page), "pf-save").on_click(None)


def test_profile_prices_and_eligibility(tmp_path):
    app, page = make_app(tmp_path, tier=Tier.GUILD)
    seed_work(app.state.db)
    app.navigate("/health")
    assert "Add your details" in all_text(find_key(v(page), "catalog-card"))
    _save_profile(app, page)
    silver = find_key(v(page), "offer-sbx-health-silver-self")
    assert "$415.00 / month" in all_text(silver)
    assert "Likely eligible" in all_text(find_key(silver, "elig-sbx-health-silver-self"))
    assert find_key(v(page), "refused-note") is not None                           # platform plan hidden
    assert "never sent" in all_text(find_key(v(page), "catalog-card"))
    find_key(v(page), "tier-family").on_select(None)
    assert "tier=family" in page.route
    assert find_key(v(page), "offer-sbx-health-silver-family") is not None
    assert not [c for c in walk(v(page)) if str(getattr(c, "key", "")).startswith("offer-sbx-life")]


def test_new_member_is_not_eligible_yet(tmp_path):
    app, page = make_app(tmp_path, tier=Tier.GUILD)
    seed_work(app.state.db)
    app.navigate("/health")
    _save_profile(app, page, **{"pf-since": "2026-09-20"})
    enroll_btn = find_key(v(page), "enroll-sbx-health-silver-self")
    assert enroll_btn.disabled and "Not eligible yet" in all_text(find_key(v(page), "elig-sbx-health-silver-self"))
    assert find_key(v(page), "enroll-sbx-dental-basic-self").disabled         # dental also needs 30 days
    assert not find_key(v(page), "enroll-sbx-life-50k-self").disabled          # life has no membership rule


def test_enrollment_with_consent(tmp_path):
    app, page = make_app(tmp_path, tier=Tier.GUILD)
    seed_work(app.state.db)
    app.navigate("/health")
    _save_profile(app, page)
    find_key(v(page), "enroll-sbx-health-silver-self").on_click(None)
    d = page.dialogs[-1]
    find_key(d, "en-name").value = "Jordan Rivera"
    find_key(d, "en-email").value = "jordan@example.com"
    find_key(d, "en-dob").value = "1990-04-02"
    find_key(d, "en-review").on_click(None)
    consent = page.dialogs[-1]
    assert consent.key == "consent-dialog"
    body = all_text(consent)
    assert "Jordan Rivera" in body and "1990-04-02" not in body and "GLD-448812" not in body     # masked
    assert "Never sent: your earnings, your platforms" in body
    find_key(consent, "en-submit").on_click(None)                                  # not ticked yet
    assert active_plans(app.state.db, include_pending=True) == []
    find_key(consent, "en-agree").value = True
    find_key(consent, "en-submit").on_click(None)
    (p,) = active_plans(app.state.db, include_pending=True)
    assert (p.status, p.plan_name, p.coverage_start) == ("pending", "Guild Silver PPO", date(2026, 10, 1))
    row = find_key(v(page), "coverage-health")
    assert "Starts Oct 1" in all_text(row) and "ref SBX-" in all_text(row)
    assert find_key(v(page), "enroll-sbx-health-silver-self").disabled                # "Enrolled"


def test_withholding_settings_and_reserve(tmp_path):
    app, page = make_app(tmp_path, tier=Tier.GUILD)
    uber, dash = seed_work(app.state.db)
    save_plan(app.state.db, association="Guild", plan_name="Silver", monthly_premium_minor=45_000,
              coverage_start=date(2026, 9, 1))
    app.navigate("/health")
    assert find_key(v(page), "split-card") is not None                             # auto mode ran on load
    assert "Automatic" in all_text(find_key(v(page), "policy-summary"))
    find_key(v(page), "btn-policy").on_click(None)
    d = page.dialogs[-1]
    find_key(d, "wp-mode").value = "percent"
    find_key(d, "wp-rate").value = "7"
    find_key(d, f"wp-exclude-{dash}").value = True
    find_key(d, "wp-save").on_click(None)
    pol = app.state.settings.health_withholding
    assert (pol["mode"], pol["rate_bp"], pol["excluded_platforms"]) == ("percent", 700, [dash])
    assert "7% of each payout (7¢ per dollar)" in all_text(find_key(v(page), "policy-summary"))
    # a new payout arrives through the normal import path: the reserve updates on the next render
    before = app.state.db
    _txn(before, uber, "2026-09-27T14:00:00.000Z", gross=10_000, fee=0)
    app.navigate("/health")
    assert "$7.00 set aside from 1 payout(s)" in all_text(find_key(v(page), "sync-note"))
    assert find_key(v(page), "kpi-reserve") is not None and find_key(v(page), "reserve-meter") is not None


def test_bad_policy_is_rejected(tmp_path):
    app, page = make_app(tmp_path, tier=Tier.GUILD)
    app.navigate("/health")
    find_key(v(page), "btn-policy").on_click(None)
    d = page.dialogs[-1]
    find_key(d, "wp-mode").value = "percent"
    find_key(d, "wp-rate").value = "40"                                            # above the 25% cap
    find_key(d, "wp-save").on_click(None)
    assert app.state.settings.health_withholding is None                         # nothing saved
    assert d in page.dialogs and d.open                                             # dialog stays open to fix


def test_manual_plan_payment_and_end(tmp_path):
    app, page = make_app(tmp_path, tier=Tier.GUILD)
    app.navigate("/health")
    find_key(v(page), "btn-add-own-plan").on_click(None)
    d = page.dialogs[-1]
    find_key(d, "mp-line").value = "dental"
    find_key(d, "mp-association").value = "Freelancers Union"
    find_key(d, "mp-name").value = "Dental Plus"
    find_key(d, "mp-premium").value = "39"
    find_key(d, "mp-save").on_click(None)
    assert "Dental: Dental Plus" in all_text(find_key(v(page), "coverage-dental"))
    find_key(v(page), "btn-premium-paid").on_click(None)
    find_key(page.dialogs[-1], "pay-save").on_click(None)
    assert "covered" in all_text(find_key(v(page), "reserve-card"))
    find_key(v(page), "end-dental").on_click(None)
    find_key(page.dialogs[-1], "end-confirm").on_click(None)
    assert find_key(v(page), "coverage-dental") is None


def test_import_triggers_withholding(tmp_path):
    """Statement imports fund premiums immediately (the earnings-stream link)."""
    from ui.viewmodels.health import sync_now

    app, page = make_app(tmp_path, tier=Tier.GUILD)
    uber = _platform(app.state.db)
    save_plan(app.state.db, association="G", plan_name="S", monthly_premium_minor=45_000,
              coverage_start=date(2026, 9, 1))
    _txn(app.state.db, uber, "2026-09-26T22:00:00.000Z", gross=10_000, fee=0)
    rep = sync_now(app.state)
    assert rep.processed == 1 and rep.withheld_minor > 0
    app2, _ = make_app(tmp_path / "solo")
    assert sync_now(app2.state) is None                                         # not a Guild member: nothing


def test_every_route_renders_on_every_tier_including_health(tmp_path):
    for width, platform in ((390, "android"), (1440, "macos")):
        app, page = make_app(tmp_path / platform, width, platform)
        for tier in (None, Tier.PRO, Tier.GUILD):
            if tier:
                app.state.license_manager().simulate_purchase(plan(tier, Period.MONTHLY), app.state.today())
            for route in ("/", "/finances", "/fleet", "/radar", "/insights", "/health", "/settings", "/plans"):
                app.navigate(route)
                assert app.last_error is None, (platform, tier, route, app.last_error)
