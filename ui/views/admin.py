"""
Admin Console sections: overview, licenses & revenue, crash reports, health and audit. The console
shell (ui/admin_console/app.py) lays them out; each builder takes (ctx, session, redraw) and is
rebuilt in place by redraw(*names), so actions keep the page's scroll position.

Everything here comes from the AI service's admin API; the console never opens anyone's local
data, and nothing it shows is personal: licenses carry an opaque customer reference at most, and
crash reports were scrubbed on the devices. Money is integer cents end to end.
"""

from __future__ import annotations

from typing import Any

import flet as ft

from core.admin.client import AdminError
from ui.components.common import card, kpi_tile, text
from ui.components.common import notice_banner as banner
from ui.viewmodels import admin as vm
from ui.views.base import ViewContext


def notice_banner(ctx: ViewContext, s: vm.AdminSession, redraw: Any) -> ft.Control:
    # builders only *read* messages (a section can be built twice); actions clear them via _begin()
    return banner("good", "Done", s.notice, ctx.pal, key="admin-notice") if s.notice else ft.Container()


begin = vm.begin                                # start of a user action: drop the last action's messages


def section_error(ctx: ViewContext, s: vm.AdminSession, name: str) -> list[ft.Control]:
    err = s.errors.get(name)
    return [banner("serious", "Couldn't load", err, ctx.pal, key=f"admin-error-{name}")] if err else []


def overview_row(ctx: ViewContext, s: vm.AdminSession, redraw: Any) -> ft.Control:
    o = s.data.get("overview") or {}
    t = o.get("telemetry") or {}
    tiles = [
        kpi_tile("Open issues", str(t.get("open_issues", "—")), ctx.pal, sub=f"{t.get('events_24h', 0)} reports in 24 h",
                 key="admin-kpi-issues"),
        kpi_tile("Licenses this month", str(o.get("licenses_this_month", "—")), ctx.pal, key="admin-kpi-licenses"),
        kpi_tile("Uptime", vm.duration(o.get("uptime_s")), ctx.pal, key="admin-kpi-uptime"),
    ]
    if s.can("licenses.read"):
        tiles.insert(2, kpi_tile("Revenue this month", vm.cents(o.get("revenue_this_month_minor"),
                                                                 o.get("currency", "USD")), ctx.pal,
                                 sub="from issued licenses", key="admin-kpi-revenue"))
    return ft.Column([*section_error(ctx, s, "overview"),
                      ft.ResponsiveRow([ft.Container(tile, col={"md": 12 // len(tiles), "xs": 6}) for tile in tiles],
                                       spacing=12, run_spacing=12)], key="admin-overview")


# --- licenses --------------------------------------------------------------------------------------------


def license_card(ctx: ViewContext, s: vm.AdminSession, redraw: Any) -> ft.Control:
    pal = ctx.pal
    body: list[ft.Control] = section_error(ctx, s, "plans")
    if not s.license_signing:
        body.append(banner("warning", "Issuing is off",
                                  "Set RS_AI_LICENSE_SIGNING_KEY on the server to issue licenses. Verification of "
                                  "existing keys still works once it's set.", pal, key="admin-signing-off"))
    options = vm.plan_options(s)
    plan = ft.Dropdown(label="Plan", options=[ft.DropdownOption(key=k, text=t) for k, t in options],
                       value=options[0][0] if options else None, key="admin-plan", width=340)
    terms = ft.TextField(label="Terms", value="1", width=90, key="admin-terms", input_filter=ft.NumbersOnlyInputFilter())
    comp = ft.Checkbox(label="Complimentary ($0)", key="admin-comp")
    ref = ft.TextField(label="Customer reference (optional)", hint_text="order id, not a name or email",
                       key="admin-ref", width=300)
    issue_error = text(s.errors.get("issue", ""), pal, size=13, role="secondary")
    issue_error.key = "admin-issue-error"

    def do_issue(e: Any) -> None:
        begin(s)
        if not plan.value:
            return
        tier, period = plan.value.split(":")
        try:
            s.issued = s.client.issue_license(tier, period, terms=int(terms.value or "1"), complimentary=bool(comp.value),
                                              customer_ref=(ref.value or "").strip() or None)
        except (AdminError, ValueError) as exc:
            s.errors["issue"] = getattr(exc, "message", str(exc))
            redraw("licenses")
            return
        vm.load(s, ("licenses", "revenue", "overview", "audit"))
        redraw("licenses", "revenue", "overview", "audit")

    body.append(ft.Row([plan, terms, comp, ref,
                        ft.FilledButton("Issue license", icon=ft.Icons.KEY, on_click=do_issue, key="admin-issue",
                                        disabled=not (s.license_signing and options and s.can("licenses.issue")))],
                       wrap=True, spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER))
    body.append(issue_error)
    if s.issued:
        i = s.issued
        body.append(ft.Container(ft.Column([
            text(f"{vm.TIER_NAMES.get(i['tier'], i['tier'])} · {i['period']} × {i['terms']} · "
                 f"{vm.cents(i['amount_minor'], i['currency'])} · expires {vm.short_date(i['expires_at'])}",
                 pal, size=13, weight=ft.FontWeight.W_600),
            ft.TextField(value=i["token"], read_only=True, multiline=True, min_lines=2, max_lines=4,
                         label="License key: send this to the customer", key="admin-issued-token"),
        ], spacing=6, horizontal_alignment=ft.CrossAxisAlignment.STRETCH), padding=12,
            border=ft.Border.all(1, pal.grid), border_radius=8, key="admin-issued"))

    verify_field = ft.TextField(label="Check a license key", multiline=True, max_lines=3, key="admin-verify-token",
                                expand=True)

    def do_verify(e: Any) -> None:
        begin(s)
        try:
            s.verified = s.client.verify_license(verify_field.value or "")
        except AdminError as exc:
            s.verified = {"signature_valid": False, "error": exc.message}
        redraw("licenses")

    body.append(ft.Row([verify_field, ft.OutlinedButton("Verify", on_click=do_verify, key="admin-verify")],
                       vertical_alignment=ft.CrossAxisAlignment.START))
    if s.verified is not None:
        v = s.verified
        if v.get("signature_valid"):
            detail = (f"{vm.TIER_NAMES.get(v.get('tier'), v.get('tier'))} · {v.get('period')} · "
                      f"{vm.cents(v.get('amount_minor'), v.get('currency') or 'USD')} · expires "
                      f"{vm.short_date(v.get('expires_at'))}" + (" (expired)" if v.get("expired") else "")
                      + ("" if v.get("in_ledger") else " · not in this server's ledger"))
            body.append(banner("warning" if v.get("expired") or not v.get("in_ledger") else "good",
                                      "Signature valid", detail, pal, key="admin-verify-result"))
        else:
            body.append(banner("serious", "Not valid", v.get("error") or "The signature doesn't match this "
                                      "server's key", pal, key="admin-verify-result"))

    body += section_error(ctx, s, "licenses")
    rows = s.data.get("licenses") or []
    if rows:
        body.append(table(["License", "Plan", "Amount", "Issued", "Expires", "Reference"], [
            [r["license_id"][:8], f"{vm.TIER_NAMES.get(r['tier'], r['tier'])} · {r['period']} × {r['terms']}",
             vm.cents(r["amount_minor"], r["currency"]) + (" (comp)" if r.get("complimentary") else ""),
             vm.short_date(r["issued_at"]), vm.short_date(r["expires_at"]), r.get("customer_ref") or "—"]
            for r in rows], key="admin-licenses"))
    return card(*body, pal=pal, title="Licenses", key="admin-license-card")


def revenue_card(ctx: ViewContext, s: vm.AdminSession, redraw: Any) -> ft.Control:
    r = s.data.get("revenue") or {}
    months = r.get("months") or []
    body = section_error(ctx, s, "revenue")
    body.append(text(f"{vm.cents(r.get('total_minor'), r.get('currency', 'USD'))} from {r.get('licenses', 0)} "
                     f"licenses in the last {len(months)} month(s) with sales", ctx.pal, size=14,
                     weight=ft.FontWeight.W_600))
    if months:
        body.append(table(["Month", "Licenses", "Total", "By plan"], [
            [m["month"], str(m["licenses"]), vm.cents(m["total_minor"], r.get("currency", "USD")),
             " · ".join(f"{vm.TIER_NAMES.get(t, t)} {v['licenses']} = {vm.cents(v['amount_minor'])}"
                        for t, v in sorted(m["tiers"].items()))] for m in months], key="admin-revenue"))
    return card(*body, pal=ctx.pal, title="Revenue (from issued licenses)", key="admin-revenue-card")


# --- telemetry -------------------------------------------------------------------------------------------


def telemetry_card(ctx: ViewContext, s: vm.AdminSession, redraw: Any) -> ft.Control:
    pal = ctx.pal
    data = s.data.get("issues") or {}
    counts = data.get("counts") or {}

    def set_filter(value: str) -> None:
        begin(s)
        s.issue_filter = value
        vm.load(s, ("issues",))
        redraw("telemetry")

    def triage(fp: str, resolve: bool) -> None:
        begin(s)
        try:
            s.client.triage(fp, resolve)
        except AdminError as exc:
            s.errors["issues"] = exc.message
        vm.load(s, ("issues", "overview", "audit"))
        redraw("telemetry", "overview", "audit")

    def toggle(fp: str) -> None:
        begin(s)
        if fp in s.expanded:
            s.expanded.pop(fp)
        else:
            try:
                s.expanded[fp] = s.client.issue_events(fp, 5)
            except AdminError as exc:
                s.errors["issues"] = exc.message
        redraw("telemetry")

    def purge(e: Any) -> None:
        def confirm(_: Any) -> None:
            ctx.close_dialog()
            begin(s)
            try:
                n = s.client.purge(90)
                s.notice = f"Deleted {n} crash reports older than 90 days."
            except AdminError as exc:
                s.errors["issues"] = exc.message
            vm.load(s, ("issues", "overview", "audit"))
            redraw("telemetry", "overview", "audit", "notice")

        ctx.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("Delete old crash reports?"),
            content=ft.Text("Reports older than 90 days are deleted from the server. Issues with no remaining "
                            "reports disappear. This can't be undone."),
            actions=[ft.TextButton("Cancel", on_click=lambda _: ctx.close_dialog()),
                     ft.FilledButton("Delete", on_click=confirm, key="admin-purge-confirm")], key="admin-purge-dialog"))

    body = section_error(ctx, s, "issues")
    body.append(ft.Row([
        ft.SegmentedButton(segments=[ft.Segment(value="open", label=ft.Text(f"Open ({counts.get('open_issues', 0)})")),
                                     ft.Segment(value="resolved",
                                                label=ft.Text(f"Resolved ({counts.get('resolved_issues', 0)})"))],
                           selected=[s.issue_filter], allow_empty_selection=False,
                           on_change=lambda e: set_filter(list(e.control.selected)[0]), key="admin-issue-filter"),
        ft.Container(expand=True),
        text(f"{counts.get('events_total', 0)} reports stored", pal, size=13, role="secondary"),
        ft.TextButton("Delete reports older than 90 days", on_click=purge, key="admin-purge",
                      visible=s.can("telemetry.triage")),
    ]))                                                    # no wrap: an expanding spacer can't live in a wrap
    issues = data.get("issues") or []
    if not issues:
        body.append(text("No issues here. Crash reports appear when users opt in to sending them.", pal, size=13,
                         role="secondary"))
    for i in issues:
        fp = i["fingerprint"]
        row = ft.Column([
            ft.Row([
                ft.Icon(ft.Icons.ERROR_OUTLINE if i.get("level") == "error" else ft.Icons.WARNING_AMBER, size=18,
                        color=pal.text_secondary),
                ft.Container(ft.Column([
                    text(f"{i['type']}: {i['message'][:160]}", pal, size=13, weight=ft.FontWeight.W_600),
                    text(f"{i['occurrences']} occurrences · {i['installs']} installs · last "
                         f"{vm.short_date(i['last_seen'])} · releases {', '.join(i.get('releases') or []) or '—'}",
                         pal, size=12, role="secondary"),
                ], spacing=2, tight=True), expand=True),
                ft.TextButton("Details", on_click=lambda e, fp=fp: toggle(fp), key=f"admin-issue-details-{fp[:12]}"),
                ft.TextButton("Resolve" if i.get("status", "open") == "open" else "Reopen",
                              on_click=lambda e, fp=fp, st=i.get("status", "open"): triage(fp, st == "open"),
                              key=f"admin-issue-triage-{fp[:12]}", visible=s.can("telemetry.triage")),
            ], vertical_alignment=ft.CrossAxisAlignment.START),
        ], spacing=4, key=f"admin-issue-{fp[:12]}")
        if fp in s.expanded:
            events = s.expanded[fp]
            frames = (events[0].get("frames") if events else None) or i.get("frames") or []
            lines = [f"{f.get('function', '?')} ({f.get('file', '?')}:{f.get('line', '?')})" for f in frames[-8:]]
            row.controls.append(ft.Container(ft.Column(
                [text(f"Latest report: release {events[0].get('release', '?')} · {events[0].get('platform', '?')} · "
                      f"{events[0].get('runtime', '?')}" if events else "No stored reports", pal, size=12,
                      role="secondary"),
                 ft.Text("\n".join(lines) or "No stack frames", font_family="monospace", size=12, selectable=True)],
                spacing=4), padding=ft.Padding.only(left=26), key=f"admin-issue-frames-{fp[:12]}"))
        body.append(row)
    return card(*body, pal=pal, title="Crash reports (scrubbed on devices)", key="admin-telemetry-card")


# --- health & audit --------------------------------------------------------------------------------------


_LEVEL_ICON = {"good": ft.Icons.CHECK_CIRCLE_OUTLINE, "warning": ft.Icons.WARNING_AMBER, "serious": ft.Icons.ERROR_OUTLINE}


def health_card(ctx: ViewContext, s: vm.AdminSession, redraw: Any) -> ft.Control:
    pal = ctx.pal
    body = section_error(ctx, s, "health")
    for label, value, level in vm.health_rows(s.data.get("health") or {}):
        body.append(ft.Row([ft.Icon(_LEVEL_ICON[level], size=16, color=pal.text_secondary),
                            ft.Container(text(label, pal, size=13, weight=ft.FontWeight.W_600), width=150),
                            ft.Container(text(value, pal, size=13, role="secondary", selectable=True), expand=True)],
                           key=f"admin-health-{label.split()[0].lower()}"))
    return card(*body, pal=pal, title="System health", key="admin-health-card")


def audit_card(ctx: ViewContext, s: vm.AdminSession, redraw: Any) -> ft.Control:
    entries = s.data.get("audit") or []
    body = section_error(ctx, s, "audit")
    if entries:
        body.append(table(["When", "Who", "Action", "Target"],
                           [[e["at"].replace("T", " ").rstrip("Z"), e["actor"], e["action"], (e.get("target") or "")[:24]]
                            for e in entries], key="admin-audit"))
    else:
        body.append(text("No admin actions yet.", ctx.pal, size=13, role="secondary"))
    return card(*body, pal=ctx.pal, title="Audit trail", key="admin-audit-card")


def table(columns: list[str], rows: list[list[str]], *, key: str) -> ft.Control:
    return ft.Row([ft.DataTable(columns=[ft.DataColumn(ft.Text(c)) for c in columns],
                                rows=[ft.DataRow(cells=[ft.DataCell(ft.Text(v, size=13)) for v in r]) for r in rows],
                                column_spacing=24, key=key)], scroll=ft.ScrollMode.AUTO)
