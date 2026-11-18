"""App side of the self-hosted AI service: client, receipt drafts, tax context, UI wiring. No network."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from core.ai.client import AIServiceBusy, AIServiceClient, AIServiceError, validate_service_url
from core.ai.receipts import ConfirmedReceipt, draft_from_response, read_receipt_file, save_draft, scan_receipt
from core.ai.tax_assistant import DISCLAIMER, ask, build_context
from core.config import ConfigError, load_config
from core.financial.expenses import ExpenseError, list_receipts
from core.financial.tax import TaxEstimator, TaxInputs, YearSummary
from core.fleet.http import HttpResponse, ProviderAuthError
from ui.app import IFlowCoreApp
from ui.viewmodels import ai as ai_vm

from .ui_helpers import FakePage, find_key, texts

TOKEN = "t" * 40
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
TODAY = date(2026, 9, 26)
NOW = datetime(2026, 9, 26, 21, 30, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def ok(payload, status=200):
    return HttpResponse(status, json.dumps(payload).encode())


def client(responses, **kw):
    t = FakeTransport(responses)
    return AIServiceClient("https://ai.example.com/", TOKEN, transport=t, sleep=lambda s: None, **kw), t


# --- URL & client -----------------------------------------------------------------------------


@pytest.mark.parametrize("url,allow_http,expected", [
    ("https://ai.example.com/", False, "https://ai.example.com"),
    ("http://localhost:8080", False, "http://localhost:8080"),
    ("http://127.0.0.1:8080", False, "http://127.0.0.1:8080"),
    ("http://192.168.1.20:8080", True, "http://192.168.1.20:8080"),
])
def test_validate_service_url_ok(url, allow_http, expected):
    assert validate_service_url(url, allow_http=allow_http) == expected


@pytest.mark.parametrize("url", ["http://ai.example.com", "ftp://x", "https://user:pw@ai.example.com",
                                 "https://ai.example.com/?k=1", "not a url", ""])
def test_validate_service_url_rejects(url):
    with pytest.raises(AIServiceError):
        validate_service_url(url)


def test_config_ai_url(tmp_path):
    cfg = load_config({"IFLOW_DATA_DIR": str(tmp_path), "IFLOW_AI_URL": "https://ai.example.com/"})
    assert cfg.ai_service_url == "https://ai.example.com"
    with pytest.raises(ConfigError):
        load_config({"IFLOW_DATA_DIR": str(tmp_path), "IFLOW_ENV": "production",
                     "IFLOW_AI_URL": "http://ai.example.com"})


def test_token_required_and_hidden():
    with pytest.raises(AIServiceError):
        AIServiceClient("https://ai.example.com", " ")
    c, _ = client([])
    assert TOKEN not in repr(c)


def test_extract_receipt_sends_raw_body_with_auth():
    c, t = client([ok({"total": "12.34"})])
    assert c.extract_receipt(JPEG, "image/jpeg", currency_hint="USD", locale="en_US") == {"total": "12.34"}
    method, url, kw = t.calls[0]
    assert (method, url) == ("POST", "https://ai.example.com/v1/receipts/extract")
    assert kw["body"] == JPEG and kw["headers"]["Content-Type"] == "image/jpeg"
    assert kw["headers"]["Authorization"] == f"Bearer {TOKEN}" and kw["headers"]["X-Currency-Hint"] == "USD"
    assert kw["timeout"] >= 60


def test_error_mapping():
    c, _ = client([HttpResponse(401, b"")])
    with pytest.raises(ProviderAuthError):
        c.status()
    c, _ = client([ok({"detail": "loading"}, 503)] * 2)
    with pytest.raises(AIServiceBusy):
        c.status()
    c, _ = client([ok({"detail": "File is larger than 25 MB"}, 413)])
    with pytest.raises(AIServiceError, match="25 MB"):
        c.extract_receipt(JPEG, "image/jpeg")
    c, _ = client([HttpResponse(200, b"<html>")])
    with pytest.raises(AIServiceError):
        c.status()


def test_ask_tax_validates_question():
    c, t = client([ok({"answer": " Set aside $500. ", "model": "m"})])
    with pytest.raises(AIServiceError):
        c.ask_tax("   ", {})
    with pytest.raises(AIServiceError):
        c.ask_tax("x" * 501, {})
    result = ask(c, "How much?", {"net_profit": "1 USD"})
    assert result["answer"] == "Set aside $500." and result["disclaimer"] == DISCLAIMER
    assert result["source"] == "ai" and "double-check" in result["source_label"]
    sent = json.loads(t.calls[0][2]["body"])
    assert sent == {"question": "How much?", "context": {"net_profit": "1 USD"}}


def test_send_events_returns_accepted():
    c, _ = client([ok({"accepted": 2, "new": 1})])
    assert c.send_events([{}, {}]) == 2


# --- receipt drafts ----------------------------------------------------------------------------


def test_draft_happy_path():
    draft = draft_from_response({
        "vendor": "  Shell   Oil ", "date": "2026-09-20", "currency": "usd", "subtotal": "40.00", "tax": "3.20",
        "total": "43.20", "items": [{"description": "Unleaded", "amount": "40.00"}, {"description": "", "amount": "1"}],
        "confidence": {"vendor": "high", "date": "high", "total": "high", "bogus": "extreme"}, "model": "qwen",
    }, default_currency="USD", today=TODAY)
    assert (draft.vendor, draft.occurred_on, draft.total_minor, draft.tax_minor) == ("Shell Oil", date(2026, 9, 20),
                                                                                     4320, 320)
    assert draft.category.category == "fuel" and len(draft.items) == 1
    assert draft.warnings == () and draft.needs_attention == () and draft.confidence["bogus"] == "low"


def test_draft_warnings_and_fallbacks():
    draft = draft_from_response({"subtotal": "10.00", "tax": "0.80", "date": "2031-01-01", "currency": "XYZ"},
                                default_currency="USD", today=TODAY)
    assert draft.total_minor == 1080 and draft.currency == "USD"
    assert any("subtotal + tax" in w for w in draft.warnings)
    assert any("future" in w for w in draft.warnings)
    assert any("XYZ" in w for w in draft.warnings)
    assert set(draft.needs_attention) == {"vendor", "date", "total"}          # no confidence sent → all low


def test_draft_mismatch_and_bad_values():
    draft = draft_from_response({"total": "50.00", "subtotal": "40.00", "tax": "3.00", "date": "yesterday",
                                 "vendor": "", "items": "nope"}, default_currency="USD", today=TODAY)
    assert draft.total_minor == 5000 and draft.occurred_on is None
    assert any("doesn't match" in w for w in draft.warnings)
    assert "date" in draft.needs_attention and "vendor" in draft.needs_attention


def test_draft_foreign_currency_and_jpy():
    draft = draft_from_response({"total": "1,200", "currency": "JPY"}, default_currency="USD", today=TODAY)
    assert draft.total_minor == 1200 and draft.currency == "JPY"
    assert any("not your base currency" in w for w in draft.warnings)


def test_read_receipt_file_checks_type(tmp_path):
    fake = tmp_path / "evil.jpg"
    fake.write_bytes(b"MZ\x90\x00 not a jpeg")
    with pytest.raises(ExpenseError):
        read_receipt_file(fake)
    good = tmp_path / "r.jpg"
    good.write_bytes(JPEG)
    assert read_receipt_file(good) == (JPEG, "image/jpeg")


def test_scan_and_save(db, tmp_path):
    photo = tmp_path / "receipt.jpg"
    photo.write_bytes(JPEG)
    c, t = client([ok({"vendor": "Jiffy Lube", "date": "2026-09-01", "total": "89.99", "currency": "USD"})])
    draft = scan_receipt(c, photo, default_currency="USD", today=TODAY)
    assert draft.category.category == "maintenance"
    confirmed = ConfirmedReceipt(amount_minor=draft.total_minor, currency="USD", occurred_on=draft.occurred_on,
                                 vendor=draft.vendor, category=draft.category.category)
    expense_id, info = save_draft(db, confirmed, photo, store_dir=tmp_path / "store", tz="America/New_York",
                                  base_currency="USD")
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    assert (row["amount_minor"], row["category"], row["base_amount_minor"]) == (8999, "maintenance", 8999)
    assert row["occurred_at"] == "2026-09-01T16:00:00.000Z"            # local noon in New York
    assert list_receipts(db, expense_id=expense_id)[0]["sha256"] == info.sha256


def test_save_draft_rolls_back_when_file_vanishes(db, tmp_path):
    photo = tmp_path / "gone.jpg"
    confirmed = ConfirmedReceipt(amount_minor=100, currency="USD", occurred_on=TODAY, vendor="x", category="other")
    with pytest.raises(ExpenseError):
        save_draft(db, confirmed, photo, store_dir=tmp_path, tz="UTC", base_currency="USD")
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM expenses WHERE deleted_at IS NULL").fetchone()[0] == 0


# --- tax context -------------------------------------------------------------------------------


def test_tax_context_has_only_aggregates():
    est = TaxEstimator().estimate(TaxInputs(tax_year=2026, gross_receipts=5_000_000, deductible_expenses=500_000,
                                            region="US-FL"))
    summary = YearSummary(2026, "USD", gross_receipts=5_000_000, deductible_expenses=500_000, business_meters=1_609_344)
    ctx = build_context(est, summary, today=TODAY, currency="USD", reserved_minor=100_000, paid_minor=0)
    assert ctx["gross_receipts"] == "50,000.00 USD" and ctx["state"] == "FL" and ctx["business_miles"] == "1,000"
    assert ctx["tax_reserve_set_aside"] == "1,000.00 USD"
    assert [q["status"] for q in ctx["quarterly_payments"]][:2] == ["past", "past"]
    # Arithmetic is done here, not by the model: 8,579.50 total − 1,000.00 reserved, over Sep–Dec (rounded up).
    assert ctx["total_estimated_tax"] == "8,579.50 USD"
    assert (ctx["still_to_set_aside"], ctx["months_left_in_tax_year"], ctx["set_aside_per_month_to_catch_up"]) == (
        "7,579.50 USD", 4, "1,894.88 USD")
    assert ctx["next_estimated_payment"]["due"] == "2027-01-15"
    for forbidden in ("vendor", "lat", "lng", "home", "occurred_at", "description", "transactions"):
        assert forbidden not in ctx


# --- UI wiring ---------------------------------------------------------------------------------


class StubClient:
    host = "ai.example.com"

    def __init__(self):
        self.scans = 0
        self.questions = []
        self.events = []

    def extract_receipt(self, content, mime, **kw):
        self.scans += 1
        return {"vendor": "Chevron", "date": "2026-09-25", "total": "45.10", "currency": "USD",
                "confidence": {"vendor": "high", "date": "high", "total": "high"}}

    def ask_tax(self, question, context):
        self.questions.append((question, context))
        return {"answer": "About $400 a month.", "model": "stub"}

    def status(self):
        return {"models": {"llm": "ready", "ocr": "ready"}}

    def send_events(self, events):
        self.events.extend(events)
        return len(events)


def make_app(tmp_path, stub, pro=True, **settings):
    page = FakePage(1280, "linux", "/")
    app = IFlowCoreApp(page, env={"IFLOW_DATA_DIR": str(tmp_path)}, clock=lambda: NOW,
                      services={"motion": None, "ai_client": stub})
    app.services.pop("motion")
    app.start()
    if pro:                                   # receipt scanning is part of iFlow Pro (Phase 7)
        from core.licensing import Period, Tier, plan

        app.state.license_manager().simulate_purchase(plan(Tier.PRO, Period.MONTHLY), app.state.today())
    if settings:
        app.state.update_settings(**settings)
    return app, page


def test_receipt_scanning_is_locked_on_solo_core(tmp_path):
    stub = StubClient()
    app, page = make_app(tmp_path, stub, pro=False, ai_service_url="https://ai.example.com", ai_receipts_enabled=True)
    app.navigate("/finances")
    assert find_key(page.views[-1], "receipt-card-lock") is not None
    assert find_key(page.views[-1], "btn-scan-receipt") is None
    photo = tmp_path / "r.jpg"
    photo.write_bytes(JPEG)
    ui = ai_vm.run_scan(app.state, app.services, str(photo))            # the view-model gate holds on its own
    assert stub.scans == 0 and "iFlow Pro" in ui.receipt_error and app.reporter.pending() == []


def test_finances_shows_setup_hint_until_enabled(tmp_path):
    app, page = make_app(tmp_path, StubClient())
    app.navigate("/finances")
    assert any("Turn it on in Settings" in t for t in texts(find_key(page.views[-1], "receipt-card")))
    assert find_key(page.views[-1], "btn-scan-receipt") is None


def test_scan_review_and_save(tmp_path):
    stub = StubClient()
    app, page = make_app(tmp_path, stub, ai_service_url="https://ai.example.com", ai_receipts_enabled=True)
    app.navigate("/finances")
    assert find_key(page.views[-1], "btn-scan-receipt") is not None
    photo = tmp_path / "r.jpg"
    photo.write_bytes(JPEG)

    from ui.views import ai_cards

    ctx = app_ctx(app)
    ai_cards.start_scan(ctx, str(photo))                         # FakePage runs background work inline
    dialog = page.dialogs[-1]
    assert find_key(dialog, "rv-vendor").value == "Chevron" and find_key(dialog, "rv-amount").value == "45.10"
    assert find_key(dialog, "rv-category").value == "fuel"
    find_key(dialog, "rv-save").on_click(None)
    with app.state.db.connection() as conn:
        row = conn.execute("SELECT amount_minor, category, vendor FROM expenses").fetchone()
    assert tuple(row) == (4510, "fuel", "Chevron")
    assert (tmp_path / "receipts").is_dir()


def test_scan_error_shows_banner(tmp_path):
    class Failing(StubClient):
        def extract_receipt(self, *a, **k):
            raise AIServiceBusy("Server is busy")

    app, page = make_app(tmp_path, Failing(), ai_service_url="https://ai.example.com", ai_receipts_enabled=True)
    photo = tmp_path / "r.jpg"
    photo.write_bytes(JPEG)
    from ui.views import ai_cards

    ai_cards.start_scan(app_ctx(app), str(photo))
    app.navigate("/finances")
    assert "Server is busy" in " ".join(texts(find_key(page.views[-1], "scan-error")))
    assert app.reporter.pending() == []                          # expected service errors aren't crash reports


def test_tax_question_flow(tmp_path):
    stub = StubClient()
    app, page = make_app(tmp_path, stub, ai_service_url="https://ai.example.com", ai_tax_enabled=True)
    ai_vm.run_question(app.state, app.services, "How much should I save?")
    app.navigate("/finances")
    answer = find_key(page.views[-1], "tax-answer")
    assert "About $400 a month." in texts(answer)
    assert "double-check" in " ".join(texts(find_key(page.views[-1], "tax-answer-source")))
    question, context = stub.questions[0]
    assert context["tax_year"] == 2026 and "net_profit" in context


def test_settings_card_saves_and_validates(tmp_path):
    app, page = make_app(tmp_path, StubClient())
    app.navigate("/settings")
    find_key(page.views[-1], "btn-ai-expand").on_click(None)          # collapsed until asked for
    card = find_key(page.views[-1], "ai-card")
    assert find_key(card, "btn-send-reports") is not None
    find_key(card, "ai-url").value = "ftp://ai.example.com"
    find_key(card, "ai-receipts").value = True
    find_key(card, "save-ai").on_click(None)
    assert app.state.settings.ai_receipts_enabled is False       # invalid URL: nothing saved
    find_key(card, "ai-url").value = ""
    find_key(card, "save-ai").on_click(None)
    assert app.state.settings.ai_receipts_enabled is False       # features need a URL
    app.navigate("/settings")
    card = find_key(page.views[-1], "ai-card")
    find_key(card, "ai-url").value = "https://ai.example.com"
    find_key(card, "ai-token").value = TOKEN
    find_key(card, "ai-telemetry").value = True
    find_key(card, "save-ai").on_click(None)
    s = app.state.settings
    assert (s.ai_service_url, s.ai_service_token, s.telemetry_enabled) == ("https://ai.example.com", TOKEN, True)


def test_reports_upload_only_after_opt_in(tmp_path):
    stub = StubClient()
    app, page = make_app(tmp_path, stub, ai_service_url="https://ai.example.com")
    app.reporter.capture_message("something broke")
    assert ai_vm.upload_reports(app.state, app.services) == 0 and stub.events == []
    app.state.update_settings(telemetry_enabled=True)
    assert ai_vm.upload_reports(app.state, app.services) == 1
    assert stub.events[0]["message"] == "something broke" and app.reporter.pending() == []


def test_error_boundary_feeds_reporter(tmp_path, monkeypatch):
    import logging

    from ui import app as app_module

    app, page = make_app(tmp_path, StubClient())
    app.reporter.install(logger=logging.getLogger("ui.app"))
    try:
        monkeypatch.setitem(app_module.VIEW_BUILDERS, "/radar", lambda ctx: 1 / 0)
        app.navigate("/radar")
    finally:
        app.reporter.uninstall()
    event = app.reporter.recent()[0]
    assert event["type"] == "ZeroDivisionError" and event["context"]["route"] == "/radar"


def app_ctx(app):
    from ui.views.base import ViewContext

    return ViewContext(app.state, app.layout, app.pal, app.navigate, app.refresh, app.notify, app.show_dialog,
                       app.close_dialog, services=app.services)
