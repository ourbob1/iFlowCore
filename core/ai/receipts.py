"""
Receipt scanning: AI-extracted fields → a reviewable expense draft.

The service returns amounts as decimal strings exactly as printed. Here they
become integer minor units (core.financial.money), the vendor is run through
the normal AutoTagger, and sanity checks turn into warnings for the review
dialog. Nothing is written until the user confirms: `save_draft` then creates
the expense and attaches the original file as a managed receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping

from core.ai.client import AIServiceClient, AIServiceError
from core.config import ConfigError, normalize_currency
from core.financial.expenses import (
    MAX_RECEIPT_BYTES,
    AutoTagger,
    ExpenseError,
    ReceiptInfo,
    TagResult,
    _sniff_mime,
    attach_receipt,
    create_expense,
)
from core.financial.money import MoneyError, parse_amount
from core.timeutil import to_iso_utc, zone

CONFIDENCE_LEVELS = ("high", "medium", "low")
MAX_AGE_DAYS = 3 * 366          # older than this is almost certainly a misread year
TOTAL_TOLERANCE_MINOR = 2       # rounding slack when checking subtotal + tax = total


@dataclass(frozen=True)
class LineItem:
    description: str
    amount_minor: int


@dataclass(frozen=True)
class ReceiptDraft:
    vendor: str | None
    occurred_on: date | None
    currency: str
    total_minor: int | None
    subtotal_minor: int | None
    tax_minor: int | None
    items: tuple[LineItem, ...]
    category: TagResult
    confidence: Mapping[str, str]
    warnings: tuple[str, ...] = ()
    model: str = ""
    duration_ms: int | None = None

    @property
    def needs_attention(self) -> tuple[str, ...]:
        """Fields the reviewer should look at first."""
        return tuple(k for k in ("vendor", "date", "total") if self.confidence.get(k, "low") == "low")


def _amount(value: Any, currency: str, label: str, warnings: list[str]) -> int | None:
    if value in (None, ""):
        return None
    try:
        minor = parse_amount(str(value), currency)
    except MoneyError:
        warnings.append(f"Couldn't read the {label} ({value!r})")
        return None
    if minor < 0:
        warnings.append(f"The {label} came back negative; check the receipt")
        return None
    return minor


def draft_from_response(payload: Mapping[str, Any], *, default_currency: str, today: date,
                        tagger: AutoTagger | None = None) -> ReceiptDraft:
    """Validate and normalize a /v1/receipts/extract response."""
    warnings: list[str] = [str(w) for w in payload.get("warnings") or [] if isinstance(w, str)][:5]

    currency = default_currency
    raw_currency = payload.get("currency")
    if isinstance(raw_currency, str) and raw_currency.strip():
        try:
            currency = normalize_currency(raw_currency)
        except ConfigError:
            warnings.append(f"Unknown currency '{raw_currency}'; using {default_currency}")
    if currency != default_currency:
        warnings.append(f"Receipt is in {currency}, not your base currency {default_currency}")

    total = _amount(payload.get("total"), currency, "total", warnings)
    subtotal = _amount(payload.get("subtotal"), currency, "subtotal", warnings)
    tax = _amount(payload.get("tax"), currency, "tax", warnings)
    if total is None and subtotal is not None:
        total = subtotal + (tax or 0)
        warnings.append("No total found; used subtotal + tax")
    if None not in (total, subtotal, tax) and abs(subtotal + tax - total) > TOTAL_TOLERANCE_MINOR:  # type: ignore[operator]
        warnings.append("Subtotal + tax doesn't match the total (tips or discounts?)")

    occurred: date | None = None
    raw_date = payload.get("date")
    if isinstance(raw_date, str) and raw_date.strip():
        try:
            occurred = date.fromisoformat(raw_date.strip()[:10])
        except ValueError:
            warnings.append(f"Couldn't read the date ({raw_date!r})")
    if occurred is not None:
        if occurred > today + timedelta(days=1):
            warnings.append("Receipt date is in the future; check the year")
        elif (today - occurred).days > MAX_AGE_DAYS:
            warnings.append("Receipt is more than 3 years old; check the year")

    vendor = payload.get("vendor")
    vendor = " ".join(vendor.split())[:120] if isinstance(vendor, str) and vendor.strip() else None

    items: list[LineItem] = []
    for raw in (payload.get("items") or [])[:50]:
        if not isinstance(raw, Mapping):
            continue
        desc = " ".join(str(raw.get("description") or "").split())[:120]
        amt = _amount(raw.get("amount"), currency, "line item", [])
        if desc and amt is not None:
            items.append(LineItem(desc, amt))

    raw_conf = payload.get("confidence") if isinstance(payload.get("confidence"), Mapping) else {}
    confidence = {k: (v if v in CONFIDENCE_LEVELS else "low") for k, v in raw_conf.items() if isinstance(k, str)}
    for k, present in (("vendor", vendor), ("date", occurred), ("total", total)):
        if present is None:
            confidence[k] = "low"

    descriptions = " ".join(i.description for i in items[:5]) or None
    category = (tagger or AutoTagger()).tag(vendor, descriptions)
    duration = payload.get("duration_ms")
    return ReceiptDraft(
        vendor=vendor, occurred_on=occurred, currency=currency, total_minor=total, subtotal_minor=subtotal,
        tax_minor=tax, items=tuple(items), category=category, confidence=confidence, warnings=tuple(warnings),
        model=str(payload.get("model") or ""), duration_ms=duration if isinstance(duration, int) else None,
    )


def read_receipt_file(path: str | Path) -> tuple[bytes, str]:
    """Load and type-check a receipt before it leaves the device (same rules as
    attach_receipt: allowed type, real file signature, size cap)."""
    src = Path(path).expanduser()
    if not src.is_file():
        raise ExpenseError(f"Receipt file not found: {src.name}")
    size = src.stat().st_size
    if size == 0:
        raise ExpenseError("Receipt file is empty")
    if size > MAX_RECEIPT_BYTES:
        raise ExpenseError(f"Receipt exceeds {MAX_RECEIPT_BYTES // (1024 * 1024)} MB")
    mime = _sniff_mime(src)
    return src.read_bytes(), mime


def scan_receipt(client: AIServiceClient, path: str | Path, *, default_currency: str, today: date,
                 locale: str | None = None, tagger: AutoTagger | None = None) -> ReceiptDraft:
    content, mime = read_receipt_file(path)
    payload = client.extract_receipt(content, mime, currency_hint=default_currency, locale=locale)
    return draft_from_response(payload, default_currency=default_currency, today=today, tagger=tagger)


@dataclass
class ConfirmedReceipt:
    """What the user approved in the review dialog."""

    amount_minor: int
    currency: str
    occurred_on: date
    vendor: str | None
    category: str
    description: str | None = None
    business_use_bp: int = 10_000
    notes: list[str] = field(default_factory=list)


def save_draft(db: Any, confirmed: ConfirmedReceipt, receipt_path: str | Path, *, store_dir: str | Path, tz: str,
               base_currency: str, device_id: str | None = None) -> tuple[str, ReceiptInfo]:
    """Create the expense and attach the scanned file (managed copy).

    The receipt has only a calendar date, so the expense is stamped at local
    noon, which keeps it on the same local day in every timezone rule the
    tax engine applies."""
    if confirmed.amount_minor <= 0:
        raise ExpenseError("Amount must be greater than zero")
    currency = normalize_currency(confirmed.currency)
    read_receipt_file(receipt_path)                 # fail before creating anything if the file moved
    occurred_at = to_iso_utc(datetime.combine(confirmed.occurred_on, time(12, 0), tzinfo=zone(tz)))
    expense_id, _ = create_expense(
        db, amount_minor=confirmed.amount_minor, currency=currency, occurred_at=occurred_at,
        vendor=confirmed.vendor, description=confirmed.description or "Scanned receipt",
        category=confirmed.category, business_use_bp=confirmed.business_use_bp,
        base_amount_minor=confirmed.amount_minor if currency == base_currency else None, device_id=device_id,
    )
    try:
        info = attach_receipt(db, receipt_path, expense_id=expense_id, store_dir=store_dir, device_id=device_id)
    except Exception:
        with db.transaction() as conn:              # no half-saved expense without its receipt
            conn.execute("UPDATE expenses SET deleted_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                         (expense_id,))
        raise
    return expense_id, info


__all__ = [
    "AIServiceError", "ConfirmedReceipt", "LineItem", "ReceiptDraft", "draft_from_response", "read_receipt_file",
    "save_draft", "scan_receipt",
]
