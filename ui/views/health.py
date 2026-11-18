"""
iFlow Health (/health): coverage, the fractional withholding reserve, what each platform's payouts
contributed, and the guild catalog with eligibility pre-checks and enrollment.

Gated by iFlow Health & Guild. Everyone can browse sample guild tiers; enrolling, the reserve and
withholding need the tier. Existing plans and the reserve stay visible if the tier lapses.

Privacy on this page: the profile (state, birth year, member id) is sealed on the device; the
enrollment screen lists every field that will go to the brokerage before anything is sent; the
applicant's name, email and birth date are used once and never stored; no health questions exist.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import urlencode

import flet as ft

from core.financial.money import MoneyError, format_minor, parse_amount
from core.guild import PremiumError, end_plan, record_entry, save_plan
from core.health.brokers import Applicant, BrokerError, describe_payload, enrollment_payload
from core.health.enrollment import QuotedOffer, enroll, first_of_next_month
from core.health.models import CoverageTier, HealthError, Line, MemberProfile
from core.health.withholding import MODES, WithholdingError, WithholdingPolicy
from core.licensing import Feature
from core.timeutil import to_iso_utc
from ui.components.common import card, kpi_tile, meter, primary_button, status_banner, text
from ui.components.upsell import locked_card
from ui.format import money, percent_bp
from ui.theme import STATUS_COLOR
from ui.viewmodels.health import (
    MODE_LABELS,
    STATUS_LABELS,
    STATUS_LEVEL,
    HealthView,
    adapter,
    evidence_text,
    load_health,
    policy_summary,
    reserve_lines,
    save_policy,
    save_profile,
)
from ui.viewmodels.settings import list_platforms
from ui.views.base import ViewContext, page_column

LINE_ICON = {"health": "LOCAL_HOSPITAL_OUTLINED", "dental": "MEDICAL_SERVICES_OUTLINED", "life": "SHIELD_OUTLINED"}


def build(ctx: ViewContext) -> ft.Control:
    return render(ctx, load_health(ctx.state, ctx.services, ctx.params))


def render(ctx: ViewContext, v: HealthView) -> ft.Control:
    pal = ctx.pal
    controls: list[ft.Control] = []
    if not v.allowed:
        controls.append(locked_card(Feature.AHP_POOL, pal, ctx.navigate, title="iFlow Health", key="health-locked",
                                    extra="Group-rate health, dental and life plans through an independent "
                                          "association, funded by a small slice of each payout."))
    if v.coverage or v.allowed:
        controls.append(reserve_card(ctx, v))
        controls.append(coverage_card(ctx, v))
    if v.allowed and v.split:
        controls.append(split_card(ctx, v))
    controls.append(catalog_card(ctx, v))
    if v.allowed:
        controls.append(profile_card(ctx, v))
    for n in v.notes:
        controls.append(text(n, pal, size=12, role="muted"))
    controls.append(ft.Row([ft.Icon(ft.Icons.LOCK_OUTLINE, size=14, color=pal.text_muted),
                            ft.Container(text("No health questions, ever. Your profile stays sealed on this device; "
                                              "only what you approve on the enrollment screen goes to a brokerage. "
                                              + v.disclaimer, pal, size=12, role="muted"), expand=True)],
                           spacing=6, vertical_alignment=ft.CrossAxisAlignment.START, key="health-privacy"))
    return page_column(ctx, *controls, key="view-health")


# --- reserve -------------------------------------------------------------------------------------


def reserve_card(ctx: ViewContext, v: HealthView) -> ft.Container:
    pal, ccy, r = ctx.pal, ctx.state.currency, v.reserve
    body: list[ft.Control] = [
        ft.ResponsiveRow([
            kpi_tile("Premium reserve", money(max(0, r.balance_minor), ccy), pal,
                     sub="Your money, set aside on this device", col={"xs": 6, "md": 4}, key="kpi-reserve"),
            kpi_tile("This week", money(r.this_week_minor, ccy), pal,
                     sub=f"of about {money(r.weekly_need_minor, ccy)} needed" if r.weekly_need_minor else None,
                     col={"xs": 6, "md": 4}, key="kpi-week"),
            kpi_tile("Next premiums", money(r.premium_due_minor, ccy) if r.due_on else "—", pal,
                     sub=f"due {r.due_on:%b} {r.due_on.day}" if r.due_on else "no coverage yet",
                     col={"xs": 12, "md": 4}, key="kpi-due"),
        ], spacing=12, run_spacing=12),
    ]
    if r.premium_due_minor:
        bp = min(10_000, r.covered_minor * 10_000 // r.premium_due_minor)
        body.append(meter("Next premiums funded", bp, f"{money(r.covered_minor, ccy)} of "
                                                      f"{money(r.premium_due_minor, ccy)}", pal,
                          color=STATUS_COLOR["good" if bp >= 10_000 else "warning"], key="reserve-meter"))
    body += [text(line, pal, size=13, role="secondary") for line in reserve_lines(r, ccy)]
    body.append(ft.Container(text(policy_summary(v.policy, ccy), pal, size=13), key="policy-summary"))
    if v.sync and v.sync.withheld_minor:
        body.append(ft.Container(text(f"Just now: {money(v.sync.withheld_minor, ccy)} set aside from "
                                      f"{v.sync.withheld_count} payout(s).", pal, size=12, role="muted"),
                                 key="sync-note"))
    if v.allowed:
        body.append(ft.Row([
            ft.FilledButton("Withholding settings", icon=ft.Icons.TUNE, on_click=lambda e: open_policy_dialog(ctx, v),
                            height=ctx.layout.min_target, key="btn-policy"),
            ft.OutlinedButton("Premium paid…", on_click=lambda e: open_payment_dialog(ctx, v),
                              height=ctx.layout.min_target, key="btn-premium-paid",
                              disabled=not v.coverage),
        ], wrap=True, spacing=8))
    return card(*body, pal=pal, title="Fractional withholding", key="reserve-card")


def open_policy_dialog(ctx: ViewContext, v: HealthView) -> None:
    p = v.policy
    ccy = ctx.state.currency
    mode = ft.Dropdown(label="How much to set aside", value=p.mode, key="wp-mode",
                       options=[ft.DropdownOption(key=m, text=MODE_LABELS[m]) for m in MODES])
    rate = ft.TextField(label="Cents per dollar (percent mode)", value=f"{p.rate_bp / 100:g}" if p.rate_bp else "",
                        key="wp-rate", keyboard_type=ft.KeyboardType.NUMBER)
    per = ft.TextField(label=f"Amount per payout ({ccy})", key="wp-per", keyboard_type=ft.KeyboardType.NUMBER,
                       value=format_minor(p.per_payout_minor, ccy) if p.per_payout_minor else "")
    cap = ft.TextField(label="Never more than (% of a payout)", value=f"{p.cap_bp / 100:g}", key="wp-cap",
                       keyboard_type=ft.KeyboardType.NUMBER)
    platforms = list_platforms(ctx.db)
    boxes = [ft.Checkbox(label=f"Don't withhold from {name}", value=pid in p.excluded_platforms, data=pid,
                         key=f"wp-exclude-{pid}") for pid, name in platforms]

    def bp_of(raw: str | None) -> int:
        raw = (raw or "").strip().rstrip("%¢")
        return round(float(raw) * 100) if raw else 0

    def save(e: Any) -> None:
        try:
            new = WithholdingPolicy(
                mode=mode.value or "auto", rate_bp=bp_of(rate.value),
                per_payout_minor=parse_amount(per.value.strip(), ccy) if (per.value or "").strip() else 0,
                cap_bp=bp_of(cap.value) or p.cap_bp,
                excluded_platforms=tuple(b.data for b in boxes if b.value),
                start=p.start or ctx.state.today())
        except (WithholdingError, MoneyError, ValueError) as exc:
            ctx.notify(str(exc))
            return
        save_policy(ctx.state, new)
        ctx.close_dialog()
        ctx.notify("Withholding updated. It applies to payouts from now on.")
        ctx.refresh()

    ctx.show_dialog(ft.AlertDialog(
        modal=True, title=ft.Text("Withholding settings"), key="policy-dialog",
        content=ft.Column([mode, rate, per, cap, *boxes,
                           ft.Text("Money stays in your own accounts; this only earmarks it in iFlow Core. No "
                                   "platform is involved.", size=12)],
                          tight=True, spacing=10, width=400),
        actions=[ft.TextButton("Cancel", on_click=lambda e: ctx.close_dialog()),
                 ft.FilledButton("Save", on_click=save, key="wp-save")]))


def open_payment_dialog(ctx: ViewContext, v: HealthView) -> None:
    plans = [row.plan for row in v.coverage]
    if not plans:
        return
    ccy = plans[0].currency
    which = ft.Dropdown(label="Plan", value=plans[0].id, key="pay-plan",
                        options=[ft.DropdownOption(key=p.id, text=f"{Line(p.line).label}: {p.plan_name}") for p in plans])
    due = v.reserve.due_on or ctx.state.today()
    amount = ft.TextField(label=f"Amount ({ccy})", value=format_minor(plans[0].monthly_premium_minor, ccy),
                          key="pay-amount", keyboard_type=ft.KeyboardType.NUMBER)
    month = ft.TextField(label="Premium month (YYYY-MM)", value=due.strftime("%Y-%m"), key="pay-month")

    def pick(e: Any) -> None:
        p = next(p for p in plans if p.id == which.value)
        amount.value = format_minor(p.monthly_premium_minor, p.currency)

    which.on_change = pick

    def save(e: Any) -> None:
        try:
            record_entry(ctx.db, which.value or plans[0].id, kind="payment",
                         amount_minor=parse_amount((amount.value or "").strip(), ccy), on=ctx.state.today(),
                         covers_month=(month.value or "").strip())
        except (PremiumError, MoneyError, ValueError) as exc:
            ctx.notify(str(exc))
            return
        ctx.close_dialog()
        ctx.notify("Premium payment recorded")
        ctx.refresh()

    ctx.show_dialog(ft.AlertDialog(
        modal=True, title=ft.Text("Premium paid"), key="payment-dialog",
        content=ft.Column([which, amount, month], tight=True, spacing=10, width=360),
        actions=[ft.TextButton("Cancel", on_click=lambda e: ctx.close_dialog()),
                 ft.FilledButton("Save", on_click=save, key="pay-save")]))


# --- coverage ---------------------------------------------------------------------------------------


def coverage_card(ctx: ViewContext, v: HealthView) -> ft.Container:
    pal = ctx.pal
    body: list[ft.Control] = []
    if not v.coverage:
        body.append(text("No coverage yet. Browse the guild plans below, or add a plan you already have.", pal,
                         size=13, role="secondary"))
    for row in v.coverage:
        p = row.plan
        actions = []
        if v.allowed:
            actions.append(ft.TextButton("End", on_click=lambda e, p=p: confirm_end(ctx, p), key=f"end-{p.line}"))
        body.append(ft.Row([
            ft.Icon(getattr(ft.Icons, LINE_ICON[p.line]), color=pal.text_secondary),
            ft.Column([text(row.title, pal, size=14, weight=ft.FontWeight.W_600),
                       text(row.detail, pal, size=12, role="secondary"),
                       text(row.status + (f" · ref {p.enrollment_ref}" if p.enrollment_ref else ""), pal, size=12,
                            role="muted")], spacing=2, tight=True, expand=True),
            *actions,
        ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.START, key=f"coverage-{p.line}"))
    if v.allowed:
        body.append(ft.TextButton("Add a plan I already have", icon=ft.Icons.ADD,
                                  on_click=lambda e: open_manual_plan_dialog(ctx), key="btn-add-own-plan"))
    return card(*body, pal=pal, title="Your coverage", key="coverage-card")


def confirm_end(ctx: ViewContext, plan: Any) -> None:
    def go(e: Any) -> None:
        end_plan(ctx.db, plan.id, on=ctx.state.today())
        ctx.close_dialog()
        ctx.notify("Coverage marked as ended. Its history and reserve are kept.")
        ctx.refresh()

    ctx.show_dialog(ft.AlertDialog(
        modal=True, title=ft.Text(f"End {plan.plan_name}?"), key="end-dialog",
        content=ft.Text("This only updates iFlow Core. To cancel the coverage itself, contact the association or "
                        "carrier. Withholding stops funding this plan; its reserve history stays."),
        actions=[ft.TextButton("Keep", on_click=lambda e: ctx.close_dialog()),
                 ft.FilledButton("Mark as ended", on_click=go, key="end-confirm")]))


def open_manual_plan_dialog(ctx: ViewContext) -> None:
    ccy = ctx.state.currency
    line = ft.Dropdown(label="Coverage type", value="health", key="mp-line",
                       options=[ft.DropdownOption(key=ln.value, text=ln.label) for ln in Line])
    association = ft.TextField(label="Association or carrier", key="mp-association")
    name = ft.TextField(label="Plan name", key="mp-name")
    premium = ft.TextField(label=f"Monthly premium ({ccy})", key="mp-premium", keyboard_type=ft.KeyboardType.NUMBER)
    due = ft.TextField(label="Due day of month (1–28)", value="1", key="mp-due", keyboard_type=ft.KeyboardType.NUMBER)

    def save(e: Any) -> None:
        try:
            save_plan(ctx.db, association=association.value or "", plan_name=name.value or "",
                      monthly_premium_minor=parse_amount((premium.value or "").strip(), ccy), currency=ccy,
                      due_day=int((due.value or "1").strip()), coverage_start=ctx.state.today().replace(day=1),
                      line=line.value or "health")
        except (PremiumError, MoneyError, ValueError) as exc:
            ctx.notify(str(exc))
            return
        ctx.close_dialog()
        ctx.notify("Plan added")
        ctx.refresh()

    ctx.show_dialog(ft.AlertDialog(
        modal=True, title=ft.Text("Add a plan I already have"), key="manual-plan-dialog",
        content=ft.Column([line, association, name, premium, due], tight=True, spacing=10, width=380),
        actions=[ft.TextButton("Cancel", on_click=lambda e: ctx.close_dialog()),
                 ft.FilledButton("Save", on_click=save, key="mp-save")]))


# --- platform split ---------------------------------------------------------------------------------


def split_card(ctx: ViewContext, v: HealthView) -> ft.Container:
    pal, ccy = ctx.pal, ctx.state.currency
    table = ft.DataTable(
        columns=[ft.DataColumn(text(c, pal, size=13, role="secondary"), numeric=i > 0)
                 for i, c in enumerate(("Platform", "Payouts (net)", "Set aside", "Share"))],
        rows=[ft.DataRow(cells=[ft.DataCell(text(s.name, pal, size=13)),
                                ft.DataCell(text(money(s.net_minor, ccy), pal, size=13)),
                                ft.DataCell(text(money(s.withheld_minor, ccy), pal, size=13)),
                                ft.DataCell(text(percent_bp(s.effective_bp, decimals=1) if s.effective_bp is not None
                                                 else "—", pal, size=13))]) for s in v.split],
        key="split-table")
    return card(ft.Row([table], scroll=ft.ScrollMode.AUTO),
                text("Every platform's payouts fund the same reserve. Platforms never see it.", pal, size=12,
                     role="muted"), pal=pal, title="This month, by platform", key="split-card")


# --- catalog & enrollment -------------------------------------------------------------------------------


def catalog_card(ctx: ViewContext, v: HealthView) -> ft.Container:
    pal = ctx.pal

    def link(tier: str) -> str:
        return "/health?" + urlencode({"tier": tier})

    body: list[ft.Control] = [
        ft.Row([ft.Chip(label=ft.Text(t.label), selected=t is v.tier, show_checkmark=False, key=f"tier-{t.value}",
                        on_select=lambda e, t=t: ctx.navigate(link(t.value))) for t in CoverageTier],
               wrap=True, spacing=8, run_spacing=8),
    ]
    if v.catalog_note:
        body.append(text(v.catalog_note, pal, size=12, role="muted"))
    ev = evidence_text(v.evidence, ctx.state.currency)
    if ev:
        body.append(text(ev, pal, size=12, role="muted"))
    if v.catalog:
        for line in Line:
            quoted = v.catalog.by_line(line)
            if not quoted:
                continue
            body.append(text(line.label, pal, size=15, weight=ft.FontWeight.W_600))
            body.append(ft.ResponsiveRow([offer_tile(ctx, v, q) for q in quoted], spacing=12, run_spacing=12))
        if v.catalog.refused:
            body.append(ft.Container(text(f"{len(v.catalog.refused)} plan(s) hidden: sponsored or paid for by a gig "
                                          "platform, which would tie your coverage to that platform.", pal, size=12,
                                          role="muted"), key="refused-note"))
    return card(*body, pal=pal, title="Guild plans", key="catalog-card")


def offer_tile(ctx: ViewContext, v: HealthView, q: QuotedOffer) -> ft.Container:
    pal, o, el = ctx.pal, q.offer, q.eligibility
    body: list[ft.Control] = [
        text(o.guild_tier, pal, size=12, role="secondary"),
        text(f"{money(o.monthly_premium_minor, o.currency)} / month", pal, size=18, weight=ft.FontWeight.W_700),
        text(f"{o.carrier} via {o.association}", pal, size=12, role="muted"),
        *[text(f"• {s}", pal, size=12) for s in o.summary],
    ]
    if v.allowed and v.profile:
        body.append(status_banner(STATUS_LEVEL[el.status], STATUS_LABELS[el.status],
                                  " ".join(c.message for c in el.checks if c.outcome != "pass") or "All checks pass.",
                                  pal, key=f"elig-{o.offer_id}"))
        taken = any(r.plan.offer_id == o.offer_id for r in v.coverage)
        body.append(primary_button("Enrolled" if taken else "Enroll…", lambda e: open_enroll_dialog(ctx, v, q),
                                   ctx.layout, key=f"enroll-{o.offer_id}",
                                   disabled=taken or el.status == "not_eligible"))
    return card(*body, pal=pal, title=o.name, col={"xs": 12, "md": 6, "lg": 4}, key=f"offer-{o.offer_id}")


def open_enroll_dialog(ctx: ViewContext, v: HealthView, q: QuotedOffer) -> None:
    """Step 1: applicant details (used once, not stored). Step 2: consent screen with every field."""
    legal = ft.TextField(label="Legal name", key="en-name")
    email = ft.TextField(label="Email for the carrier", key="en-email", keyboard_type=ft.KeyboardType.EMAIL)
    dob = ft.TextField(label="Date of birth (YYYY-MM-DD)", key="en-dob")
    start = first_of_next_month(ctx.state.today())

    def review(e: Any) -> None:
        try:
            applicant = Applicant(legal.value or "", email.value or "", date.fromisoformat((dob.value or "").strip()))
            consented_at = to_iso_utc(ctx.state.now())
            payload = enrollment_payload(q.offer, v.profile, applicant, coverage_start=start, consented_at=consented_at)
        except (HealthError, ValueError) as exc:
            ctx.notify(str(exc))
            return
        ctx.close_dialog()
        open_consent_dialog(ctx, v, q, applicant, payload, consented_at, start)

    ctx.show_dialog(ft.AlertDialog(
        modal=True, title=ft.Text(f"Enroll in {q.offer.name}"), key="enroll-dialog",
        content=ft.Column([
            ft.Text(f"{money(q.offer.monthly_premium_minor, q.offer.currency)} / month · coverage starts "
                    f"{start:%b} {start.day}, {start.year}"),
            legal, email, dob,
            ft.Text("These are used for this enrollment only and aren't saved in iFlow Core. No health questions.",
                    size=12),
        ], tight=True, spacing=10, width=400),
        actions=[ft.TextButton("Cancel", on_click=lambda e: ctx.close_dialog()),
                 ft.FilledButton("Review what's sent", on_click=review, key="en-review")]))


def open_consent_dialog(ctx: ViewContext, v: HealthView, q: QuotedOffer, applicant: Applicant, payload: dict,
                        consented_at: str, start: date) -> None:
    agree = ft.Checkbox(label=f"Send these details to {adapter(ctx.state, ctx.services).info.name}", key="en-agree")
    rows = [ft.Row([ft.Container(ft.Text(label, size=12), width=150), ft.Text(value, size=12, selectable=True)])
            for label, value in describe_payload(payload)]

    def submit(e: Any) -> None:
        if not agree.value:
            ctx.notify("Tick the box to confirm what's sent")
            return
        try:
            enroll(ctx.db, adapter(ctx.state, ctx.services), q, v.profile, applicant, today=ctx.state.today(),
                   consented_at=consented_at, coverage_start=start)
        except (HealthError, BrokerError) as exc:
            ctx.notify(str(exc))
            return
        ctx.close_dialog()
        ctx.notify(f"Enrollment sent. {q.offer.name} starts {start:%b} {start.day}.")
        ctx.refresh()

    ctx.show_dialog(ft.AlertDialog(
        modal=True, title=ft.Text("Exactly what will be sent"), key="consent-dialog",
        content=ft.Column([*rows, ft.Text("Never sent: your earnings, your platforms, your location history.",
                                          size=12), agree], tight=True, spacing=6, width=440),
        actions=[ft.TextButton("Cancel", on_click=lambda e: ctx.close_dialog()),
                 ft.FilledButton("Send enrollment", on_click=submit, key="en-submit")]))


# --- profile --------------------------------------------------------------------------------------


def profile_card(ctx: ViewContext, v: HealthView) -> ft.Container:
    pal, p = ctx.pal, v.profile
    state = ft.TextField(label="State (e.g. FL)", value=p.state if p else "", max_length=2, key="pf-state",
                         capitalization=ft.TextCapitalization.CHARACTERS)
    year = ft.TextField(label="Birth year", value=str(p.birth_year) if p else "", key="pf-year",
                        keyboard_type=ft.KeyboardType.NUMBER)
    tier = ft.Dropdown(label="Who needs coverage", value=(p.coverage_tier.value if p else "self"), key="pf-tier",
                       options=[ft.DropdownOption(key=t.value, text=t.label) for t in CoverageTier])
    since = ft.TextField(label="Association member since (YYYY-MM-DD, optional)", key="pf-since",
                         value=p.association_member_since.isoformat() if p and p.association_member_since else "")
    member_id = ft.TextField(label="Association member ID (optional)", password=True, can_reveal_password=True,
                             value=(p.association_member_id or "") if p else "", key="pf-member")

    def save(e: Any) -> None:
        try:
            new = MemberProfile(state=state.value or "", birth_year=int((year.value or "0").strip()),
                                coverage_tier=CoverageTier(tier.value or "self"),
                                association_member_since=date.fromisoformat(since.value.strip())
                                if (since.value or "").strip() else None,
                                association_member_id=(member_id.value or "").strip() or None)
        except (HealthError, ValueError) as exc:
            ctx.notify(str(exc))
            return
        save_profile(ctx.state, new)
        ctx.notify("Saved on this device")
        ctx.refresh()

    return card(state, year, tier, since, member_id,
                text("Sealed in your encrypted database. Quotes use only your state, age band and who's covered.",
                     pal, size=12, role="muted"),
                ft.FilledButton("Save", on_click=save, height=ctx.layout.min_target, key="pf-save"),
                pal=pal, title="Your details", key="profile-card")

