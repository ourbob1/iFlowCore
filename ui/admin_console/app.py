"""
iFlow Core Admin Console: a separate desktop app for whoever runs the iFlow Core service.

    iFlow Core Admin Console (app menu)      # packaged: scripts/package_desktop.py --app console
    python admin_console.py                  # from source
    IFLOW_ADMIN_URL=https://ai.example.com python admin_console.py     # preselect a remote server

"This computer" (the default) runs the admin service inside the console on 127.0.0.1
(ui/admin_console/local_service.py): no terminal, no environment variables. "Another server" signs
in to a deployed service over HTTPS.

Pages
-----
  Set up            first run on this computer: create the owner account (no accounts exist yet)
  Sign in           this computer or another server + username + password (or a service token)
  Choose password   forced after a temporary password (first sign-in or an admin reset)
  Console           a navigation rail with only the pages your role allows:
                    Overview · Users · Licenses & revenue · Parser Studio · Crash reports ·
                    System health · Audit trail · Advertisers · My account

Data sovereignty
----------------
The console never opens an iFlow Core user database: there's no AppState, no vault, no key. It
talks only to the service's /admin/v1 API. The session token lives in memory and is dropped on
sign-out, on window close, or when the server ends the session (30 min idle, 12 h absolute).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

import flet as ft

from core import __version__
from core.admin.client import AdminError
from ui import brand
from ui.components.common import card, notice_banner, text
from ui.layout import layout_for
from ui.theme import palette
from ui.viewmodels import admin as vm
from ui.views import admin as sections
from ui.views.admin_parser_studio import studio_page
from ui.views.admin_users import PERMISSION_LABELS, users_page
from ui.views.admin_advertisers import advertisers_page
from ui.views.base import ViewContext
from ui.admin_console.local_service import LocalService, LocalServiceError, PublishingSettings

TITLE = f"{brand.NAME} Admin Console"
SEED_COLOR = "#2a78d6"
MIN_WIDTH = 900                                  # desktop console; below this it asks for a wider window


@dataclass(frozen=True)
class Page:
    id: str
    label: str
    icon: str
    permission: str | None
    sections: tuple[str, ...]


PAGES: tuple[Page, ...] = (
    Page("overview", "Overview", "DASHBOARD_OUTLINED", None, ("overview", "health", "audit")),
    Page("users", "Users", "GROUP_OUTLINED", "users.manage", ("users",)),
    Page("licenses", "Licenses & revenue", "KEY_OUTLINED", "licenses.read", ("licenses", "revenue")),
    Page("parsers", "Parser Studio", "DATA_OBJECT", "parsers.edit", ("parsers",)),
    Page("advertisers", "Advertisers", "CAMPAIGN", "advertisers.manage", ("advertisers",)),
    Page("telemetry", "Crash reports", "BUG_REPORT_OUTLINED", "telemetry.read", ("telemetry",)),
    Page("health", "System health", "MONITOR_HEART_OUTLINED", "health.read", ("health",)),
    Page("audit", "Audit trail", "HISTORY", "audit.read", ("audit",)),
    Page("account", "My account", "ACCOUNT_CIRCLE_OUTLINED", None, ("account",)),
)
# what each section loads, and what it needs to be shown
SECTION_DATA = {"overview": ("overview",), "health": ("health",), "audit": ("audit",), "users": ("users",),
                "licenses": ("plans", "licenses"), "revenue": ("revenue",), "parsers": ("parsers", "integrations"),
                "advertisers": ("advertisers",), "telemetry": ("issues",), "account": ()}
SECTION_PERMISSION = {"health": "health.read", "audit": "audit.read", "users": "users.manage",
                      "licenses": "licenses.read", "revenue": "licenses.read", "parsers": "parsers.edit",
                      "advertisers": "advertisers.manage", "telemetry": "telemetry.read"}


def visible_pages(s: vm.AdminSession) -> list[Page]:
    return [p for p in PAGES if s.can(p.permission)]


class AdminConsoleApp:
    def __init__(self, page: Any, *, services: dict[str, Any] | None = None,
                 env: dict[str, str] | None = None) -> None:
        self.page = page
        self.env = dict(os.environ if env is None else env)
        self.services: dict[str, Any] = dict(services or {})
        self.current = "overview"
        self._modal_stack: list[ft.DialogControl] = []
        self._generation = 0
        self.slots: dict[str, ft.Container] = {}
        self.local: LocalService = self.services.get("local_service") or LocalService.shared()
        self.services.setdefault("console_server", "remote" if self.env.get("IFLOW_ADMIN_URL") else "local")

    # -- lifecycle -----------------------------------------------------------------------------------------

    def start(self) -> None:
        p = self.page
        p.title = TITLE
        p.theme_mode = ft.ThemeMode.SYSTEM
        p.theme = ft.Theme(color_scheme_seed=SEED_COLOR)
        p.dark_theme = ft.Theme(color_scheme_seed=SEED_COLOR)
        p.on_resize = lambda e: self.render()
        p.on_platform_brightness_change = lambda e: self.render()
        p.on_close = lambda e: self.shutdown()
        services = getattr(p, "services", None)
        if "file_picker" not in self.services and services is not None:
            picker = ft.FilePicker()                     # Parser Studio: load a sample statement
            services.append(picker)
            self.services["file_picker"] = picker
        if self.services["console_server"] == "local":
            self.start_local()
        self.render()

    def shutdown(self) -> None:
        self.sign_out(render=False)
        if self.services.get("local_service") is not None or not getattr(self.page, "web", False):
            self.local.stop()                           # desktop: the window is the app; web: others may share it

    @property
    def local_mode(self) -> bool:
        return self.services.get("console_server") == "local"

    def start_local(self) -> None:
        """Start the built-in service in the background; the sign-in page shows its state."""
        if self.services.get("local_status") in ("starting", "ready"):
            return
        self.services["local_status"] = "starting"

        def work() -> None:
            try:
                self.local.start()
                self.services["local_status"] = "ready"
            except (LocalServiceError, OSError, ImportError) as exc:
                self.services["local_status"] = f"error:{exc}"
            self.render()

        runner = getattr(self.page, "run_thread", None)
        runner(work) if runner else work()

    @property
    def session(self) -> vm.AdminSession | None:
        return vm.session(self.services)

    @property
    def pal(self):
        mode = self.page.theme_mode
        if mode == ft.ThemeMode.DARK:
            return palette("dark")
        if mode == ft.ThemeMode.LIGHT:
            return palette("light")
        return palette("dark" if self.page.platform_brightness == ft.Brightness.DARK else "light")

    def context(self) -> ViewContext:
        layout = layout_for(self.page.width, str(getattr(self.page.platform, "value", self.page.platform) or ""))
        return ViewContext(None, layout, self.pal, self.go, self.render, self.notify,  # type: ignore[arg-type]
                           self.show_dialog, self.close_dialog, services=self.services)

    # -- actions -------------------------------------------------------------------------------------------

    def go(self, page_id: str) -> None:
        s = self.session
        if s is not None and any(p.id == page_id for p in visible_pages(s)):
            self.current = page_id
            vm.begin(s)
            vm.load(s, self._data_for(page_id))
        self.render()

    def reload(self) -> None:
        s = self.session
        if s is not None:
            vm.begin(s)
            vm.load(s, self._data_for(self.current))
        self.render()

    def sign_out(self, *, render: bool = True, message: str | None = None) -> None:
        vm.sign_out(self.services)
        self.current = "overview"
        if message:
            self.services["console_login_error"] = message
        if render:
            self.render()

    def _data_for(self, page_id: str) -> tuple[str, ...]:
        page = next(p for p in PAGES if p.id == page_id)
        return tuple(d for name in page.sections for d in SECTION_DATA[name])

    def notify(self, message: str) -> None:
        self.show_dialog(ft.SnackBar(ft.Text(message)))

    def show_dialog(self, dialog: ft.DialogControl) -> None:
        if not isinstance(dialog, ft.SnackBar):
            self._modal_stack.append(dialog)
        self.page.show_dialog(dialog)

    def close_dialog(self) -> None:
        while self._modal_stack:
            dialog = self._modal_stack.pop()
            if not getattr(dialog, "open", False):
                continue
            dialog.open = False
            try:
                dialog.update()
            except Exception:
                pass
            closed = getattr(self.page, "closed_dialog", None)      # test double hook
            if closed:
                closed(dialog)
            return

    # -- rendering -----------------------------------------------------------------------------------------

    def render(self) -> None:
        self._generation += 1
        s = self.session
        if s is not None and s.expired:
            self.sign_out(render=False, message="Your session has ended. Sign in again.")
            s = None
        ctx = self.context()
        if s is None and self.local_mode and self.services.get("local_status") == "ready" and self.local.needs_setup():
            body, route = self.setup_page(ctx), "/setup"
        elif s is None:
            body, route = self.login_page(ctx), "/login"
        elif s.must_change_password:
            body, route = self.password_page(ctx, s), "/password"
        elif ctx.layout.width < MIN_WIDTH:
            body, route = self._too_narrow(ctx), "/narrow"
        else:
            body, route = self.shell(ctx, s), f"/{self.current}"
        view = ft.View(route=route, controls=[ft.Container(body, expand=True, key=f"render-{self._generation}")],
                       bgcolor=ctx.pal.surface, padding=0)
        current = self.page.views[-1] if self.page.views else None
        if isinstance(current, ft.View) and len(self.page.views) == 1:
            for name in ("route", "controls", "bgcolor", "padding"):
                setattr(current, name, getattr(view, name))
        else:
            self.page.views.clear()
            self.page.views.append(view)
        self.page.update()

    def _centered(self, ctx: ViewContext, *controls: ft.Control, key: str) -> ft.Control:
        column = ft.Column([ft.Container(ft.Column(list(controls), spacing=16, tight=True,
                                                   horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                                         width=460, padding=ft.Padding.symmetric(vertical=48, horizontal=16))],
                           horizontal_alignment=ft.CrossAxisAlignment.CENTER, scroll=ft.ScrollMode.AUTO,
                           expand=True, key=key)
        # in a Row, expand means full width: the card centers horizontally at any window size
        return ft.Row([column], expand=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH)

    def _too_narrow(self, ctx: ViewContext) -> ft.Control:
        return self._centered(ctx, notice_banner(
            "warning", "Widen the window", f"The Admin Console is a desktop app; make the window at least "
            f"{MIN_WIDTH} px wide.", ctx.pal, key="console-too-narrow"),
            ft.TextButton("Sign out", on_click=lambda e: self.sign_out(), key="console-sign-out"),
            key="console-narrow")

    def _brand_header(self, ctx: ViewContext, subtitle: str) -> ft.Control:
        return ft.Column([
            ft.Row([brand.emblem(40, key="console-emblem"),
                    ft.Column([text(TITLE, ctx.pal, size=20, weight=ft.FontWeight.W_700),
                               text(subtitle, ctx.pal, size=13, role="secondary")], spacing=2, tight=True)],
                   spacing=12),
        ], tight=True)

    def login_page(self, ctx: ViewContext) -> ft.Control:
        pal = ctx.pal
        local = self.local_mode
        default_url = self.services.get("admin_last_url") or self.env.get("IFLOW_ADMIN_URL") or ""
        use_token = bool(self.services.get("console_use_token"))
        where = ft.SegmentedButton(
            segments=[ft.Segment(value="local", label=ft.Text("This computer"), icon=ft.Icon(ft.Icons.COMPUTER)),
                      ft.Segment(value="remote", label=ft.Text("Another server"), icon=ft.Icon(ft.Icons.PUBLIC))],
            selected=["local" if local else "remote"], allow_empty_selection=False, key="console-where",
            on_change=lambda e: self.choose_server(list(e.control.selected)[0]))
        url = ft.TextField(label="Server address", value=default_url, key="console-url", visible=not local,
                           hint_text="https://admin.iflowcore.com")
        username = ft.TextField(label="Username", value=self.services.get("console_last_user", ""), autofocus=True,
                                key="console-username", visible=not use_token)
        password = ft.TextField(label="Password", password=True, can_reveal_password=True, key="console-password",
                                visible=not use_token)
        token = ft.TextField(label="Service token", password=True, can_reveal_password=True, key="console-token",
                             visible=use_token)
        error = self.services.pop("console_login_error", None)
        status = self.services.get("local_status", "")
        ready = not local or status == "ready"

        def submit(e: Any) -> None:
            if not ready:
                return
            address = self.local.url if local else (url.value or "")
            if not local:
                self.services["admin_last_url"] = url.value
            transport = self.services.get("admin_transport")
            try:
                if use_token:
                    vm.sign_in(self.services, address, token.value or "", transport=transport)
                else:
                    self.services["console_last_user"] = (username.value or "").strip()
                    vm.sign_in_password(self.services, address, username.value or "", password.value or "",
                                        transport=transport)
                self.current = "overview"
            except AdminError as exc:
                self.services["console_login_error"] = exc.message
            self.render()

        def toggle_mode(e: Any) -> None:
            self.services["console_use_token"] = not use_token
            self.services["admin_last_url"] = url.value
            self.render()

        local_line: list[ft.Control] = []
        if local:
            if status == "ready":
                local_line.append(ft.Row([ft.Icon(ft.Icons.CHECK_CIRCLE_OUTLINE, size=16, color=pal.text_secondary),
                                          text("Built-in service is running on this computer", pal, size=12,
                                               role="secondary")], spacing=6, key="console-local-ready"))
            elif status.startswith("error:"):
                local_line.append(notice_banner("serious", "The built-in service didn't start", status[6:], pal,
                                                key="console-local-error",
                                                trailing=ft.TextButton("Try again", key="console-local-retry",
                                                                       on_click=lambda e: self.retry_local())))
            else:
                local_line.append(ft.Row([ft.ProgressRing(width=14, height=14, stroke_width=2),
                                          text("Starting the built-in service…", pal, size=12, role="secondary")],
                                         spacing=8, key="console-local-starting"))
        password.on_submit = token.on_submit = submit
        return self._centered(
            ctx,
            self._brand_header(ctx, "Operators only"),
            card(
                *([notice_banner("serious", "Can't sign in", error, pal, key="console-login-error")] if error else []),
                where, *local_line,
                *(_wide(f) for f in (url, username, password, token)),
                ft.FilledButton("Sign in", icon=ft.Icons.LOGIN, on_click=submit, key="console-sign-in",
                                height=44, disabled=not ready),
                ft.TextButton("Sign in with a username instead" if use_token else "Use a service token (automation)",
                              on_click=toggle_mode, key="console-toggle-mode"),
                pal=pal, title="Sign in", key="console-login"),
            text("Authorized personnel only. Every action is recorded in the audit trail. Sessions end after "
                 "30 minutes idle or 12 hours.", pal, size=12, role="muted"),
            text(f"v{__version__}", pal, size=11, role="muted"),
            key="console-login-page")

    def choose_server(self, where: str) -> None:
        self.services["console_server"] = where
        if where == "local":
            self.start_local()
        self.render()

    def retry_local(self) -> None:
        self.services.pop("local_status", None)
        self.start_local()
        self.render()

    def setup_page(self, ctx: ViewContext) -> ft.Control:
        """First run on this computer: no console accounts exist yet, so create the owner."""
        pal = ctx.pal
        username = ft.TextField(label="Username", key="setup-username", autofocus=True, hint_text="e.g. jdoe")
        name = ft.TextField(label="Your name", key="setup-name")
        password = ft.TextField(label="Password", password=True, can_reveal_password=True, key="setup-password",
                                helper="12+ characters, not your username or a common word")
        confirm = ft.TextField(label="Confirm password", password=True, can_reveal_password=True, key="setup-confirm")
        error = self.services.pop("console_setup_error", None)

        def submit(e: Any) -> None:
            try:
                self.local.create_first_owner((username.value or "").strip(), (name.value or "").strip(),
                                              password.value or "", confirm.value or "")
                vm.sign_in_password(self.services, self.local.url, username.value or "", password.value or "",
                                    transport=self.services.get("admin_transport"))
                s = vm.session(self.services)
                if s is not None:
                    s.notice = "Welcome. Your owner account is ready on this computer."
            except (LocalServiceError, AdminError) as exc:
                self.services["console_setup_error"] = getattr(exc, "message", str(exc))
            self.render()

        confirm.on_submit = submit
        return self._centered(
            ctx,
            self._brand_header(ctx, "Set up this computer"),
            card(
                text("There are no console accounts on this computer yet. Create the owner account; you can add "
                     "administrators, developers and support staff afterwards.", pal, size=13, role="secondary"),
                *([notice_banner("serious", "Not created", error, pal, key="setup-error")] if error else []),
                *(_wide(f) for f in (username, name, password, confirm)),
                ft.FilledButton("Create owner account", icon=ft.Icons.PERSON_ADD_ALT, on_click=submit,
                                key="setup-submit", height=44),
                pal=pal, title="Create the owner account", key="console-setup"),
            text("Everything stays on this computer: accounts, the audit trail and the signing key that apps trust "
                 f"(in {self.local.data_dir}). Back that folder up.", pal, size=12, role="muted"),
            key="console-setup-page")

    def password_page(self, ctx: ViewContext, s: vm.AdminSession) -> ft.Control:
        pal = ctx.pal
        current = ft.TextField(label="Temporary password", password=True, can_reveal_password=True,
                               key="console-pw-current")
        new = ft.TextField(label="New password", password=True, can_reveal_password=True, key="console-pw-new",
                           helper="12+ characters, not your username or a common word")
        confirm = ft.TextField(label="Confirm new password", password=True, can_reveal_password=True,
                               key="console-pw-confirm")
        error = self.services.pop("console_pw_error", None)

        def submit(e: Any) -> None:
            try:
                vm.change_password(s, current.value or "", new.value or "", confirm.value or "")
                s.notice = "Password changed. Welcome to the Admin Console."
            except AdminError as exc:
                self.services["console_pw_error"] = exc.message
            self.render()

        confirm.on_submit = submit
        who = s.user.get("display_name") or s.user.get("username") or s.actor
        return self._centered(
            ctx,
            self._brand_header(ctx, f"Signed in as {who}"),
            card(
                text("You're using a temporary password. Choose your own to continue; nothing else is available "
                     "until you do.", pal, size=13, role="secondary"),
                *([notice_banner("serious", "Not changed", error, pal, key="console-pw-error")] if error else []),
                *(_wide(f) for f in (current, new, confirm)),
                ft.FilledButton("Set password", icon=ft.Icons.LOCK_RESET, on_click=submit, key="console-pw-submit",
                                height=44),
                ft.TextButton("Sign out", on_click=lambda e: self.sign_out(), key="console-sign-out"),
                pal=pal, title="Choose your password", key="console-password-change"),
            key="console-password-page")

    # -- the console -----------------------------------------------------------------------------------------

    def shell(self, ctx: ViewContext, s: vm.AdminSession) -> ft.Control:
        pal = ctx.pal
        pages = visible_pages(s)
        if self.current not in {p.id for p in pages}:
            self.current = pages[0].id
        page = next(p for p in pages if p.id == self.current)
        builders: dict[str, Callable[[ViewContext, vm.AdminSession, Any], ft.Control]] = {
            "notice": sections.notice_banner, "overview": sections.overview_row, "health": sections.health_card,
            "audit": sections.audit_card, "licenses": sections.license_card, "revenue": sections.revenue_card,
            "telemetry": sections.telemetry_card, "users": users_page, "parsers": studio_page,
            "advertisers": advertisers_page, "account": self.account_section,
        }
        names = ["notice"] + [n for n in page.sections if s.can(SECTION_PERMISSION.get(n))]
        self.slots = {}

        def redraw(*changed: str) -> None:
            """Rebuild only these sections, in place: the page keeps its scroll position."""
            if s.expired:
                self.render()
                return
            order = [n for n in dict.fromkeys(("notice", *changed)) if n in self.slots]
            for name in order:
                self.slots[name].content = builders[name](ctx, s, redraw)
            try:
                for name in order:
                    self.slots[name].update()
            except RuntimeError:                   # not mounted (a test page): rebuild everything
                self.render()

        for name in names:
            self.slots[name] = ft.Container(builders[name](ctx, s, redraw), key=f"console-slot-{name}")

        extended = ctx.layout.width >= 1100
        rail = ft.NavigationRail(
            selected_index=pages.index(page), extended=extended, min_extended_width=220,
            label_type=None if extended else ft.NavigationRailLabelType.ALL, bgcolor=pal.surface_raised,
            leading=ft.Container(ft.Row([brand.emblem(28, key=None)] + (
                [text("Admin Console", pal, size=15, weight=ft.FontWeight.W_700)] if extended else []), spacing=10),
                padding=ft.Padding.symmetric(vertical=12)),
            destinations=[ft.NavigationRailDestination(icon=getattr(ft.Icons, p.icon), label=p.label, key=f"nav-{p.id}")
                          for p in pages],
            on_change=lambda e: self.go(pages[int(e.control.selected_index)].id), key="console-rail")
        who = s.user.get("display_name") or s.user.get("username") or s.actor
        role = (s.user.get("role") or "service token").replace("admin", "administrator").title()
        topbar = ft.Container(ft.Row([
            text(page.label, pal, size=20, weight=ft.FontWeight.W_700),
            ft.Container(expand=True),
            text(self.server_label(s), pal, size=12, role="muted"),
            ft.Container(ft.Row([ft.Icon(ft.Icons.PERSON_OUTLINE, size=16, color=pal.text_secondary),
                                 text(f"{who} · {role}", pal, size=13, role="secondary")], spacing=6),
                         padding=ft.Padding.symmetric(horizontal=12), key="console-user-chip"),
            ft.IconButton(ft.Icons.REFRESH, tooltip="Refresh", on_click=lambda e: self.reload(), key="console-refresh"),
            ft.TextButton("Sign out", icon=ft.Icons.LOGOUT, on_click=lambda e: self.sign_out(),
                          key="console-sign-out"),
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER), padding=ft.Padding.symmetric(horizontal=24, vertical=10),
            border=ft.Border.only(bottom=ft.BorderSide(1, pal.grid)), key="console-topbar")
        content = ft.Column([ft.Container(ft.Column(list(self.slots.values()), spacing=16, tight=True),
                                          padding=ft.Padding.all(24))],
                            scroll=ft.ScrollMode.AUTO, expand=True, key=f"console-page-{page.id}")
        return ft.Row([rail, ft.VerticalDivider(width=1, color=pal.grid),
                       ft.Column([topbar, content], expand=True, spacing=0)], expand=True, spacing=0,
                      vertical_alignment=ft.CrossAxisAlignment.STRETCH, key="console-shell")

    def account_section(self, ctx: ViewContext, s: vm.AdminSession, redraw: Any) -> ft.Control:
        pal = ctx.pal
        u = s.user
        profile: list[ft.Control] = []
        for label, value in (("Username", u.get("username") or s.actor), ("Name", u.get("display_name") or "—"),
                             ("Role", u.get("role_label") or "Service token (owner)"),
                             ("Last sign-in", vm.short_date(u["last_login_at"]) if u.get("last_login_at") else "—"),
                             ("Server", self.server_label(s))):
            profile.append(ft.Row([ft.Container(text(label, pal, size=13, weight=ft.FontWeight.W_600), width=140),
                                   text(value, pal, size=13, role="secondary", selectable=True)]))
        profile.append(text("You can: " + ", ".join(PERMISSION_LABELS.get(p, p).lower() for p in sorted(s.permissions)),
                            pal, size=13, role="secondary"))
        cards = [card(*profile, pal=pal, title="Profile", key="account-profile")]
        if s.user.get("id"):
            current = ft.TextField(label="Current password", password=True, can_reveal_password=True, width=320,
                                   key="account-pw-current")
            new = ft.TextField(label="New password", password=True, can_reveal_password=True, width=320,
                               key="account-pw-new", helper="12+ characters, not your username")
            confirm = ft.TextField(label="Confirm", password=True, can_reveal_password=True, width=320,
                                   key="account-pw-confirm")
            err = s.errors.get("action")

            def change(e: Any) -> None:
                sections.begin(s)
                try:
                    vm.change_password(s, current.value or "", new.value or "", confirm.value or "")
                    s.notice = "Password changed. Other sessions were signed out."
                except AdminError as exc:
                    s.errors["action"] = exc.message
                redraw("account")

            cards.append(card(
                *([notice_banner("serious", "Not changed", err, pal, key="account-pw-error")] if err else []),
                current, new, confirm,
                ft.Row([ft.FilledButton("Change password", on_click=change, key="account-pw-submit")]),
                pal=pal, title="Password", key="account-password"))
        if self.is_local(s) and s.can("parsers.publish"):
            cards += [self.signing_card(ctx), self.publishing_card(ctx, s, redraw)]
        return ft.Column(cards, spacing=16, key="console-account")

    def is_local(self, s: vm.AdminSession) -> bool:
        return s.client.base_url == self.local.url

    def server_label(self, s: vm.AdminSession) -> str:
        return "This computer (built-in service)" if self.is_local(s) else s.client.base_url

    def signing_card(self, ctx: ViewContext) -> ft.Control:
        """The key apps trust for licenses and statement formats. Only its public half is shown."""
        from core.licensing.tokens import VENDOR_PUBLIC_KEYS

        pal = ctx.pal
        try:
            public = self.local.public_key_hex()
        except (LocalServiceError, OSError, ValueError) as exc:
            return card(notice_banner("serious", "Signing key unreadable", str(exc), pal), pal=pal,
                        title="Signing key", key="account-signing")
        pinned = public in VENDOR_PUBLIC_KEYS
        return card(
            notice_banner("good" if pinned else "warning", "Trusted by this app version" if pinned else "Not pinned yet",
                          "iFlow Core apps built from this version accept licenses and statement formats signed "
                          "with this key." if pinned else "Apps accept what this key signs only once its public key "
                          "is in core/licensing/tokens.py (VENDOR_PUBLIC_KEYS) and a new app version is built.",
                          pal, key="account-signing-status"),
            ft.Row([ft.Container(text("Public key", pal, size=13, weight=ft.FontWeight.W_600), width=140),
                    text(public, pal, size=12, role="secondary", selectable=True)], key="account-public-key"),
            text(f"The private key is in {self.local.key_path} (readable only by you). Back it up somewhere safe "
                 "and offline: if it's lost, new licenses and formats need a new key and new app builds.", pal,
                 size=12, role="muted"),
            pal=pal, title="Signing key", key="account-signing")

    def publishing_card(self, ctx: ViewContext, s: vm.AdminSession, redraw: Any) -> ft.Control:
        """Where Parser Studio sends published formats (the CDN bucket from infra/, IflowCoreFormats)."""
        pal = ctx.pal
        cur = self.local.load_publishing()
        bucket = ft.TextField(label="CDN bucket", value=cur.formats_bucket, width=320, key="publish-bucket",
                              hint_text="from the IflowCoreFormats deploy output")
        dist = ft.TextField(label="CloudFront distribution id", value=cur.formats_distribution_id, width=240,
                            key="publish-distribution")
        url = ft.TextField(label="Public address apps download", value=cur.formats_public_url, width=440,
                           key="publish-url")
        profile = ft.TextField(label="AWS profile", value=cur.aws_profile, width=160, key="publish-profile",
                               hint_text="iflow")
        note = self.services.pop("publish_note", None)

        def save(e: Any) -> None:
            sections.begin(s)
            self.local.save_publishing(PublishingSettings(
                formats_bucket=(bucket.value or "").strip(), formats_distribution_id=(dist.value or "").strip(),
                formats_public_url=(url.value or "").strip(), aws_profile=(profile.value or "").strip()))
            try:
                self.local.restart()                         # sessions live in admin.db: you stay signed in
                self.services["publish_note"] = ("good", "Saved. Publishing now goes to the CDN." if bucket.value
                                                 else "Saved. Publishing to the CDN is off.")
            except LocalServiceError as exc:
                self.services["publish_note"] = ("serious", f"Saved, but the service didn't restart: {exc}")
            redraw("account")

        return card(
            text("Parser Studio sends each published format to this bucket, and apps download it from the public "
                 "address. Sign in to AWS with `aws sso login --profile <profile>` before publishing.", pal,
                 size=13, role="secondary"),
            *([notice_banner(note[0], "Publishing", note[1], pal, key="publish-note")] if note else []),
            ft.Row([bucket, dist], wrap=True, spacing=12, run_spacing=12),
            ft.Row([url, profile], wrap=True, spacing=12, run_spacing=12),
            ft.Row([ft.FilledButton("Save", on_click=save, key="publish-save")]),
            pal=pal, title="Publishing to the CDN", key="account-publishing")


def _wide(field: ft.TextField) -> ft.Row:
    field.expand = True
    return ft.Row([field], visible=field.visible)


def main(page: ft.Page) -> None:
    page.window.min_width = MIN_WIDTH
    page.window.min_height = 600
    AdminConsoleApp(page).start()


def run() -> None:
    ft.run(main, assets_dir=str(brand.ASSETS_DIR))
