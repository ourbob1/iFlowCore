"""Unlock / first-run passphrase screen."""

from __future__ import annotations

from typing import Any, Callable

import flet as ft

from ui import brand
from ui.components.common import status_banner, text
from ui.state import UnlockError, Unlocker
from ui.views.base import ViewContext


def build(ctx: ViewContext) -> ft.Control:
    unlocker: Unlocker = ctx.services["unlocker"]
    on_unlocked: Callable[[Any], None] = ctx.services["on_unlocked"]
    plan = unlocker.plan()
    if plan.mode == "device_key":
        return device_key_screen(ctx, plan.message)
    creating = plan.mode in ("create", "encrypt_existing")

    passphrase = ft.TextField(label="Passphrase", password=True, can_reveal_password=True, autofocus=True,
                              key="passphrase", width=360)
    confirm = ft.TextField(label="Confirm passphrase", password=True, can_reveal_password=True, key="confirm",
                           width=360, visible=creating)
    error = text("", ctx.pal, size=13, role="secondary")

    def submit(e: Any) -> None:
        try:
            db = unlocker.open(passphrase.value or None, confirm=confirm.value if creating else None)
        except UnlockError as exc:
            error.value = str(exc)
            passphrase.value = ""
            confirm.value = ""
            error.update()
            passphrase.update()
            confirm.update()
            return
        passphrase.value = confirm.value = ""       # don't keep the secret in the control tree
        on_unlocked(db)

    passphrase.on_submit = submit
    confirm.on_submit = submit
    children: list[ft.Control] = [
        brand.logo(ctx.pal, width=min(300, ctx.layout.width - 64)),
        ft.Row([ft.Icon(ft.Icons.LOCK_OUTLINE, size=16, color=ctx.pal.text_secondary),
                text(plan.message, ctx.pal, size=14, role="secondary")],
               spacing=6, alignment=ft.MainAxisAlignment.CENTER, wrap=True),
    ]
    if plan.mode == "encrypt_existing":
        children.append(status_banner(
            "warning", "Encrypt your existing data",
            "This database was saved without encryption (for example by a development run). Your data is kept "
            "and encrypted with the passphrase you choose. It can't be recovered if lost.", ctx.pal,
            key="encrypt-existing"))
    elif creating:
        children.append(status_banner(
            "warning", "Remember this passphrase",
            "It encrypts everything on this device. It can't be recovered if lost.", ctx.pal))
    if plan.mode == "plaintext_dev":
        children.append(status_banner("warning", "Development mode", "Leave blank to open unencrypted.", ctx.pal))
    children += [passphrase, confirm, error,
                 ft.FilledButton({"create": "Create & unlock", "encrypt_existing": "Encrypt & unlock"}
                                 .get(plan.mode, "Unlock"), on_click=submit,
                                 height=ctx.layout.min_target, key="btn-unlock")]
    return ft.Container(
        ft.Column(children, horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=14, tight=True, width=420),
        alignment=ft.Alignment.CENTER, expand=True, padding=ft.Padding.all(24), key="view-unlock",
    )


def device_key_screen(ctx: ViewContext, message: str) -> ft.Control:
    """Mobile: no passphrase — the key lives in the Keychain / Android Keystore."""
    error = ctx.services.get("last_error")
    retry = ctx.services.get("device_unlock")
    children: list[ft.Control] = [brand.logo(ctx.pal, width=min(300, ctx.layout.width - 64))]
    if error:
        children += [
            status_banner("critical", "Secure storage problem", error, ctx.pal, key="device-key-error"),
            ft.FilledButton("Try again", on_click=lambda e: retry() if retry else None,
                            height=ctx.layout.min_target, key="btn-retry-device-key"),
        ]
    else:
        children += [ft.ProgressRing(key="device-key-progress"), text(message, ctx.pal, size=14, role="secondary")]
    return ft.Container(
        ft.Column(children, horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=14, tight=True, width=420),
        alignment=ft.Alignment.CENTER, expand=True, padding=ft.Padding.all(24), key="view-unlock",
    )
