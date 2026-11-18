"""
Finances tool cards: exports (plain CSV free; accountant formats Pro) and a
summary of iFlow Health (the full workflow is the /health view).

Gated cards never hide existing data: a lapsed Guild member still sees their
plans and reserve.
"""

from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import flet as ft

from core.financial.exports import ExportError
from core.licensing import Feature, FeatureLocked
from ui.components.common import card, primary_button, text
from ui.components.upsell import locked_card, locked_dialog, locked_row
from ui.viewmodels.licensing import EXPORT_FORMATS, export_filename, run_export
from ui.views.base import ViewContext

RANGES = {"ytd": "This year", "last_year": "Last year", "90d": "Last 90 days"}


def range_dates(key: str, today: date) -> tuple[date, date]:
    if key == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if key == "90d":
        return today - timedelta(days=89), today
    return date(today.year, 1, 1), today


# --- exports -------------------------------------------------------------------------------------


def export_card(ctx: ViewContext) -> ft.Container:
    pal, s = ctx.pal, ctx.state
    options = []
    for f in EXPORT_FORMATS.values():
        locked = not s.allows(f.feature)
        options.append(ft.DropdownOption(key=f.key, text=f"{f.label}{'  (Pro)' if locked else ''}"))
    fmt = ft.Dropdown(label="Format", value="csv", options=options, width=280, key="export-format")
    rng = ft.Dropdown(label="Period", value="ytd", width=180, key="export-range",
                      options=[ft.DropdownOption(key=k, text=v) for k, v in RANGES.items()])

    async def export(e: Any) -> None:
        f = EXPORT_FORMATS[fmt.value or "csv"]
        if not s.allows(f.feature):
            ctx.show_dialog(locked_dialog(f.feature, ctx.navigate, ctx.close_dialog,
                                          detail="Plain CSV exports stay free on every plan."))
            return
        start, end = range_dates(rng.value or "ytd", s.today())
        name = export_filename(f, start, end)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                report = run_export(s, f.key, Path(tmp) / name, start=start, end=end)
            except (ExportError, FeatureLocked, ValueError) as exc:
                ctx.notify(f"Export failed: {exc}")
                return
            data = report.path.read_bytes()
        picker = ctx.services.get("file_picker")
        if picker is None:
            ctx.notify("Saving files isn't available here")
            return
        target = await picker.save_file(dialog_title="Save export", file_name=name, src_bytes=data)
        if not target:                                      # cancelled (or a browser download already started)
            return
        if not Path(target).exists():                       # desktop returns a path; mobile saves the bytes itself
            Path(target).write_bytes(data)
        warn = f" ({len(report.warnings)} note(s))" if report.warnings else ""
        ctx.notify(f"Exported {report.entries} row(s) to {Path(target).name}{warn}")

    note = ("CSV of your ledger and mileage log is free on every plan: your data is always yours to take. "
            "Quicken, QuickBooks and OFX formats are part of iFlow Pro.")
    return card(ft.Row([fmt, rng], wrap=True, spacing=12),
                primary_button("Export…", export, ctx.layout, icon="DOWNLOAD", key="btn-export"),
                text(note, pal, size=12, role="muted"), pal=pal, title="Export", key="export-card")


# --- iFlow Health summary -------------------------------------------------------------------------


def guild_card(ctx: ViewContext) -> ft.Container:
    """Finances summary of iFlow Health; the full workflow lives at /health."""
    from core.guild import active_plans
    from core.health.withholding import reserve_status
    from ui.format import money

    s, pal = ctx.state, ctx.pal
    plans = active_plans(ctx.db, include_pending=True)
    if not plans and not s.allows(Feature.AHP_POOL):
        return locked_card(Feature.AHP_POOL, pal, ctx.navigate, title="Health & Guild", key="guild-card",
                           extra="Set aside a slice of each payout so your Association Health Plan premium is "
                                 "covered by its due date.")
    body: list[ft.Control] = []
    if plans:
        r = reserve_status(ctx.db, today=s.today(), currency=s.currency)
        body.append(text(" · ".join(f"{p.line.capitalize()}: {p.plan_name}" for p in plans), pal, size=13,
                         role="secondary"))
        body.append(text(f"Premium reserve {money(max(0, r.balance_minor), s.currency)}"
                         + (f" · next premiums {money(r.premium_due_minor, s.currency)} due {r.due_on:%b} "
                            f"{r.due_on.day}" if r.due_on else ""), pal, size=13))
    else:
        body.append(text("Group health, dental and life plans, funded by a small slice of each payout.", pal,
                         size=13, role="secondary"))
    if not s.allows(Feature.AHP_POOL):
        body.append(locked_row(Feature.AHP_POOL, pal, ctx.navigate, key="guild-locked"))
    body.append(ft.TextButton("Open iFlow Health", icon=ft.Icons.FAVORITE_BORDER,
                              on_click=lambda e: ctx.navigate("/health"), key="btn-open-health"))
    return card(*body, pal=pal, title="Health & Guild", key="guild-card")
