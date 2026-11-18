"""
Receipt field extraction: model output + deterministic cross-checks.

Pipelines (RS_AI_RECEIPT_BACKEND):

  ocr_llm  Tesseract OCR → the shared instruct LLM turns text into JSON.
           Small, CPU-friendly, one model in memory for receipts and tax Q&A.
  vlm      A vision-language model reads the image directly. Better on
           crumpled/angled photos; needs a bigger server (or a GPU).

Either way the model's answer is checked against regex heuristics over the
OCR text: a total the LLM "read" that appears nowhere on the receipt is
replaced or flagged. Confidence per field is "high" when model and
heuristics agree, "medium" when only one found it, "low" otherwise.

Output amounts are decimal strings exactly as printed (the app converts to
integer minor units); dates are ISO YYYY-MM-DD.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

# --- amounts --------------------------------------------------------------------------

_AMOUNT = r"[-(]?\s?[$€£]?\s?(\d{1,3}(?:[,.]\d{3})*(?:[.,]\d{2})|\d+[.,]\d{2})\)?"
_TOTAL_LINE = re.compile(
    rf"(?im)^(?P<label>(?:grand\s+)?total(?:\s+(?:due|amount|sale|usd|eur))?|amount\s+(?:due|paid)|balance\s+due|"
    rf"total\s+to\s+pay|montant\s+total|gesamt(?:betrag)?|importe\s+total)\b[^\d\n-]*{_AMOUNT}"
)
_SUBTOTAL_LINE = re.compile(rf"(?im)^\s*(?:sub\s*-?\s*total|net\s+amount)\b[^\d\n-]*{_AMOUNT}")
_TAX_LINE = re.compile(rf"(?im)^\s*(?:sales\s+tax|tax|vat|gst|hst|tva|mwst|iva)\b[^\d\n%-]*(?:\d+(?:\.\d+)?\s*%[^\d\n-]*)?{_AMOUNT}")

_CURRENCY_HINTS = (
    ("EUR", re.compile(r"€|\bEUR\b")), ("GBP", re.compile(r"£|\bGBP\b")), ("CAD", re.compile(r"\bCAD\b|C\$")),
    ("AUD", re.compile(r"\bAUD\b|A\$")), ("MXN", re.compile(r"\bMXN\b")), ("USD", re.compile(r"\$|\bUSD\b")),
)

# --- dates ----------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_ISO_DATE = re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b")
_NUM_DATE = re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b")
_NAME_DATE = re.compile(r"(?i)\b(\d{1,2})?\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(\d{1,2})?,?\s*(20\d{2})\b")

_VENDOR_SKIP = re.compile(r"(?i)^(?:receipt|welcome|thank|store\s*#|tel|phone|www\.|http|\d|order|invoice|date|cashier)")


def normalize_amount(raw: str | None) -> str | None:
    """'1,234.56' / '1.234,56' / '12,50' → '1234.56' / '1234.56' / '12.50'."""
    if raw is None:
        return None
    s = re.sub(r"[^\d.,-]", "", str(raw))
    if not s:
        return None
    negative = s.startswith("-")
    s = s.lstrip("-")
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) == 2 else s.replace(",", "")
    elif s.count(".") > 1:
        head, _, tail = s.rpartition(".")
        s = f"{head.replace('.', '')}.{tail}" if len(tail) == 2 else s.replace(".", "")
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    if negative:
        value = -value
    return f"{value:.2f}" if value == value.quantize(Decimal("0.01")) else str(value)


def _valid(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def find_dates(text: str, *, day_first: bool = False) -> list[date]:
    found: list[date] = []
    for y, m, d in _ISO_DATE.findall(text):
        if (v := _valid(int(y), int(m), int(d))):
            found.append(v)
    for a, b, y in _NUM_DATE.findall(text):
        year = int(y) + (2000 if len(y) == 2 else 0)
        m, d = (int(b), int(a)) if day_first else (int(a), int(b))
        v = _valid(year, m, d) or _valid(year, d, m)      # fall back when the first reading is impossible
        if v:
            found.append(v)
    for d1, mon, d2, y in _NAME_DATE.findall(text):
        day = d1 or d2
        if day and (v := _valid(int(y), _MONTHS[mon.lower()[:3]], int(day))):
            found.append(v)
    return found


@dataclass
class Heuristics:
    total: str | None = None
    subtotal: str | None = None
    tax: str | None = None
    date: str | None = None
    vendor: str | None = None
    currency: str | None = None
    amounts: set[str] = field(default_factory=set)      # every amount printed on the receipt


def heuristics(text: str, *, locale: str | None = None) -> Heuristics:
    h = Heuristics()
    h.amounts = {a for a in (normalize_amount(m) for m in re.findall(r"\d[\d,.]*[.,]\d{2}\b", text)) if a}
    totals = [normalize_amount(m.group(m.lastindex or 0)) for m in _TOTAL_LINE.finditer(text)]
    totals = [t for t in totals if t]
    if totals:
        h.total = max(totals, key=lambda t: Decimal(t))   # "TOTAL" often repeats; tender lines can be lower
    if (m := _SUBTOTAL_LINE.search(text)):
        h.subtotal = normalize_amount(m.group(m.lastindex or 0))
    if (m := _TAX_LINE.search(text)):
        h.tax = normalize_amount(m.group(m.lastindex or 0))
    day_first = bool(locale) and not str(locale).lower().startswith(("en_us", "en-us", "es_us"))
    dates = find_dates(text, day_first=day_first)
    if dates:
        h.date = dates[0].isoformat()
    for ccy, rx in _CURRENCY_HINTS:
        if rx.search(text):
            h.currency = ccy
            break
    for line in text.splitlines()[:6]:
        clean = line.strip(" *-=#|")
        if len(clean) >= 3 and sum(c.isalpha() for c in clean) >= 3 and not _VENDOR_SKIP.match(clean):
            h.vendor = " ".join(clean.split())[:80]
            break
    return h


# --- LLM prompt & parsing -----------------------------------------------------------------

EXTRACTION_SYSTEM = (
    "You extract data from store receipts. Reply with one JSON object and nothing else, using exactly these keys: "
    '"vendor" (store or business name), "date" (YYYY-MM-DD), "currency" (ISO 4217 code), '
    '"subtotal", "tax", "total" (amounts as printed, digits and decimal point only), '
    '"items" (list of {"description", "amount"}). Use null for anything not printed on the receipt. '
    "Never guess or calculate a value that is not printed."
)


def extraction_prompt(ocr_text: str, *, currency_hint: str | None) -> str:
    hint = f"The user's usual currency is {currency_hint}.\n" if currency_hint else ""
    return f"{hint}Receipt text (from OCR, may contain errors):\n<<<\n{ocr_text[:6000]}\n>>>"


def parse_json_object(text: str) -> dict[str, Any] | None:
    """First balanced {...} in model output (models sometimes add prose or ```json fences)."""
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
                    return value if isinstance(value, dict) else None
        start = text.find("{", start + 1)
    return None


# --- merge --------------------------------------------------------------------------------


def _clean_str(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    s = " ".join(value.split())
    return s[:limit] if s and s.lower() not in ("null", "none", "n/a", "unknown") else None


def _clean_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10]).isoformat()
    except ValueError:
        found = find_dates(value)
        return found[0].isoformat() if found else None


def _fix_ocr_letters(name: str | None) -> str | None:
    """OCR reads O as 0 in names ("AUTOZ0NE"); fix zeros that sit inside words so vendor
    matching works. Standalone numbers ("7-ELEVEN #0042") are left alone."""
    if not name:
        return name
    return re.sub(r"(?<=[A-Za-z])0(?=[A-Za-z])|(?<=[A-Za-z]{2})0\b|\b0(?=[A-Za-z]{2})", "O", name)


def merge(model: Mapping[str, Any] | None, h: Heuristics, *, has_text: bool) -> dict[str, Any]:
    """Combine model output with heuristics into the API response fields."""
    model = model or {}
    out: dict[str, Any] = {"confidence": {}, "warnings": []}
    conf, warnings = out["confidence"], out["warnings"]

    for key in ("total", "subtotal", "tax"):
        m_val = normalize_amount(str(model[key])) if model.get(key) not in (None, "") else None
        h_val = getattr(h, key)
        if m_val and h_val and Decimal(m_val) == Decimal(h_val):
            out[key], conf[key] = m_val, "high"
        elif m_val and has_text and m_val not in h.amounts:
            # The model produced a number that isn't printed anywhere: don't trust it.
            out[key], conf[key] = h_val, "medium" if h_val else "low"
            if key == "total":
                warnings.append("The model's total isn't printed on the receipt; using the receipt's TOTAL line"
                                if h_val else "Couldn't find the total on the receipt")
        elif m_val or h_val:
            out[key], conf[key] = m_val or h_val, "medium"
        else:
            out[key], conf[key] = None, "low"

    m_date, h_date = _clean_date(model.get("date")), h.date
    out["date"] = m_date or h_date
    conf["date"] = "high" if m_date and m_date == h_date else ("medium" if out["date"] else "low")

    m_vendor = _fix_ocr_letters(_clean_str(model.get("vendor"), 120))
    out["vendor"] = m_vendor or _fix_ocr_letters(h.vendor)
    conf["vendor"] = ("high" if m_vendor and h.vendor and m_vendor.lower()[:6] == h.vendor.lower()[:6]
                      else "medium" if out["vendor"] else "low")

    m_ccy = _clean_str(model.get("currency"), 3)
    out["currency"] = (m_ccy.upper() if m_ccy and re.fullmatch(r"[A-Za-z]{3}", m_ccy) else None) or h.currency

    items = []
    for raw in model.get("items") or []:
        if isinstance(raw, Mapping):
            desc = _clean_str(raw.get("description"), 120)
            amt = normalize_amount(str(raw.get("amount"))) if raw.get("amount") not in (None, "") else None
            if desc and amt:
                items.append({"description": desc, "amount": amt})
    out["items"] = items[:50]
    return out
