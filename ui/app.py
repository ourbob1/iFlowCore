"""
Root Flet application: secure startup, routing, and the adaptive shell.

    python main.py            # desktop window
    flet run main.py          # hot reload
    flet build apk|ipa|macos|windows|linux    # packaged apps (see pyproject.toml)

Startup
-------
1. Load config (env + preferences). 2. `Unlocker.plan()` decides whether the
database opens silently (env key / dev) or shows /unlock. 3. After unlock the
user lands on the route they asked for.

Shell
-----
Phones get a bottom NavigationBar; wider windows a NavigationRail (extended
on desktop). The shell re-renders on resize and theme change, so the same
code adapts from a 360 dp phone to a 4K monitor. Every view is built inside
an error boundary: a failing view shows an error card, never a blank app.

Errors
------
`main()` installs the ErrorReporter before anything else, so startup, view,
background-thread and uncaught errors all land in the local error log
(`<data_dir>/logs`). They are uploaded to the user's own server only after
they opt in (Settings → AI service & diagnostics).
"""

from __future__ import annotations

import logging
import os
import traceback
from typing import Any, Callable

import flet as ft

from core import __version__
from core.config import AppConfig, load_config, migrate_legacy_data, promote_legacy_env
from core.telemetry import ErrorReporter, ReporterInfo
from core.timeutil import utc_now
from ui import brand, router
from ui.components.common import card, status_banner, text
from ui.layout import LayoutSpec, layout_for
from ui.state import AppState, UnlockError, Unlocker
from ui.theme import Palette, palette
from ui.views import dashboard, finances, fleet, health, intelligence, pricing, radar, settings, unlock
from ui.views.base import ViewContext

log = logging.getLogger(__name__)

VIEW_BUILDERS: dict[str, Callable[[ViewContext], ft.Control]] = {
    "/": dashboard.build,
    "/finances": finances.build,
    "/fleet": fleet.build,
    "/radar": radar.build,
    "/insights": intelligence.build,
    "/plans": pricing.build,
    "/health": health.build,
    "/settings": settings.build,
    "/unlock": unlock.build,
}

SEED_COLOR = "#2a78d6"


class IFlowCoreApp:
    def __init__(self, page: Any, *, config: AppConfig | None = None, env: dict[str, str] | None = None,
                 clock: Callable = utc_now, services: dict[str, Any] | None = None) -> None:
        self.page = page
        self.config = config or load_config(env)
        self.state = AppState(self.config, clock=clock)
        self.services: dict[str, Any] = dict(services or {})
        self.unlocker = Unlocker(self.config, env=env, vault=self.services.get("web_vault"))
        self.reporter: ErrorReporter = self.services.get("reporter") or create_reporter(self.config)
        self.services["reporter"] = self.reporter
        self.current_path = router.HOME
        self._next = router.HOME            # where to go after unlocking
        self._last_tab = router.HOME        # phones: where Settings' back arrow returns to
        self._modal_stack: list[ft.DialogControl] = []    # dialogs this app opened (never SnackBars)
        self.last_error: str | None = None
        self._generation = 0                 # bumps on every render; guards re-entrant renders

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        p = self.page
        p.title = brand.NAME
        p.theme_mode = ft.ThemeMode.SYSTEM
        p.theme = ft.Theme(color_scheme_seed=SEED_COLOR)
        p.dark_theme = ft.Theme(color_scheme_seed=SEED_COLOR)
        p.on_route_change = lambda e: self.render()
        p.on_resize = lambda e: self.render()
        p.on_platform_brightness_change = lambda e: self.render()
        p.on_view_pop = lambda e: self.navigate(router.HOME)
        p.on_error = lambda e: self.reporter.capture_message(f"Flet client error: {getattr(e, 'data', e)}",
                                                             source="flet")
        self._install_services()

        plan = self.unlocker.plan()
        if plan.mode == "device_key":
            runner = getattr(self.page, "run_task", None)
            if runner:
                runner(self.unlock_with_device_key)
        elif plan.mode in ("auto_key", "plaintext_dev") and not self.unlocker.lockout_remaining():
            try:
                self._on_unlocked(self.unlocker.open(), navigate=False)
            except Exception as exc:        # fall back to the unlock screen with the reason
                self.last_error = str(exc)
        self.render()

    def _install_services(self) -> None:
        self.services.setdefault("unlocker", self.unlocker)
        self.services["on_unlocked"] = self._on_unlocked
        self.services["lock"] = self.lock
        self.services["device_unlock"] = lambda: self.page.run_task(self.unlock_with_device_key)
        self.services.setdefault("run_background", self._run_background)
        services = getattr(self.page, "services", None)
        if "file_picker" not in self.services and services is not None:
            picker = ft.FilePicker()
            services.append(picker)
            self.services["file_picker"] = picker
        if "motion" not in self.services:
            from ui.motion_bridge import MotionBridge

            self.services["motion"] = MotionBridge(self.page, self.state, notify=self.notify,
                                                   show_dialog=self.show_dialog, close_dialog=self.close_dialog,
                                                   on_trip=lambda e: self.refresh())

    async def unlock_with_device_key(self) -> bool:
        """Mobile startup: fetch (or create) the device key from platform secure
        storage, then open the database. Errors stay on the unlock screen with a
        retry button; the app never falls back to unsealed sensitive data."""
        from core.security import FletSecureKeyStore, KeyStoreError, ensure_device_key

        self.last_error = None
        try:
            store = self.services.get("keystore")
            if store is None:
                store = self.services["keystore"] = FletSecureKeyStore(self.page)
            key = await ensure_device_key(store)
            db = self.unlocker.open_with_device_key(key)
        except (KeyStoreError, UnlockError) as exc:
            self.last_error = str(exc)
            self.render()
            return False
        self.state.attach(db, device_key=key)
        del key
        self._on_unlocked(db, attached=True)
        return True

    def _run_background(self, fn: Callable[[], None]) -> None:
        runner = getattr(self.page, "run_thread", None)
        if runner:
            runner(fn)
        else:
            fn()

    def _on_unlocked(self, db: Any, *, navigate: bool = True, attached: bool = False) -> None:
        if self.services.get("web_vault") is not None:
            self._reload_preferences()          # the browser's preferences.json came out of the vault
        if not attached:
            self.state.attach(db)
        next_path = self._next
        from core.licensing import Feature

        if self.state.settings.sentry_enabled and not self.state.allows(Feature.MOTION_SENTRY):
            # License lapsed: stop starting the sentry. Recorded trips are untouched.
            self.state.update_settings(sentry_enabled=False, sentry_background=False)
            self.services["license_notice"] = ("Automatic trip detection was turned off because iFlow Pro is no "
                                               "longer active. Your trips are all still here.")
        motion = self.services.get("motion")
        if motion is not None and self.state.settings.sentry_enabled and getattr(motion, "available", False):
            runner = getattr(self.page, "run_task", None)
            if runner:
                runner(motion.set_enabled, True, background=self.state.settings.sentry_background)
        run = self.services.get("run_background", self._run_background)    # may unlock before services install
        if self.state.settings.telemetry_enabled:
            run(self._upload_reports)
        run(self._sync_health)
        run(self._update_formats)
        run(self._sync_account)
        if navigate:
            self.navigate(next_path if next_path in router.BY_PATH else router.HOME)

    def _sync_health(self) -> None:
        """Fund premiums from payouts imported since last time (iFlow Health & Guild only)."""
        from ui.viewmodels.health import sync_now

        try:
            sync_now(self.state)
        except Exception:
            log.exception("Premium withholding sync failed")

    def _sync_account(self) -> None:
        """E2E sync on unlock when this device has it turned on (quiet: the chip shows the result)."""
        from ui.viewmodels import account

        try:
            if account.status(self.state, self.services).syncing:
                account.sync_now(self.state, self.services, quiet=True)
        except Exception:
            log.exception("Sync on unlock failed")

    def _update_formats(self) -> None:
        """Daily statement-format check (signed bundle from the iFlow CDN); quiet unless something changed."""
        from ui.viewmodels.formats import check

        try:
            message = check(self.state, self.services)
        except Exception:
            log.exception("Statement-format update failed")
            return
        if message and ("new" in message or "updated" in message):
            self.services["formats_status"] = message        # shown on the Settings card

    def _upload_reports(self) -> None:
        """Opt-in upload of queued crash reports; failures stay queued quietly."""
        from ui.viewmodels.ai import upload_reports

        try:
            sent = upload_reports(self.state, self.services)
        except Exception as exc:          # warning, not error: an error would queue another report
            log.warning("Crash report upload failed: %s", type(exc).__name__)
            return
        if sent:
            log.info("Uploaded %d crash report(s)", sent)

    def _reload_preferences(self) -> None:
        from dataclasses import replace

        from core.config import load_preferences

        self.config = replace(self.config, preferences=load_preferences(self.config.preferences_path))
        self.state.config = self.config

    def lock(self) -> None:
        self.state.lock()
        vault = self.services.get("web_vault")
        if vault is not None:
            vault.lock()                        # save, forget the key, wipe the in-memory copy
        self.navigate(router.UNLOCK)
        if self.unlocker.mobile:          # phones re-open from secure storage (biometric gate: later phase)
            runner = getattr(self.page, "run_task", None)
            if runner:
                runner(self.unlock_with_device_key)

    # -- navigation ----------------------------------------------------------

    def navigate(self, route: str) -> None:
        self.page.route = route
        self.render()

    def refresh(self) -> None:
        self.render()

    def notify(self, message: str) -> None:
        self.show_dialog(ft.SnackBar(ft.Text(message)))

    def show_dialog(self, dialog: ft.DialogControl) -> None:
        if not isinstance(dialog, ft.SnackBar):
            self._modal_stack.append(dialog)
        self.page.show_dialog(dialog)

    def close_dialog(self) -> None:
        """Close the most recent dialog *we* opened. Not Flet's pop_dialog(): that closes the topmost
        open overlay, which is often a SnackBar still counted as open, leaving the real dialog up."""
        while self._modal_stack:
            dialog = self._modal_stack.pop()
            if not getattr(dialog, "open", False):
                continue                        # already dismissed by the user (tap outside / back)
            dialog.open = False
            try:
                dialog.update()
            except Exception:                   # not mounted (tests) or page gone
                pass
            closed = getattr(self.page, "closed_dialog", None)      # test double hook
            if closed:
                closed(dialog)
            return

    def _license_banners(self, spec: router.RouteSpec, pal: Palette, pad: ft.Padding) -> list[ft.Control]:
        from core.licensing import Status

        out: list[ft.Control] = []
        notice = self.services.get("license_notice")
        if notice:
            def dismiss(e: Any) -> None:
                self.services.pop("license_notice", None)
                self.refresh()

            out.append(ft.Container(status_banner(
                "warning", "Plan changed", notice, pal, key="license-notice",
                trailing=ft.IconButton(ft.Icons.CLOSE, tooltip="Dismiss", on_click=dismiss)), padding=pad))
        lic = self.state.license
        if lic.status is Status.GRACE and spec.path != "/plans":
            out.append(ft.Container(status_banner(
                "warning", f"Renew {lic.tier.label}",
                f"Your license expired; features stay on for {lic.days_left} more day(s). Nothing will be deleted.",
                pal, key="license-grace",
                trailing=ft.TextButton("Plans", on_click=lambda e: self.navigate("/plans"))), padding=pad))
        return out

    def _back_target(self) -> str:
        spec = router.BY_PATH.get(self.current_path)
        if spec is not None and spec.nav_parent:
            return spec.nav_parent
        return self._last_tab if self._last_tab != self.current_path else router.HOME

    # -- rendering -----------------------------------------------------------

    @property
    def layout(self) -> LayoutSpec:
        return layout_for(self.page.width, str(getattr(self.page.platform, "value", self.page.platform) or ""))

    @property
    def pal(self) -> Palette:
        mode = self.page.theme_mode
        if mode == ft.ThemeMode.DARK:
            return palette("dark")
        if mode == ft.ThemeMode.LIGHT:
            return palette("light")
        return palette("dark" if self.page.platform_brightness == ft.Brightness.DARK else "light")

    def render(self) -> None:
        self._generation += 1
        generation = self._generation
        res = router.resolve(self.page.route, unlocked=self.state.unlocked)
        if res.spec.path == router.UNLOCK and "next" in res.params:
            self._next = res.params["next"]
        if res.redirect_to:
            self.page.route = res.redirect_to
        if self.current_path in {r.path for r in router.COMPACT_NAV_ROUTES}:
            self._last_tab = self.current_path
        self.current_path = res.spec.path
        self.reporter.context["route"] = res.spec.path
        layout, pal = self.layout, self.pal
        self.services["last_error"] = self.last_error
        ctx = ViewContext(self.state, layout, pal, self.navigate, self.refresh, self.notify, self.show_dialog,
                          self.close_dialog, params=res.params, services=self.services)
        body = self.build_view(res.spec.path, ctx)
        if generation != self._generation:
            return          # a newer render (e.g. triggered by a finished background refresh) already won
        self._present(self.shell(res.spec, body, layout, pal))
        self.page.update()

    _VIEW_FIELDS = ("route", "appbar", "navigation_bar", "controls", "bgcolor", "padding")

    def _present(self, view: ft.View) -> None:
        """Show `view` by updating the one View already on screen instead of replacing it.
        Flutter shows dialogs as routes on the navigator; swapping the navigator's page while a
        dialog is closing strands the dialog on screen (e.g. close → notify → refresh).

        Each render's content goes in a wrapper keyed by the render generation. Views are rebuilt
        from scratch, and Flet reconciles list items that share an explicit `key` with their
        predecessors in *frozen* mode: the rebuilt controls become immutable, so a handler that
        later sets e.g. `error.value` raises "Frozen controls cannot be updated". A fresh key per
        render makes Flet replace the old subtree and mount the new one as ordinary controls."""
        view.controls = [ft.Container(c, expand=getattr(c, "expand", None), key=f"render-{self._generation}-{i}")
                         for i, c in enumerate(view.controls)]
        current = self.page.views[-1] if self.page.views else None
        if isinstance(current, ft.View) and len(self.page.views) == 1:
            for name in self._VIEW_FIELDS:
                setattr(current, name, getattr(view, name))
            return
        self.page.views.clear()
        self.page.views.append(view)

    def build_view(self, path: str, ctx: ViewContext) -> ft.Control:
        try:
            return VIEW_BUILDERS[path](ctx)
        except Exception as exc:          # error boundary
            log.exception("View %s failed", path)
            self.last_error = f"{type(exc).__name__}: {exc}"
            return ft.Container(card(
                status_banner("critical", "This screen hit a problem", str(exc) or type(exc).__name__, ctx.pal),
                text("Your data is safe. Try again, or check Settings.", ctx.pal, size=13, role="secondary"),
                ft.TextButton("Go to dashboard", on_click=lambda e: self.navigate(router.HOME)),
                pal=ctx.pal), padding=ft.Padding.all(ctx.layout.padding), key="view-error",
                data=traceback.format_exc())

    def shell(self, spec: router.RouteSpec, body: ft.Control, layout: LayoutSpec, pal: Palette) -> ft.View:
        if spec.path == router.UNLOCK or not self.state.unlocked:
            return ft.View(route=spec.path, controls=[body], bgcolor=pal.surface, padding=0)

        compact = layout.nav == "bar"
        index = router.nav_index(spec.path, compact=compact)
        if index is None and spec.nav_parent:
            index = router.nav_index(spec.nav_parent, compact=compact)
        selected = 0 if index is None else index
        from ui.viewmodels.account import chip

        sync_label, sync_tip = chip(self.state, self.services)
        actions: list[ft.Control] = [
            ft.Container(text(sync_label, pal, size=12, role="secondary"), tooltip=sync_tip,
                         padding=ft.Padding.symmetric(horizontal=12), key="sync-chip",
                         on_click=lambda e: self.navigate(router.SETTINGS)),
        ]
        if compact and spec.path != "/health":
            actions.append(ft.IconButton(ft.Icons.FAVORITE_BORDER, tooltip="iFlow Health", key="appbar-health",
                                         on_click=lambda e: self.navigate("/health")))
        if compact and spec.path != router.SETTINGS:
            actions.append(ft.IconButton(ft.Icons.SETTINGS_OUTLINED, tooltip="Settings", key="appbar-settings",
                                         on_click=lambda e: self.navigate(router.SETTINGS)))
        banners: list[ft.Control] = []
        pad = ft.Padding.only(left=layout.padding, right=layout.padding, top=layout.padding)
        if self.state.banner:
            banners.append(ft.Container(status_banner("warning", "Not encrypted", self.state.banner, pal), padding=pad))
        banners += self._license_banners(spec, pal, pad)

        leading = None
        if compact and index is None:          # a page outside the bottom bar (Settings): back arrow, no bar
            leading = ft.IconButton(ft.Icons.ARROW_BACK, tooltip="Back", key="appbar-back",
                                    on_click=lambda e: self.navigate(self._back_target()))
        appbar = ft.AppBar(title=ft.Text(spec.title), actions=actions, bgcolor=pal.surface, leading=leading,
                           automatically_imply_leading=False)
        content = ft.Column([*banners, ft.Container(body, expand=True)], expand=True, spacing=0)

        def on_nav(e: Any) -> None:
            self.navigate(router.path_for_index(int(e.control.selected_index), compact=compact))

        if compact:
            if index is None:
                return ft.View(route=spec.path, appbar=appbar, controls=[content], bgcolor=pal.surface, padding=0)
            nav_bar = ft.NavigationBar(
                selected_index=selected, on_change=on_nav, key="nav-bar",
                destinations=[ft.NavigationBarDestination(icon=getattr(ft.Icons, r.icon),
                                                          selected_icon=getattr(ft.Icons, r.selected_icon),
                                                          label=r.title) for r in router.nav_routes(compact=True)],
            )
            return ft.View(route=spec.path, appbar=appbar, navigation_bar=nav_bar, controls=[content],
                           bgcolor=pal.surface, padding=0)

        rail = ft.NavigationRail(
            selected_index=selected, on_change=on_nav, extended=layout.nav == "rail_extended",
            label_type=ft.NavigationRailLabelType.ALL if layout.nav == "rail" else None,
            min_width=72, min_extended_width=200, key="nav-rail",
            leading=ft.Container(
                ft.Row([brand.emblem(36), *([ft.Text(brand.NAME, size=16, weight=ft.FontWeight.W_700,
                                                     color=pal.text_primary)] if layout.nav == "rail_extended" else [])],
                       spacing=10, tight=True),
                padding=ft.Padding.only(top=12, bottom=8)),
            destinations=[ft.NavigationRailDestination(icon=getattr(ft.Icons, r.icon),
                                                       selected_icon=getattr(ft.Icons, r.selected_icon),
                                                       label=r.title) for r in router.NAV_ROUTES],
        )
        return ft.View(route=spec.path, appbar=appbar, bgcolor=pal.surface, padding=0, controls=[
            ft.Row([rail, ft.VerticalDivider(width=1), content], expand=True, spacing=0,
                   vertical_alignment=ft.CrossAxisAlignment.STRETCH),
        ])


def create_reporter(config: AppConfig) -> ErrorReporter:
    info = ReporterInfo(release=__version__, environment=config.environment.value, platform=config.platform)
    return ErrorReporter(config.data_dir / "logs", info, extra_scrub_paths=[config.data_dir, config.db_path])


def main(page: ft.Page) -> None:
    logging.basicConfig(level=logging.INFO)
    promote_legacy_env(os.environ)
    migrate_legacy_data()           # pre-rebrand install: RubySoho data dir / rubysoho.db → iFlowCore
    config = load_config()          # a bad config fails before there is a data dir to log into
    reporter = create_reporter(config).install()
    IFlowCoreApp(page, config=config, services={"reporter": reporter}).start()


async def main_web(page: ft.Page) -> None:
    """Browser build (Pyodide): open the encrypted vault in the browser's private storage first."""
    import asyncio

    from core.web.session import WebVaultSession
    from core.web.vault import OpfsStorage, Vault, VaultError

    logging.basicConfig(level=logging.INFO)
    config = load_config()
    reporter = create_reporter(config).install()
    try:
        storage = await OpfsStorage.open()
    except VaultError as exc:
        page.title = brand.NAME
        page.add(ft.Container(ft.Column([brand.logo(palette("light"), width=260),
                                         ft.Text(str(exc), size=16, text_align=ft.TextAlign.CENTER)],
                                        horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=20),
                              alignment=ft.Alignment.CENTER, expand=True, padding=24))
        page.update()
        return
    session = WebVaultSession(Vault(storage), config.db_path, config.preferences_path)
    IFlowCoreApp(page, config=config, services={"reporter": reporter, "web_vault": session}).start()

    async def autosave() -> None:           # catches writes outside transactions (e.g. preferences)
        while True:
            await asyncio.sleep(3)
            try:
                session.save_if_changed()
            except Exception:
                logging.getLogger(__name__).exception("vault autosave failed")

    page.run_task(autosave)


def run() -> None:
    from core.fleet.http import in_browser

    if in_browser():
        ft.run(main_web)                    # assets are served next to index.html by `flet publish`
    else:
        ft.run(main, assets_dir=str(brand.ASSETS_DIR))      # absolute: works from source and in bundles
