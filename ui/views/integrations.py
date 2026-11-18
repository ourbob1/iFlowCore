"""
Finances → "Connect a platform": one panel per published integration (e.g. Uber), with its
connection form rendered from the signed schema, sign-in, "Pull now" and "Disconnect".

Shown only when at least one integration is installed. Network work runs in the background and
the page redraws when it finishes.
"""

from __future__ import annotations

from typing import Any

import flet as ft

from core.integration.schema import IntegrationSchema
from ui.components.common import card, notice_banner, text
from ui.components.integration_form import build_form
from ui.viewmodels import integrations as vm
from ui.views.base import ViewContext


def integrations_card(ctx: ViewContext, platforms: tuple[tuple[str, str], ...]) -> ft.Control | None:
    schemas = vm.installed(ctx.state)
    if not schemas:
        return None
    pal = ctx.pal
    body: list[ft.Control] = [text(
        "Bring in earnings straight from a platform instead of downloading statements. You sign in with the "
        "platform; the app keeps the connection on this device (encrypted) and only ever calls that platform.",
        pal, size=13, role="secondary")]
    for schema in schemas:
        body.append(_panel(ctx, schema, platforms))
    return card(*body, pal=pal, title="Connect a platform", key="integrations-card")


def _panel(ctx: ViewContext, schema: IntegrationSchema, platforms: tuple[tuple[str, str], ...]) -> ft.Control:
    pal, s, services, lay = ctx.pal, ctx.state, ctx.services, ctx.layout
    u = vm.ui(services, schema.id)
    connected, line = vm.status(s, schema)
    entry = vm.saved(s, schema.id)
    key = f"conn-{schema.id}"

    def background(fn: Any) -> Any:
        def handler(e: Any) -> None:
            def work() -> None:
                fn()
                ctx.refresh()

            runner = services.get("run_background")
            runner(work) if runner else work()
        return handler

    def toggle(e: Any) -> None:
        u.open = not u.open
        ctx.refresh()

    rows: list[ft.Control] = [ft.Row([
        ft.Icon(ft.Icons.LINK if connected else ft.Icons.LINK_OFF, color=pal.text_secondary, size=20),
        ft.Column([text(schema.name, pal, size=14, weight=ft.FontWeight.W_600),
                   text(line, pal, size=12, role="secondary")], spacing=2, tight=True, expand=True),
        ft.TextButton("Hide" if u.open else ("Manage" if connected else "Set up"), on_click=toggle,
                      key=f"{key}-toggle"),
    ], key=f"{key}-row")]
    if u.error:
        rows.append(notice_banner("serious", "Not done", u.error, pal, key=f"{key}-error"))
    if u.notice:
        rows.append(notice_banner("good", "Done", u.notice, pal, key=f"{key}-notice"))
    if not u.open:
        return ft.Column(rows, spacing=8, key=key)

    if schema.description:
        rows.append(text(schema.description, pal, size=13))
    form = build_form(schema, vm.values_for(s, schema), key_prefix=key, errors=u.field_errors,
                      secrets_saved=vm.secrets_saved(s, schema))
    platform = ft.Dropdown(label="Add to platform", width=260, key=f"{key}-platform",
                           value=entry.get("platform_id") or vm.default_platform(platforms, schema),
                           options=[ft.DropdownOption(key=i, text=n) for i, n in platforms])
    rows += form.controls
    rows.append(platform if platforms else text("Add a platform in Settings first.", pal, size=13, role="muted"))

    def save() -> bool:
        return vm.save_form(s, services, schema, form.read(), platform.value)

    actions: list[ft.Control] = []
    if schema.auth.type == "oauth2_pkce":
        if vm.can_sign_in_here(s):
            actions.append(ft.OutlinedButton(
                "Reconnect" if connected else f"Sign in with {schema.platform}", icon=ft.Icons.LOGIN,
                key=f"{key}-connect", height=lay.min_target,
                on_click=background(lambda: save() and vm.connect(s, services, schema))))
        else:
            rows.append(text(f"Sign in with {schema.platform} from iFlow Core on a computer; then pull from here.",
                             pal, size=12, role="muted"))
    actions += [
        ft.FilledButton("Pull now", icon=ft.Icons.DOWNLOAD, key=f"{key}-pull", height=lay.min_target,
                        disabled=not platforms,
                        on_click=background(lambda: save() and vm.pull_now(s, services, schema))),
        ft.TextButton("Save", key=f"{key}-save", on_click=lambda e: (save(), ctx.refresh())),
    ]
    if connected:
        actions.append(ft.TextButton("Disconnect", key=f"{key}-disconnect",
                                     on_click=lambda e: (vm.disconnect(s, services, schema), ctx.refresh())))
    rows.append(ft.Row(actions, wrap=True, spacing=8))
    rows.append(text(f"Calls only {schema.host}. Tokens stay on this device; nothing goes to iFlow except the "
                     "sign-in step for platforms that require it.", pal, size=12, role="muted"))
    return ft.Container(ft.Column(rows, spacing=8), padding=ft.Padding.only(bottom=8), key=key)
