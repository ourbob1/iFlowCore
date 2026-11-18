"""
Settings: plan, display preferences, tax profile, platforms, trip detection,
demand-radar sources, AI service & diagnostics, and security. Each section saves on its own button so
a mistake in one field never blocks the others.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import flet as ft

from core.ai.client import AIServiceError, validate_service_url
from core.config import ConfigError, UnitSystem, save_preferences
from core.licensing import Feature
from core.fleet.demand import AIRPORTS
from core.timeutil import zone
from ui.components.common import card, text
from ui.components.upsell import locked_row
from ui.viewmodels.ai import check_connection, service_url, upload_reports
from ui.viewmodels.radar import normalize_feed_url
from ui.viewmodels.settings import (
    STATUS_LABELS,
    add_platform,
    current_tax_profile,
    list_platforms,
    region_options,
    save_tax_profile,
)
from ui.views.base import ViewContext, page_column


def build(ctx: ViewContext) -> ft.Control:
    from ui.views.account import account_card

    col = {"xs": 12, "lg": 6}
    return page_column(ctx, ft.ResponsiveRow([
        account_card(ctx, col), plan_card(ctx, col), preferences_card(ctx, col), tax_card(ctx, col), platforms_card(ctx, col),
        motion_card(ctx, col), radar_card(ctx, col), ai_card(ctx, col), security_card(ctx, col),
        formats_card(ctx, col),
    ], spacing=12, run_spacing=12), key="view-settings")


def formats_card(ctx: ViewContext, col: dict) -> ft.Container:
    """Statement formats published by iFlow Core (Parser Studio): signed files from the iFlow CDN."""
    from core.financial.parsers.dynamic import SchemaError
    from core.financial.parsers.sync import import_file, summary
    from ui.viewmodels import formats

    pal, st = ctx.pal, ctx.state.settings
    status = text(ctx.services.pop("formats_status", ""), pal, size=13, role="secondary")
    status.key = "formats-status"
    enabled = ctx.state.config.formats_url is not None

    def done(message: str | None) -> None:
        ctx.services["formats_status"] = message or ""
        ctx.refresh()

    def check_now(e: Any) -> None:
        ctx.services["formats_status"] = "Checking…"

        def work() -> None:
            done(formats.check(ctx.state, ctx.services, force=True))

        runner = ctx.services.get("run_background")
        runner(work) if runner else work()

    def toggle(e: Any) -> None:
        ctx.state.update_settings(formats_auto_update=bool(e.control.value))
        ctx.refresh()

    async def import_(e: Any) -> None:
        picker = ctx.services.get("file_picker")
        if picker is None:
            ctx.notify("File picking isn't available here")
            return
        files = await picker.pick_files(dialog_title="Choose a statement-format file")
        if not files or not files[0].path:
            return
        try:
            done(summary(import_file(ctx.db, files[0].path)))
        except (SchemaError, OSError) as exc:
            done(str(exc))

    rows = [text(f"{f.name} · {f.platform} · v{f.version}", pal, size=13) for f in formats.installed(ctx.db)]
    checked = st.formats_checked_at.replace("T", " ")[:16] + " UTC" if st.formats_checked_at else "never"
    controls: list[ft.Control] = [
        text("When a platform changes its statement layout, iFlow Core publishes an updated format. The app "
             "downloads the signed file (no account or device details are sent), checks iFlow Core's signature, "
             "and only then installs it. Your statements never leave this device.", pal, size=13,
             role="secondary"),
        *(rows or [text("None installed yet. Built-in formats still work.", pal, size=13, role="muted")]),
    ]
    if enabled:
        controls += [
            ft.Switch(label="Get new formats automatically (once a day)", value=st.formats_auto_update,
                      on_change=toggle, key="formats-auto"),
            text(f"Last checked: {checked}", pal, size=12, role="muted"),
        ]
    controls += [
        ft.Row([*([ft.OutlinedButton("Check now", icon=ft.Icons.SYNC, on_click=check_now, key="btn-formats-check",
                                     height=ctx.layout.min_target)] if enabled else []),
                ft.TextButton("Import format file", on_click=import_, key="btn-formats-import",
                              height=ctx.layout.min_target)], wrap=True),
        status,
    ]
    return card(*controls, pal=pal, title="Statement formats", col=col, key="formats-card")


def _save_button(label: str, handler: Any, ctx: ViewContext, key: str) -> ft.FilledButton:
    return ft.FilledButton(label, on_click=handler, height=ctx.layout.min_target, key=key)


# -- plan -------------------------------------------------------------------------------


def plan_card(ctx: ViewContext, col: dict) -> ft.Container:
    from ui.components.common import status_banner
    from ui.viewmodels.licensing import status_view

    sv = status_view(ctx.state.license)
    return card(status_banner(sv.level, sv.title, sv.message, ctx.pal),
                ft.Row([ft.FilledButton("Compare plans", icon=ft.Icons.WORKSPACE_PREMIUM_OUTLINED,
                                        on_click=lambda e: ctx.navigate("/plans"), height=ctx.layout.min_target,
                                        key="btn-plans")]),
                text("Plans are checked on this device with a signed license key. Nothing is sent anywhere.",
                     ctx.pal, size=12, role="muted"),
                pal=ctx.pal, title="Your plan", col=col, key="plan-card")


# -- preferences ------------------------------------------------------------------


def preferences_card(ctx: ViewContext, col: dict) -> ft.Container:
    s = ctx.state
    prefs = s.config.preferences
    units = ft.Dropdown(label="Distance units", value=prefs.unit_system.value, options=[
        ft.DropdownOption(key="imperial", text="Miles"), ft.DropdownOption(key="metric", text="Kilometers")])
    currency = ft.TextField(label="Base currency (ISO)", value=prefs.base_currency, max_length=3,
                            capitalization=ft.TextCapitalization.CHARACTERS)
    tz = ft.TextField(label="Time zone (IANA)", value=s.tz, hint_text="America/New_York")
    goal = ft.TextField(label="Monthly profit goal (whole units, optional)", keyboard_type=ft.KeyboardType.NUMBER,
                        value=str(s.settings.monthly_profit_goal_minor // 100) if s.settings.monthly_profit_goal_minor else "")

    def save(e: Any) -> None:
        try:
            zone(tz.value or "")
            new_prefs = prefs.with_updates(unit_system=UnitSystem(units.value or "imperial"),
                                           base_currency=currency.value or "USD")
            goal_raw = (goal.value or "").replace(",", "").strip()
            if goal_raw and not goal_raw.isdigit():
                raise ValueError("Goal must be a whole number")
        except (ConfigError, ValueError) as exc:
            ctx.notify(str(exc))
            return
        save_preferences(new_prefs, s.config.preferences_path)
        s.config = replace(s.config, preferences=new_prefs)
        from core.config import minor_units
        s.update_settings(timezone=tz.value,
                          monthly_profit_goal_minor=int(goal_raw) * 10 ** minor_units(new_prefs.base_currency)
                          if goal_raw else None)
        ctx.notify("Preferences saved")
        ctx.refresh()

    return card(units, currency, tz, goal, _save_button("Save", save, ctx, "save-prefs"),
                pal=ctx.pal, title="Display", col=col, key="prefs-card")


# -- tax profile ---------------------------------------------------------------------


def tax_card(ctx: ViewContext, col: dict) -> ft.Container:
    year = ctx.state.today().year
    current = current_tax_profile(ctx.db, year) or {}
    status = ft.Dropdown(label=f"{year} filing status", value=current.get("filing_status", "single"),
                         options=[ft.DropdownOption(key=k, text=v) for k, v in STATUS_LABELS.items()])
    region = ft.Dropdown(label="State", value=current.get("region_code") or "",
                         options=[ft.DropdownOption(key="", text="Not set")]
                         + [ft.DropdownOption(key=r, text=r.removeprefix("US-")) for r in region_options()])

    def save(e: Any) -> None:
        save_tax_profile(ctx.db, year, filing_status=status.value or "single", region_code=region.value or None)
        ctx.notify("Tax profile saved")

    return card(status, region, text("Estimates only; not tax advice.", ctx.pal, size=12, role="muted"),
                _save_button("Save", save, ctx, "save-tax"), pal=ctx.pal, title="Taxes", col=col, key="tax-card")


# -- platforms -------------------------------------------------------------------------


def platforms_card(ctx: ViewContext, col: dict) -> ft.Container:
    name = ft.TextField(label="Add platform (e.g. Uber, DoorDash)", expand=True)
    existing = [text(n, ctx.pal, size=13) for _, n in list_platforms(ctx.db)] or \
        [text("None yet", ctx.pal, size=13, role="secondary")]

    def add(e: Any) -> None:
        try:
            add_platform(ctx.db, name.value or "")
        except Exception as exc:
            ctx.notify(f"Couldn't add: {exc}")
            return
        ctx.refresh()

    return card(*existing, ft.Row([name, _save_button("Add", add, ctx, "add-platform")]),
                pal=ctx.pal, title="Platforms", col=col, key="platforms-card")


# -- motion sentry ---------------------------------------------------------------------


def motion_card(ctx: ViewContext, col: dict) -> ft.Container:
    s = ctx.state.settings
    bridge = ctx.services.get("motion")
    licensed = ctx.state.allows(Feature.MOTION_SENTRY)
    available = bool(bridge and bridge.available) and licensed
    enabled = ft.Switch(label="Detect trips automatically", value=s.sentry_enabled, disabled=not available,
                        key="sentry-switch")
    background = ft.Switch(label="Also when the app is closed", value=s.sentry_background,
                           disabled=not available or not s.sentry_enabled, key="sentry-bg-switch")
    shift = ft.Switch(label="I'm working now (on shift)", value=s.shift_active, key="shift-switch")
    speed = ft.Slider(min=5, max=20, divisions=15, value=s.start_speed_mph, label="{value} mph", key="sentry-speed")

    async def toggle(e: Any) -> None:
        if bridge is None:
            return
        await bridge.set_enabled(bool(enabled.value), background=bool(background.value))
        ctx.refresh()

    def save_shift(e: Any) -> None:
        ctx.state.update_settings(shift_active=bool(shift.value), start_speed_mph=int(speed.value or 10))

    enabled.on_change = toggle
    background.on_change = toggle
    shift.on_change = save_shift
    speed.on_change_end = save_shift
    note = ("Starts a trip when you're moving faster than the threshold, and asks you to confirm "
            "business vs personal. Only distance and times are saved — never your route.")
    if not available:
        note = "Automatic trip detection needs the phone app. Log trips manually here."
    extra: list[ft.Control] = []
    if not licensed:
        note = "Logging trips by hand is free on every plan."
        extra.append(locked_row(Feature.MOTION_SENTRY, ctx.pal, ctx.navigate, key="motion-locked"))
    return card(enabled, background, shift, text("Start detecting above", ctx.pal, size=13, role="secondary"), speed,
                text(note, ctx.pal, size=12, role="muted"), *extra, pal=ctx.pal, title="Trip detection", col=col,
                key="motion-card")


# -- demand radar ------------------------------------------------------------------------


def radar_card(ctx: ViewContext, col: dict) -> ft.Container:
    s = ctx.state.settings
    airport = ft.Dropdown(label="Airport", value=s.radar_airport_icao,
                          options=[ft.DropdownOption(key=a.icao, text=f"{a.iata} — {a.name}")
                                   for a in sorted(AIRPORTS.values(), key=lambda a: a.iata)])
    radius = ft.TextField(label="Radius (miles)", value=str(s.radar_radius_miles), keyboard_type=ft.KeyboardType.NUMBER)
    feed_name = ft.TextField(label="Calendar name", hint_text="Team home games")
    feed_url = ft.TextField(label="Calendar URL (ICS / webcal)")
    tm_key = ft.TextField(label="Ticketmaster API key", password=True, can_reveal_password=True,
                          value=s.ticketmaster_api_key or "")
    feeds = [ft.Row([text(f["name"], ctx.pal, size=13, role="secondary"),
                     ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Remove",
                                   on_click=lambda e, u=f["url"]: remove_feed(u))], spacing=4)
             for f in s.ics_feeds]

    def remove_feed(url: str) -> None:
        ctx.state.update_settings(ics_feeds=[f for f in ctx.state.settings.ics_feeds if f["url"] != url])
        ctx.refresh()

    def save(e: Any) -> None:
        try:
            miles = int((radius.value or "25").strip())
            if not 1 <= miles <= 100:
                raise ValueError("Radius must be 1–100 miles")
            new_feeds = list(ctx.state.settings.ics_feeds)
            if (feed_url.value or "").strip():
                url = normalize_feed_url(feed_url.value or "")
                new_feeds.append({"name": (feed_name.value or "Calendar").strip(), "url": url})
        except ValueError as exc:
            ctx.notify(str(exc))
            return
        ctx.state.update_settings(radar_airport_icao=airport.value or "KTPA", radar_radius_miles=miles,
                                  ics_feeds=new_feeds, ticketmaster_api_key=(tm_key.value or None))
        ctx.notify("Radar sources saved")
        ctx.refresh()

    return card(airport, radius, *feeds, feed_name, feed_url, tm_key,
                text("Keys are stored in the encrypted database on this device.", ctx.pal, size=12, role="muted"),
                _save_button("Save", save, ctx, "save-radar"), pal=ctx.pal, title="Demand radar", col=col,
                key="radar-card")


# -- AI service & diagnostics ---------------------------------------------------------------


def ai_card(ctx: ViewContext, col: dict) -> ft.Container:
    s, pal = ctx.state.settings, ctx.pal
    reporter = ctx.services.get("reporter")
    env_url = ctx.state.config.ai_service_url
    url = ft.TextField(label="AI server URL", value=s.ai_service_url or "", key="ai-url",
                       hint_text=env_url or "https://ai.example.com")
    token = ft.TextField(label="Access token", password=True, can_reveal_password=True, key="ai-token",
                         value=s.ai_service_token or "")
    receipts = ft.Switch(label="Scan receipts with my AI server", value=s.ai_receipts_enabled, key="ai-receipts")
    tax = ft.Switch(label="Answer tax questions with my AI server", value=s.ai_tax_enabled, key="ai-tax")
    telemetry = ft.Switch(label="Send crash reports to my server", value=s.telemetry_enabled, key="ai-telemetry")

    def save(e: Any) -> None:
        raw = (url.value or "").strip()
        try:
            clean = validate_service_url(raw, allow_http=not ctx.state.config.is_production) if raw else None
        except AIServiceError as exc:
            ctx.notify(str(exc))
            return
        wants = bool(receipts.value or tax.value or telemetry.value)
        if wants and not (clean or env_url):
            ctx.notify("Add the AI server URL before turning features on")
            return
        ctx.state.update_settings(ai_service_url=clean, ai_service_token=(token.value or "").strip() or None,
                                  ai_receipts_enabled=bool(receipts.value), ai_tax_enabled=bool(tax.value),
                                  telemetry_enabled=bool(telemetry.value))
        ctx.notify("AI settings saved")
        ctx.refresh()

    def test(e: Any) -> None:
        ctx.notify("Checking the AI server…")

        def work() -> None:
            ok, message = check_connection(ctx.state, ctx.services)
            ctx.notify(("✓ " if ok else "✗ ") + message)

        runner = ctx.services.get("run_background")
        runner(work) if runner else work()

    def send_now(e: Any) -> None:
        def work() -> None:
            try:
                sent = upload_reports(ctx.state, ctx.services)
            except Exception as exc:
                ctx.notify(f"Couldn't send reports: {exc}")
                return
            ctx.notify(f"Sent {sent} report(s)" if sent else "Nothing to send")
            ctx.refresh()

        runner = ctx.services.get("run_background")
        runner(work) if runner else work()

    def clear(e: Any) -> None:
        if reporter is not None:
            reporter.clear()
        ctx.notify("Error log cleared")
        ctx.refresh()

    diagnostics: list[ft.Control] = []
    if reporter is not None:
        pending = len(reporter.pending())
        recent = reporter.recent(3)
        diagnostics.append(text(f"Errors logged on this device: {len(reporter.recent(200))}"
                                + (f" · {pending} waiting to send" if pending and s.telemetry_enabled else ""),
                                pal, size=13, role="secondary"))
        for ev in recent:
            diagnostics.append(text(f"{ev.get('timestamp', '')[:16].replace('T', ' ')} · {ev.get('type')}: "
                                    f"{ev.get('message', '')[:120]}", pal, size=12, role="muted", selectable=True))
        diagnostics.append(ft.Row([
            ft.OutlinedButton("Send reports now", on_click=send_now, key="btn-send-reports",
                              disabled=not (s.telemetry_enabled and pending), height=ctx.layout.min_target),
            ft.TextButton("Clear log", on_click=clear, key="btn-clear-log"),
        ], wrap=True, spacing=8))

    active = service_url(ctx.state)
    if not (active or ctx.services.get("ai_expanded")):
        def expand(e: Any) -> None:
            ctx.services["ai_expanded"] = True
            ctx.refresh()

        return card(text("Receipt scanning and tax Q&A can use an AI server you run yourself. Most people don't "
                         "need this.", pal, size=13, role="secondary"),
                    ft.Row([ft.OutlinedButton("Set up a self-hosted AI server…", on_click=expand, key="btn-ai-expand",
                                              height=ctx.layout.min_target)]),
                    *diagnostics,
                    pal=pal, title="Self-hosted AI & diagnostics (advanced)", col=col, key="ai-card")
    note = ("Runs on a server you control. Receipts are read in memory and not kept; tax questions send only "
            "your yearly totals. Crash reports have paths, amounts, emails, keys and locations removed first.")
    return card(url, token, receipts, tax, telemetry,
                text(f"Using {active}" if active else "Not connected", pal, size=12, role="secondary"),
                text(note, pal, size=12, role="muted"),
                ft.Row([_save_button("Save", save, ctx, "save-ai"),
                        ft.OutlinedButton("Test connection", on_click=test, key="btn-test-ai",
                                          height=ctx.layout.min_target)], wrap=True, spacing=8),
                *diagnostics,
                pal=pal, title="Self-hosted AI & diagnostics (advanced)", col=col, key="ai-card")


# -- security ------------------------------------------------------------------------------


def security_card(ctx: ViewContext, col: dict) -> ft.Container:
    db = ctx.state.db
    protection = {
        "sqlcipher": "Encrypted with SQLCipher (AES-256)",
        "platform": "Protected by app sandbox + device encryption; sensitive fields sealed with the device key",
        "none": "NOT encrypted (development mode)",
    }.get(db.protection if db else "none")
    lines = [
        text(f"Database: {protection}", ctx.pal, size=13),
        text(f"Changes waiting to sync: {ctx.state.pending_sync_count()}", ctx.pal, size=13, role="secondary"),
    ]

    def lock(e: Any) -> None:
        lock_fn = ctx.services.get("lock")
        if lock_fn:
            lock_fn()

    return card(*lines, ft.OutlinedButton("Lock now", icon=ft.Icons.LOCK, on_click=lock,
                                          height=ctx.layout.min_target, key="btn-lock"),
                pal=ctx.pal, title="Security & sync", col=col, key="security-card")
