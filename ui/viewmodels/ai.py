"""
AI service & diagnostics view-model: builds the client from settings, runs
receipt scans and tax questions, and uploads crash reports when allowed.

UI state for in-flight work lives in `services["ai_ui"]` (an `AIUiState`) so
background threads only update plain data and ask the shell to re-render;
they never touch Flet controls directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.ai.client import ENV_AI_TOKEN, AIServiceClient, AIServiceError
from core.ai.receipts import ConfirmedReceipt, ReceiptDraft, save_draft, scan_receipt
from core.ai.tax_assistant import ask, build_context
from core.financial.expenses import AutoTagger, ExpenseError, load_vendor_history
from core.financial.tax import estimate_for_year
from core.fleet.http import ProviderError
from core.licensing import Feature, FeatureLocked
from ui.state import AppState
from ui.viewmodels.dashboard import set_aside_totals


@dataclass
class AIUiState:
    busy: str | None = None                 # "receipt" | "tax" | None
    draft: ReceiptDraft | None = None
    draft_path: str | None = None
    answer: dict[str, Any] | None = None
    question: str = ""
    receipt_error: str | None = None
    tax_error: str | None = None
    status: dict[str, Any] | None = None    # last /v1/status result
    history: list[dict[str, Any]] = field(default_factory=list)   # recent Q&A, newest first, this session only


def ui_state(services: dict[str, Any]) -> AIUiState:
    return services.setdefault("ai_ui", AIUiState())


def service_url(state: AppState) -> str | None:
    return state.settings.ai_service_url or state.config.ai_service_url


def make_client(state: AppState, services: dict[str, Any]) -> AIServiceClient | None:
    """None when no URL/token is configured. Tests inject `services["ai_client"]`."""
    injected = services.get("ai_client")
    if injected is not None:
        return injected(state) if callable(injected) else injected
    url = service_url(state)
    token = state.store.secret("ai_service_token", ENV_AI_TOKEN) if state.store else None
    if not url or not token:
        return None
    return AIServiceClient(url, token, allow_http=not state.config.is_production)


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, FeatureLocked):
        return str(exc)
    if isinstance(exc, (AIServiceError, ExpenseError)):
        return str(exc)
    if isinstance(exc, ProviderError):
        return str(exc) or "The AI service couldn't be reached"
    return f"Something went wrong ({type(exc).__name__})"


# -- receipts ------------------------------------------------------------------------


def receipt_store_dir(state: AppState) -> Path:
    return state.config.data_dir / "receipts"


def run_scan(state: AppState, services: dict[str, Any], path: str) -> AIUiState:
    ui = ui_state(services)
    ui.busy, ui.receipt_error, ui.draft, ui.draft_path = "receipt", None, None, None
    try:
        state.require(Feature.RECEIPT_OCR)            # iFlow Pro
        client = make_client(state, services)
        if client is None:
            raise AIServiceError("Set up the AI service in Settings first")
        tagger = AutoTagger(vendor_history=load_vendor_history(state.db)) if state.db else None
        ui.draft = scan_receipt(client, path, default_currency=state.currency, today=state.today(),
                                locale=state.config.preferences.locale, tagger=tagger)
        ui.draft_path = path
    except Exception as exc:              # surfaced in the card; unexpected ones also reach the reporter
        ui.receipt_error = friendly_error(exc)
        if not isinstance(exc, (ProviderError, ExpenseError, FeatureLocked)):
            _report(services, exc)
    finally:
        ui.busy = None
    return ui


def confirm_scan(state: AppState, services: dict[str, Any], confirmed: ConfirmedReceipt) -> str:
    ui = ui_state(services)
    if ui.draft_path is None:
        raise ExpenseError("No scanned receipt to save")
    expense_id, _ = save_draft(state.db, confirmed, ui.draft_path, store_dir=receipt_store_dir(state), tz=state.tz,
                               base_currency=state.currency)
    ui.draft, ui.draft_path = None, None
    return expense_id


# -- tax questions -------------------------------------------------------------------


def tax_context(state: AppState) -> dict[str, Any]:
    today = state.today()
    estimate, summary = estimate_for_year(state.db, today.year, base_currency=state.currency, timezone=state.tz)
    reserved, paid = set_aside_totals(state.db, today.year)
    return build_context(estimate, summary, today=today, currency=state.currency, reserved_minor=reserved,
                         paid_minor=paid)


def run_question(state: AppState, services: dict[str, Any], question: str) -> AIUiState:
    ui = ui_state(services)
    ui.busy, ui.tax_error, ui.question = "tax", None, question
    try:
        client = make_client(state, services)
        if client is None:
            raise AIServiceError("Set up the AI service in Settings first")
        ui.answer = ask(client, question, tax_context(state))
        ui.history.insert(0, {"question": question, **ui.answer})
        del ui.history[5:]
    except Exception as exc:
        ui.tax_error = friendly_error(exc)
        if not isinstance(exc, ProviderError):
            _report(services, exc)
    finally:
        ui.busy = None
    return ui


# -- diagnostics ---------------------------------------------------------------------


def check_connection(state: AppState, services: dict[str, Any]) -> tuple[bool, str]:
    try:
        client = make_client(state, services)
        if client is None:
            return False, "Add the service URL and access token first"
        status = client.status()
    except Exception as exc:
        return False, friendly_error(exc)
    ui_state(services).status = status
    models = status.get("models") if isinstance(status.get("models"), dict) else {}
    parts = [f"{k}: {v}" for k, v in sorted(models.items())]
    return True, f"Connected to {client.host}" + (f" ({', '.join(parts)})" if parts else "")


def upload_reports(state: AppState, services: dict[str, Any]) -> int:
    """Send queued crash reports if the user opted in. Returns how many were sent."""
    reporter = services.get("reporter")
    if reporter is None or not state.settings.telemetry_enabled:
        return 0
    client = make_client(state, services)
    if client is None:
        return 0
    return reporter.flush(client.send_events)


def _report(services: dict[str, Any], exc: Exception) -> None:
    reporter = services.get("reporter")
    if reporter is not None:
        reporter.capture_exception(exc, handled=True, source="ai")
