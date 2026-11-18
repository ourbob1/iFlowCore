"""
Plans: current tier, what each tier unlocks, license key entry, and the
(simulated, development-only) upgrade flow.

Transparency rules for this page:
  * every price is shown with its billing period and renewal amount before
    confirming; any credit for unused time is itemized
  * it says, in so many words, that licenses are checked on the device and
    that nothing is ever deleted or hidden when a plan ends
  * simulated purchases say they are simulations, and production builds
    don't offer them
"""

from __future__ import annotations

from typing import Any

import flet as ft

from core.config import minor_units
from core.licensing import FEATURES, LicenseError, Period, Plan, Tier
from ui.components.common import card, primary_button, status_banner, text
from ui.format import money
from ui.viewmodels.licensing import comparison, status_view, tier_cards
from ui.views.base import ViewContext, page_column

HOW_IT_WORKS = (
    "Your plan is a signed license key stored in your encrypted database. It's checked on this device; "
    "no account, no sign-in, and nothing about you or your money is sent anywhere.",
    "If a plan ends, you get a 7-day grace period, then you're back on Solo Core. Nothing is deleted or hidden: "
    "only new Pro or Guild actions (like starting trip detection) pause until you renew.",
    "CSV export of your ledger and mileage log is always free, on every plan.",
)


def build(ctx: ViewContext) -> ft.Control:
    st = ctx.state.license
    pal = ctx.pal
    sv = status_view(st)
    focus = ctx.params.get("focus")
    controls: list[ft.Control] = [
        status_banner(sv.level, sv.title, sv.message, pal, key="plan-status"),
        ft.ResponsiveRow([tier_card(ctx, c, focused=c.tier.value == focus) for c in tier_cards(st)],
                         spacing=12, run_spacing=12, key="tier-cards"),
        comparison_card(ctx),
        license_card(ctx),
        card(*[ft.Row([ft.Icon(ft.Icons.VERIFIED_USER_OUTLINED, size=16, color=pal.text_secondary),
                       ft.Container(text(t, pal, size=13, role="secondary"), expand=True)],
                      spacing=8, vertical_alignment=ft.CrossAxisAlignment.START) for t in HOW_IT_WORKS],
             pal=pal, title="How plans work", key="how-it-works"),
    ]
    return page_column(ctx, *controls, key="view-plans")


def tier_card(ctx: ViewContext, c: Any, *, focused: bool) -> ft.Container:
    pal, lay = ctx.pal, ctx.layout
    body: list[ft.Control] = [
        text(c.tagline, pal, size=13, role="secondary"),
        text(c.price, pal, size=22, weight=ft.FontWeight.W_700),
    ]
    if c.price_note:
        body.append(text(c.price_note, pal, size=12, role="muted"))
    if c.includes_below:
        body.append(text(c.includes_below, pal, size=12, role="secondary"))
    for label in c.features:
        body.append(ft.Row([ft.Icon(ft.Icons.CHECK, size=16, color=pal.text_primary),
                            ft.Container(text(label, pal, size=13), expand=True)], spacing=6))
    st = ctx.state.license
    if c.current:
        body.append(ft.OutlinedButton("Current plan", disabled=True, height=lay.min_target, key=f"cta-{c.tier.value}"))
    elif c.tier is Tier.SOLO:
        body.append(ft.OutlinedButton("Switch to Solo Core", on_click=lambda e: confirm_downgrade(ctx),
                                      height=lay.min_target, key="cta-solo"))
    else:
        for p in sorted(c.plans, key=lambda p: p.period is Period.ANNUAL):
            per = "month" if p.period is Period.MONTHLY else "year"
            verb = "Switch to" if st.paid else "Get"
            whole = p.amount_minor % 10 ** minor_units(p.currency) == 0
            body.append(primary_button(f"{verb} {c.name} · {money(p.amount_minor, p.currency, cents=not whole)}/{per}",
                                       lambda e, p=p: open_upgrade(ctx, p), lay, key=f"cta-{p.id}"))
    box = card(*body, pal=pal, title=c.name, col={"xs": 12, "md": 4}, key=f"tier-{c.tier.value}")
    if focused or c.current:
        box.border = ft.Border.all(2, pal.text_primary)
    return box


def comparison_card(ctx: ViewContext) -> ft.Container:
    pal = ctx.pal
    rows = comparison()

    def mark(on: bool) -> ft.Control:
        return ft.Row([ft.Icon(ft.Icons.CHECK if on else ft.Icons.REMOVE, size=16,
                               color=pal.text_primary if on else pal.text_muted),
                       text("Included" if on else "Not included", pal, size=11,
                            role="secondary" if on else "muted")], spacing=4, tight=True)

    table = ft.DataTable(
        columns=[ft.DataColumn(text("Feature", pal, size=13, role="secondary"))]
        + [ft.DataColumn(text(t.label, pal, size=13, role="secondary")) for t in (Tier.SOLO, Tier.PRO, Tier.GUILD)],
        rows=[ft.DataRow(cells=[ft.DataCell(text(r.label, pal, size=13))] + [ft.DataCell(mark(v)) for v in r.tiers])
              for r in rows],
        column_spacing=16, key="comparison-table",
    )
    scroller = ft.Row([table], scroll=ft.ScrollMode.AUTO)
    if ctx.layout.size_class == "compact":
        return card(ft.ExpansionTile(title=text("Compare every feature", pal, size=14), controls=[scroller],
                                     key="comparison-tile"), pal=pal, key="comparison-card")
    return card(scroller, pal=pal, title="Compare plans", key="comparison-card")


def license_card(ctx: ViewContext) -> ft.Container:
    pal = ctx.pal
    st = ctx.state.license
    key_field = ft.TextField(label="License key", hint_text="RSL1.…", password=True, can_reveal_password=True,
                             multiline=False, key="license-key")

    def activate(e: Any) -> None:
        try:
            new = ctx.state.license_manager().activate(key_field.value or "")
        except LicenseError as exc:
            ctx.notify(str(exc))
            return
        ctx.notify(f"{new.tier.label} activated on this device")
        ctx.refresh()

    body: list[ft.Control] = []
    if st.claims is not None:
        c = st.claims
        issuer = "simulated (development build)" if c.issuer == "simulation" else "iFlow Core"
        body.append(text(f"{c.tier.label} · {c.period.value} · issued by {issuer} · "
                         f"expires {c.expires_at:%b} {c.expires_at.day}, {c.expires_at.year} · id …{c.license_id[-6:]}",
                         pal, size=12, role="secondary"))
        body.append(ft.TextButton("Remove license from this device", icon=ft.Icons.DELETE_OUTLINE,
                                  on_click=lambda e: confirm_downgrade(ctx), key="btn-remove-license"))
    body += [key_field,
             ft.FilledButton("Activate", on_click=activate, height=ctx.layout.min_target, key="btn-activate")]
    return card(*body, pal=pal, title="License key", key="license-card")


# --- flows ---------------------------------------------------------------------------------------


def open_upgrade(ctx: ViewContext, target: Plan) -> None:
    mgr = ctx.state.license_manager()
    today = ctx.state.today()
    pal = ctx.pal
    if not mgr.allow_simulation:
        ctx.show_dialog(ft.AlertDialog(
            title=ft.Text(f"Get {target.tier.label}"), key="upgrade-dialog",
            content=ft.Text("In-app purchases aren't available in this build. If you bought a license, paste its "
                            "key under License key on this page."),
            actions=[ft.TextButton("OK", on_click=lambda e: ctx.close_dialog())]))
        return
    q = mgr.quote_for(target, today)
    per = "month" if target.period is Period.MONTHLY else "year"
    lines: list[ft.Control] = [
        text(f"{target.tier.label}, billed every {per}", pal, size=14, weight=ft.FontWeight.W_600),
        text(f"Price: {money(target.amount_minor, target.currency)} / {per}", pal, size=13),
    ]
    if q.credit_minor:
        lines.append(text(f"Credit for unused time on your current plan: −{money(q.credit_minor, target.currency)}",
                          pal, size=13))
    lines += [
        text(f"Due today: {money(q.due_today_minor, target.currency)}", pal, size=15, weight=ft.FontWeight.W_700,
             selectable=True),
        text(f"Renews {q.renews_on:%b} {q.renews_on.day}, {q.renews_on.year} at "
             f"{money(q.renewal_minor, target.currency)}", pal, size=13, role="secondary"),
        ft.Container(status_banner("warning", "Simulation", "No payment is taken. This development build issues a "
                                   "simulated license so you can try the upgrade flow.", pal),
                     padding=ft.Padding.only(top=8)),
    ]

    def confirm(e: Any) -> None:
        try:
            st, _ = mgr.simulate_purchase(target, today)
        except LicenseError as exc:
            ctx.notify(str(exc))
            return
        ctx.close_dialog()
        ctx.notify(f"{st.tier.label} is active (simulated)")
        ctx.navigate("/plans")

    verb = "Switch to" if ctx.state.license.paid else "Upgrade to"
    ctx.show_dialog(ft.AlertDialog(
        modal=True, title=ft.Text(f"{verb} {target.tier.label}"), key="upgrade-dialog",
        content=ft.Column(lines, tight=True, spacing=6, width=380),
        actions=[ft.TextButton("Cancel", on_click=lambda e: ctx.close_dialog(), key="upgrade-cancel"),
                 ft.FilledButton(f"Confirm simulated {'switch' if verb == 'Switch to' else 'upgrade'}",
                                 on_click=confirm, key="upgrade-confirm")]))


def confirm_downgrade(ctx: ViewContext) -> None:
    st = ctx.state.license

    def go(e: Any) -> None:
        ctx.state.license_manager().remove()
        ctx.close_dialog()
        ctx.notify("You're on Solo Core. All your data is still here.")
        ctx.navigate("/plans")

    paused = [FEATURES[f].label for f in FEATURES if FEATURES[f].tier is not Tier.SOLO and st.tier.includes(FEATURES[f].tier)]
    ctx.show_dialog(ft.AlertDialog(
        modal=True, title=ft.Text("Switch to Solo Core?"), key="downgrade-dialog",
        content=ft.Column([
            ft.Text(f"This removes the {st.tier.label} license from this device. Nothing you've recorded is "
                    "deleted or hidden."),
            ft.Text("These pause until you upgrade again:"),
            *[ft.Text(f"•  {label}") for label in paused],
        ], tight=True, spacing=6, width=420),
        actions=[ft.TextButton("Keep my plan", on_click=lambda e: ctx.close_dialog()),
                 ft.FilledButton("Switch to Solo Core", on_click=go, key="downgrade-confirm")]))

