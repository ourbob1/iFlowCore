"""
Finances: import statements (CSV/PDF via mapping profiles), scan receipts and
ask tax questions (self-hosted AI, opt-in), recent ledger, tax set-asides.
Import is two-step — preview (with row errors) then commit — and scanned
receipts go through a review dialog, so nothing lands in the ledger unseen.
"""

from __future__ import annotations

from typing import Any

import flet as ft

from core.financial.ingestion import IngestionError
from ui.components.common import card, empty_state, primary_button, text
from ui.viewmodels.finances import FinancesData, commit_import, load_finances, preview_import, recognize
from ui.views.ai_cards import receipt_card, tax_question_card
from ui.views.base import ViewContext, page_column
from ui.views.finance_tools import export_card, guild_card


def build(ctx: ViewContext) -> ft.Control:
    s = ctx.state
    return render(ctx, load_finances(ctx.db, today=s.today(), currency=s.currency))


def render(ctx: ViewContext, data: FinancesData) -> ft.Control:
    pal = ctx.pal
    col = {"xs": 12, "lg": 6}
    from ui.views.integrations import integrations_card

    connections = integrations_card(ctx, data.platforms)
    controls: list[ft.Control] = [
        *([connections] if connections is not None else []),
        import_card(ctx, data),
        ft.ResponsiveRow([ft.Container(receipt_card(ctx), col=col), ft.Container(tax_question_card(ctx), col=col),
                          ft.Container(export_card(ctx), col=col), ft.Container(guild_card(ctx), col=col)],
                         spacing=12, run_spacing=12),
    ]

    if data.rows:
        if ctx.layout.size_class == "compact":
            body: list[ft.Control] = [
                ft.Row([ft.Column([text(r.payee, pal, size=14), text(f"{r.day:%b} {r.day.day} · {r.category}", pal,
                                                                        size=12, role="secondary")],
                                  spacing=2, tight=True, expand=True),
                        text(r.amount, pal, size=14, weight=ft.FontWeight.W_600)])
                for r in data.rows
            ]
        else:
            body = [ft.DataTable(
                columns=[ft.DataColumn(ft.Text(c)) for c in ("Date", "Payee", "Category", "Type")]
                + [ft.DataColumn(ft.Text("Amount"), numeric=True)],
                rows=[ft.DataRow(cells=[ft.DataCell(ft.Text(v)) for v in
                                        (r.day.isoformat(), r.payee, r.category, r.kind, r.amount)])
                      for r in data.rows],
                key="ledger-table",
            )]
        controls.append(card(*body, pal=pal, title="Last 30 days", key="ledger-card"))
    else:
        controls.append(empty_state("RECEIPT_LONG", "No activity in the last 30 days",
                                    "Import a platform statement to get started.", pal))

    for w in data.warnings:
        controls.append(text(w, pal, size=12, role="muted"))
    if data.set_asides:
        controls.append(card(*[text(f"{d} · {k} · {a}", pal, size=13) for d, k, a in data.set_asides],
                             pal=pal, title="Tax set-asides this year", key="set-asides"))
    return page_column(ctx, *controls, key="view-finances")


def import_card(ctx: ViewContext, data: FinancesData) -> ft.Container:
    pal, lay = ctx.pal, ctx.layout
    if not data.platforms:
        return card(text("Add a platform in Settings before importing statements.", pal, size=13, role="secondary"),
                    pal=pal, title="Import a statement", key="import-card")

    profile = ft.Dropdown(label="Statement format", value=data.profiles[0] if data.profiles else None,
                          options=[ft.DropdownOption(key=p, text=p) for p in data.profiles], width=280)
    platform = ft.Dropdown(label="Platform", value=data.platforms[0][0],
                           options=[ft.DropdownOption(key=i, text=n) for i, n in data.platforms], width=220)
    status = text("", pal, size=13, role="secondary")
    commit_btn = ft.FilledButton("Import", disabled=True, height=lay.min_target, key="btn-commit-import")
    state: dict[str, Any] = {"preview": None, "path": None, "recognized": None}

    def run_preview() -> None:
        path = state["path"]
        try:
            preview = preview_import(ctx.db, path, profile.value or "", tz=ctx.state.tz)
        except Exception as exc:                  # IngestionError, or a CSV fed to a PDF format…
            state["preview"] = None
            status.value = (f"Can't read that file: {exc}" if isinstance(exc, IngestionError)
                            else "Can't read that file with this format. Pick another format above.")
            commit_btn.disabled = True
        else:
            state["preview"] = preview
            errors = preview.result.errors[:3]
            head = f"{state['recognized']}\n" if state["recognized"] else ""
            status.value = head + preview.summary + "".join(f"\n• line {er.line}: {er.message}" for er in errors)
            commit_btn.disabled = not preview.result.records
        for control in (status, commit_btn, profile, platform):
            try:
                control.update()
            except RuntimeError:                # not mounted (tests)
                pass

    async def choose(e: Any) -> None:
        picker = ctx.services.get("file_picker")
        if picker is None:
            ctx.notify("File picking isn't available here")
            return
        files = await picker.pick_files(allowed_extensions=["csv", "pdf"], dialog_title="Choose a statement")
        if not files or not files[0].path:
            return
        load_file(files[0].path)

    def load_file(path: str) -> None:
        state["path"] = path
        found = recognize(ctx.db, path, data.platforms)
        state["recognized"] = None
        if found is not None:
            profile.value = found.profile
            if found.platform_id:
                platform.value = found.platform_id
            state["recognized"] = found.label
        elif path.lower().endswith(".csv"):
            state["preview"] = None
            status.value = "Format not recognized. Pick the statement format above to preview it."
            commit_btn.disabled = True
            for control in (status, commit_btn):
                try:
                    control.update()
                except RuntimeError:
                    pass
            return
        run_preview()

    def on_profile(e: Any) -> None:
        if state["path"]:
            state["recognized"] = None            # the user chose the format themselves
            run_preview()

    profile.on_select = on_profile
    ctx.services["import_load_file"] = load_file   # tests / drag-and-drop entry point

    def commit(e: Any) -> None:
        preview = state["preview"]
        if preview is None:
            return
        result = commit_import(ctx.db, preview, platform_id=platform.value or "", base_currency=ctx.state.currency)
        from ui.viewmodels.health import sync_now

        sync_now(ctx.state)                   # new payouts fund health premiums right away (Guild only)
        msg = f"Imported {result.inserted}" + (f", {result.duplicates} already there" if result.duplicates else "")
        ctx.notify(msg)
        ctx.refresh()

    commit_btn.on_click = commit
    return card(
        ft.Row([profile, platform], wrap=True, spacing=12),
        ft.Row([primary_button("Choose file…", choose, lay, icon="UPLOAD_FILE", key="btn-choose-file"), commit_btn],
               wrap=True, spacing=12),
        status,
        pal=pal, title="Import a statement", key="import-card",
    )

