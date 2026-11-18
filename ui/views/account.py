"""
Settings → iFlow account: "Sign in to iFlow" and end-to-end encrypted sync (Phase 10.5).

One card, one step at a time (see ui/viewmodels/account.py for the flow). Nothing to paste: the
iFlow-hosted server is the default; self-hosters change it under "Advanced". Network calls run in
the background and the card redraws when they finish.
"""

from __future__ import annotations

from typing import Any

import flet as ft

from ui.components.common import card, notice_banner, text
from ui.viewmodels import account as vm
from ui.views.base import ViewContext

INTRO = ("Optional. Sync your records across your devices with end-to-end encryption: everything is encrypted on "
         "this device with your sync passphrase before it leaves, so iFlow's servers only ever store ciphertext. "
         "iFlow Core works fully without an account.")


def account_card(ctx: ViewContext, col: dict) -> ft.Container:
    pal = ctx.pal
    u = vm.ui(ctx.services)
    st = vm.status(ctx.state, ctx.services)

    def background(fn: Any) -> Any:
        """Run a network action off the UI thread, then redraw."""
        def handler(e: Any) -> None:
            def work() -> None:
                fn()
                ctx.refresh()

            runner = ctx.services.get("run_background")
            runner(work) if runner else work()
        return handler

    def field(key: str, label: str, *, password: bool = False, value: str = "", **kw: Any) -> ft.TextField:
        return ft.TextField(label=label, value=value, password=password, can_reveal_password=password,
                            key=f"account-{key}", **kw)

    body: list[ft.Control] = [text(INTRO, pal, size=13, role="secondary")]
    if u.error:
        body.append(notice_banner("serious", "Not done", u.error, pal, key="account-error"))
    if u.notice:
        body.append(notice_banner("good", "Done", u.notice, pal, key="account-notice"))
    if u.recovery_key:
        body.append(_recovery_key(ctx, u))

    if not st.signed_in:
        body += _signed_out(ctx, u, field, background)
    elif not st.syncing:
        body += _passphrase(ctx, u, st, field, background)
    else:
        body += _syncing(ctx, u, st, background)
    title = "iFlow account & sync" + (f": {st.email}" if st.email else "")
    return card(*body, pal=pal, title=title, col=col, key="account-card")


def _row(*controls: ft.Control) -> ft.Row:
    return ft.Row(list(controls), wrap=True, spacing=8, run_spacing=8)


def _signed_out(ctx: ViewContext, u: vm.AccountUi, field: Any, background: Any) -> list[ft.Control]:
    pal, s, services = ctx.pal, ctx.state, ctx.services
    lay = ctx.layout
    goto = lambda step: (lambda e: (vm.go(services, step), ctx.refresh()))            # noqa: E731
    if u.step == "signup":
        email, pw, confirm = (field("email", "Email", value=u.email), field("password", "Password", password=True),
                              field("confirm", "Confirm password", password=True))
        return [email, pw, confirm,
                text("12+ characters. This password signs you in; your sync passphrase (next) protects your data.",
                     pal, size=12, role="muted"),
                _row(ft.FilledButton("Create account", key="account-signup", height=lay.min_target,
                                     on_click=background(lambda: vm.sign_up(s, services, email.value, pw.value,
                                                                            confirm.value))),
                     ft.TextButton("I already have an account", on_click=goto("signin"), key="account-to-signin"))]
    if u.step == "verify":
        code = field("code", "6-digit code", max_length=6, input_filter=ft.NumbersOnlyInputFilter())
        return [text(f"Check {u.email} for a 6-digit code.", pal, size=13), code,
                _row(ft.FilledButton("Verify", key="account-verify", height=lay.min_target,
                                     on_click=background(lambda: vm.verify(s, services, code.value))),
                     ft.TextButton("Send a new code", key="account-resend",
                                   on_click=background(lambda: vm.resend(s, services))),
                     ft.TextButton("Back", on_click=goto("signin"), key="account-back"))]
    if u.step == "forgot":
        email = field("email", "Email", value=u.email)
        return [email,
                _row(ft.FilledButton("Send reset code", key="account-forgot", height=lay.min_target,
                                     on_click=background(lambda: vm.forgot(s, services, email.value))),
                     ft.TextButton("Back", on_click=goto("signin"), key="account-back"))]
    if u.step == "reset":
        code, pw, confirm = (field("code", "Code from the email", max_length=6),
                             field("password", "New password", password=True),
                             field("confirm", "Confirm new password", password=True))
        return [code, pw, confirm,
                _row(ft.FilledButton("Change password", key="account-reset", height=lay.min_target,
                                     on_click=background(lambda: vm.reset(s, services, code.value, pw.value,
                                                                          confirm.value))),
                     ft.TextButton("Back", on_click=goto("signin"), key="account-back"))]
    email, pw = field("email", "Email", value=u.email), field("password", "Password", password=True)
    sign_in = background(lambda: vm.sign_in(s, services, email.value, pw.value))
    pw.on_submit = sign_in
    server = field("server", "Sync server", value=vm.server_url(s, services), dense=True)
    advanced = ft.ExpansionTile(
        title=ft.Text("Advanced: use my own sync server", size=13), key="account-advanced",
        controls=[ft.Container(ft.Column([
            server,
            text("Only if you run the iFlow sync server yourself. Data is end-to-end encrypted either way.", pal,
                 size=12, role="muted"),
            ft.TextButton("Use this server", key="account-set-server",
                          on_click=lambda e: (vm.set_server(s, services, server.value), ctx.refresh()))],
            spacing=8, tight=True), padding=ft.Padding.only(bottom=8))])
    return [email, pw,
            _row(ft.FilledButton("Sign in to iFlow", icon=ft.Icons.LOGIN, key="account-signin", height=lay.min_target,
                                 on_click=sign_in),
                 ft.TextButton("Create an account", on_click=goto("signup"), key="account-to-signup"),
                 ft.TextButton("Forgot password?", on_click=goto("forgot"), key="account-to-forgot")),
            advanced]


def _passphrase(ctx: ViewContext, u: vm.AccountUi, st: vm.Status, field: Any, background: Any) -> list[ft.Control]:
    pal, s, services, lay = ctx.pal, ctx.state, ctx.services, ctx.layout
    if u.step == "recover":
        key, pw, confirm = (field("recovery", "Recovery key", text_style=ft.TextStyle(font_family="monospace")),
                            field("passphrase", "New sync passphrase", password=True),
                            field("passphrase-confirm", "Confirm new passphrase", password=True))
        return [text("Enter the recovery key you saved when you set up sync, then choose a new passphrase.", pal,
                     size=13), key, pw, confirm,
                _row(ft.FilledButton("Recover sync", key="account-recover", height=lay.min_target,
                                     on_click=background(lambda: vm.recover(s, services, key.value, pw.value,
                                                                            confirm.value))),
                     ft.TextButton("Back", key="account-back",
                                   on_click=lambda e: (vm.go(services, "signin"), ctx.refresh())))]
    pw = field("passphrase", "Sync passphrase", password=True)
    confirm = field("passphrase-confirm", "Confirm passphrase", password=True)
    turn_on = background(lambda: vm.setup_sync(s, services, pw.value or "", confirm.value or ""))
    confirm.on_submit = turn_on
    return [
        text(f"Signed in as {st.email}.", pal, size=13, weight=ft.FontWeight.W_600),
        text("Choose a sync passphrase (10+ characters). It encrypts your data on this device and is never sent "
             "to iFlow. Already syncing on another device? Enter the same passphrase there.", pal, size=13,
             role="secondary"),
        pw, confirm,
        _row(ft.FilledButton("Turn on sync", icon=ft.Icons.LOCK_OUTLINE, key="account-sync-on", height=lay.min_target,
                             on_click=turn_on),
             ft.TextButton("Forgot the passphrase? Use your recovery key", key="account-to-recover",
                           on_click=lambda e: (vm.go(services, "recover"), ctx.refresh())),
             ft.TextButton("Sign out", key="account-signout",
                           on_click=background(lambda: vm.sign_out(s, services)))),
    ]


def _recovery_key(ctx: ViewContext, u: vm.AccountUi) -> ft.Control:
    pal = ctx.pal

    def saved(e: Any) -> None:
        u.recovery_key = None
        u.notice = "Recovery key hidden. Keep it somewhere safe and offline."
        ctx.refresh()

    return ft.Container(ft.Column([
        notice_banner("warning", "Save your recovery key", "If you forget your sync passphrase, this key is the only "
                      "way to get your synced data back. iFlow can't recover it for you. Write it down or keep it in "
                      "a password manager.", pal),
        ft.TextField(value=u.recovery_key, read_only=True, key="account-recovery-key", multiline=True,
                     text_style=ft.TextStyle(font_family="monospace", size=15)),
        ft.FilledButton("I've saved it", key="account-recovery-saved", on_click=saved),
    ], spacing=8, tight=True), key="account-recovery")


def _syncing(ctx: ViewContext, u: vm.AccountUi, st: vm.Status, background: Any) -> list[ft.Control]:
    pal, s, services, lay = ctx.pal, ctx.state, ctx.services, ctx.layout
    label, tip = vm.chip(s, services)
    lines: list[ft.Control] = [
        ft.Row([ft.Icon(ft.Icons.LOCK_OUTLINE, size=16, color=pal.text_secondary),
                text(f"{label} · end-to-end encrypted", pal, size=13, weight=ft.FontWeight.W_600)], spacing=6,
               key="account-sync-status"),
        text(f"Last sync: {u.last_sync} ({u.last_report})" if u.last_sync else "Syncs when you open the app and when "
             "you tap Sync now.", pal, size=12, role="muted"),
        text(f"Server: {st.server}", pal, size=12, role="muted"),
    ]
    if u.sync_error:
        lines.append(notice_banner("warning", "Sync paused", u.sync_error, pal, key="account-sync-error"))
    devices = u.devices or []
    if devices:
        rows = []
        for d in devices:
            current = bool(d.get("current"))
            rows.append(ft.Row([
                ft.Icon(ft.Icons.DEVICES_OTHER_OUTLINED, size=16, color=pal.text_secondary),
                ft.Container(text(f"{d.get('name', 'Device')}" + (" (this device)" if current else "")
                                  + (f" · last seen {d['last_seen_on']}" if d.get("last_seen_on") else ""), pal, size=13), expand=True),
                *([] if current else [ft.TextButton("Remove", key=f"account-remove-{d.get('id')}",
                                                    on_click=background(lambda i=d.get("id"): _revoke(s, services, i)))]),
            ], key=f"account-device-{d.get('id')}"))
        lines += [text("Devices", pal, size=13, weight=ft.FontWeight.W_600), *rows]

    def confirm_delete(e: Any) -> None:
        def go(_: Any) -> None:
            ctx.close_dialog()
            background(lambda: vm.delete_account(s, services))(None)

        ctx.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("Delete your iFlow account?"),
            content=ft.Text("This deletes your account and every encrypted copy on iFlow's servers, and signs out "
                            "your other devices. The data on each device stays there. This can't be undone."),
            actions=[ft.TextButton("Cancel", on_click=lambda _: ctx.close_dialog()),
                     ft.FilledButton("Delete account", on_click=go, key="account-delete-confirm")],
            key="account-delete-dialog"))

    lines.append(_row(
        ft.FilledButton("Sync now", icon=ft.Icons.SYNC, key="account-sync-now", height=lay.min_target,
                        on_click=background(lambda: vm.sync_now(s, services))),
        ft.OutlinedButton("Turn off sync on this device", key="account-sync-off", height=lay.min_target,
                          on_click=lambda e: (vm.turn_off_sync(s, services), ctx.refresh())),
        ft.TextButton("Sign out", key="account-signout", on_click=background(lambda: vm.sign_out(s, services))),
        ft.TextButton("Delete account…", key="account-delete", on_click=confirm_delete)))
    return lines


def _revoke(state: Any, services: dict[str, Any], device_id: str) -> None:
    u = vm.ui(services)
    u.clear()
    try:
        vm.client(state, services).revoke_device(device_id)
        u.notice = "Removed. That device is signed out and can't sync any more."
    except Exception as exc:
        u.error = getattr(exc, "message", None) or "Couldn't remove that device."
