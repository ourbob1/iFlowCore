"""
Finances cards backed by the self-hosted AI service: receipt scanning (with a
review dialog; nothing is saved unconfirmed) and tax questions grounded in the
app's own estimate.

Work runs on `run_background`; results land in `AIUiState` and the view
re-renders, so these builders are pure functions of state.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import flet as ft

from core.ai.receipts import ConfirmedReceipt, ReceiptDraft
from core.config import ConfigError, normalize_currency
from core.financial.expenses import EXPENSE_CATEGORIES, ExpenseError
from core.financial.money import MoneyError, format_minor, parse_amount
from core.licensing import Feature
from ui.components.common import card, primary_button, status_banner, text
from ui.components.upsell import locked_card
from ui.viewmodels.ai import AIUiState, confirm_scan, friendly_error, run_question, run_scan, service_url, ui_state
from ui.views.base import ViewContext

SUGGESTED_QUESTIONS = (
    "How much should I set aside each month?",
    "When is my next estimated payment due, and how much?",
    "Why is my self-employment tax this high?",
)
CATEGORY_LABEL = {c: c.replace("_", " ").capitalize() for c in EXPENSE_CATEGORIES}


def _setup_hint(ctx: ViewContext, what: str, key: str, title: str) -> ft.Container:
    return card(text(f"{what} uses your own AI server. Turn it on in Settings → AI service & diagnostics.", ctx.pal,
                     size=13, role="secondary"),
                ft.TextButton("Open Settings", on_click=lambda e: ctx.navigate("/settings")),
                pal=ctx.pal, title=title, key=key)


def _run(ctx: ViewContext, work: Any) -> None:
    runner = ctx.services.get("run_background")
    if runner:
        runner(work)
    else:
        work()


# -- receipts ---------------------------------------------------------------------------


def receipt_card(ctx: ViewContext) -> ft.Container:
    s, pal, lay = ctx.state, ctx.pal, ctx.layout
    if not s.allows(Feature.RECEIPT_OCR):                  # iFlow Pro
        return locked_card(Feature.RECEIPT_OCR, pal, ctx.navigate, title="Scan a receipt", key="receipt-card",
                           extra="Attaching receipts to expenses by hand is free on every plan.")
    if not (s.settings.ai_receipts_enabled and service_url(s)):
        return _setup_hint(ctx, "Receipt scanning", "receipt-card", "Scan a receipt")
    ui = ui_state(ctx.services)
    busy = ui.busy == "receipt"

    async def choose(e: Any) -> None:
        picker = ctx.services.get("file_picker")
        if picker is None:
            ctx.notify("File picking isn't available here")
            return
        files = await picker.pick_files(allowed_extensions=["jpg", "jpeg", "png", "webp", "heic", "pdf"],
                                        dialog_title="Choose a receipt photo or PDF")
        if not files or not files[0].path:
            return
        start_scan(ctx, files[0].path)

    controls: list[ft.Control] = [
        text(f"Photos and PDFs are sent to {_host(service_url(s))}, read in memory, and not stored there. "
             "You review everything before it's saved.", pal, size=12, role="muted"),
        primary_button("Reading receipt…" if busy else "Scan a receipt…", choose, lay, icon="DOCUMENT_SCANNER",
                       key="btn-scan-receipt", disabled=busy),
    ]
    if busy:
        controls.append(ft.ProgressBar(key="scan-progress"))
    if ui.receipt_error and not busy:
        controls.append(status_banner("serious", "Scan failed", ui.receipt_error, pal, key="scan-error"))
    if ui.draft is not None and not busy:
        controls.append(ft.TextButton("Review scanned receipt", on_click=lambda e: open_review(ctx),
                                      key="btn-review-receipt"))
    return card(*controls, pal=pal, title="Scan a receipt", key="receipt-card")


def _host(url: str | None) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url or "").hostname or "your server"


def start_scan(ctx: ViewContext, path: str) -> None:
    ui = ui_state(ctx.services)
    ui.busy = "receipt"
    ctx.refresh()

    def work() -> None:
        run_scan(ctx.state, ctx.services, path)
        ctx.refresh()
        if ui.draft is not None:
            open_review(ctx)

    _run(ctx, work)


def open_review(ctx: ViewContext) -> None:
    ui = ui_state(ctx.services)
    if ui.draft is not None:
        ctx.show_dialog(review_dialog(ctx, ui.draft, ui))


def review_dialog(ctx: ViewContext, draft: ReceiptDraft, ui: AIUiState) -> ft.AlertDialog:
    pal = ctx.pal
    low = set(draft.needs_attention)

    def hint(field: str) -> str | None:
        return "Check this — low confidence" if field in low else None

    vendor = ft.TextField(label="Vendor", value=draft.vendor or "", helper=hint("vendor"), key="rv-vendor")
    day = ft.TextField(label="Date (YYYY-MM-DD)", value=(draft.occurred_on or ctx.state.today()).isoformat(),
                       helper=hint("date"), key="rv-date")
    amount = ft.TextField(label="Total", key="rv-amount", helper=hint("total"),
                          value=format_minor(draft.total_minor, draft.currency) if draft.total_minor is not None else "",
                          keyboard_type=ft.KeyboardType.NUMBER)
    currency = ft.TextField(label="Currency", value=draft.currency, max_length=3, key="rv-currency",
                            capitalization=ft.TextCapitalization.CHARACTERS)
    category = ft.Dropdown(label="Category", value=draft.category.category, key="rv-category",
                           options=[ft.DropdownOption(key=c, text=CATEGORY_LABEL[c]) for c in EXPENSE_CATEGORIES])
    business = ft.TextField(label="Business use %", value="100", keyboard_type=ft.KeyboardType.NUMBER,
                            key="rv-business")
    why = (f"Suggested from '{draft.category.matched_keyword}'" if draft.category.matched_keyword
           else "No category match; pick one")
    extras: list[ft.Control] = [text(why, pal, size=12, role="muted")]
    for w in draft.warnings:
        extras.append(text(f"⚠ {w}", pal, size=12, role="secondary"))
    if draft.items:
        extras.append(text(f"{len(draft.items)} line item(s) read", pal, size=12, role="muted"))

    def save(e: Any) -> None:
        try:
            ccy = normalize_currency(currency.value or "")
            confirmed = ConfirmedReceipt(
                amount_minor=parse_amount((amount.value or "").strip(), ccy),
                currency=ccy,
                occurred_on=date.fromisoformat((day.value or "").strip()),
                vendor=(vendor.value or "").strip() or None,
                category=category.value or "other",
                business_use_bp=_percent_bp(business.value),
            )
            confirm_scan(ctx.state, ctx.services, confirmed)
        except (ConfigError, MoneyError, ValueError, ExpenseError) as exc:
            ctx.notify(friendly_error(exc) if isinstance(exc, ExpenseError) else str(exc))
            return
        ctx.close_dialog()
        ctx.notify("Expense saved with its receipt")
        ctx.refresh()

    def discard(e: Any) -> None:
        ui.draft, ui.draft_path = None, None
        ctx.close_dialog()
        ctx.refresh()

    return ft.AlertDialog(
        modal=True, title=ft.Text("Review receipt"), key="receipt-review",
        content=ft.Column([vendor, ft.Row([day, currency], wrap=True), amount, category, business, *extras],
                          tight=True, scroll=ft.ScrollMode.AUTO, spacing=10, width=420),
        actions=[ft.TextButton("Discard", on_click=discard, key="rv-discard"),
                 ft.FilledButton("Save expense", on_click=save, key="rv-save")],
    )


def _percent_bp(raw: str | None) -> int:
    value = (raw or "100").strip().rstrip("%")
    try:
        pct = float(value)
    except ValueError as exc:
        raise ValueError("Business use must be a number from 0 to 100") from exc
    if not 0 < pct <= 100:
        raise ValueError("Business use must be a number from 0 to 100")
    return round(pct * 100)


# -- tax questions ---------------------------------------------------------------------


def tax_question_card(ctx: ViewContext) -> ft.Container:
    s, pal, lay = ctx.state, ctx.pal, ctx.layout
    if not (s.settings.ai_tax_enabled and service_url(s)):
        return _setup_hint(ctx, "Tax Q&A", "tax-ask-card", "Ask about your taxes")
    ui = ui_state(ctx.services)
    busy = ui.busy == "tax"
    question = ft.TextField(label="Your question", value=ui.question, max_length=500, multiline=True, min_lines=1,
                            max_lines=4, key="tax-question", disabled=busy)

    def submit(value: str) -> None:
        if not value.strip():
            ctx.notify("Type a question first")
            return
        ui.busy, ui.question = "tax", value.strip()
        ctx.refresh()

        def work() -> None:
            run_question(ctx.state, ctx.services, value)
            ctx.refresh()

        _run(ctx, work)

    chips = [ft.Chip(label=ft.Text(q), on_click=lambda e, q=q: submit(q), disabled=busy) for q in SUGGESTED_QUESTIONS]
    controls: list[ft.Control] = [
        question,
        ft.Row(chips, wrap=True, spacing=8, run_spacing=8),
        primary_button("Thinking…" if busy else "Ask", lambda e: submit(question.value or ""), lay,
                       icon="QUESTION_ANSWER", key="btn-ask-tax", disabled=busy),
    ]
    if busy:
        controls.append(ft.ProgressBar(key="tax-progress"))
    if ui.tax_error and not busy:
        controls.append(status_banner("serious", "Couldn't answer", ui.tax_error, pal, key="tax-error"))
    if ui.answer and not busy:
        controls.append(ft.Container(text(ui.answer["answer"], pal, size=14, selectable=True), key="tax-answer"))
        if ui.answer.get("source_label"):
            icon = ft.Icons.CALCULATE if ui.answer.get("source") == "rules" else ft.Icons.AUTO_AWESOME
            controls.append(ft.Row([ft.Icon(icon, size=16, color=pal.text_secondary),
                                    text(ui.answer["source_label"], pal, size=12, role="secondary")],
                                   spacing=6, key="tax-answer-source"))
        controls.append(text(ui.answer["disclaimer"], pal, size=12, role="muted"))
    controls.append(text("Only your yearly totals from this app are sent (no transactions, vendors or places).",
                         pal, size=12, role="muted"))
    return card(*controls, pal=pal, title="Ask about your taxes", key="tax-ask-card")
