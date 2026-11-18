"""
Accounting exports: QFX (Quicken Web Connect / OFX 1.02), QIF, and CSV
(generic + QuickBooks Online bank-upload layouts).

Two layers:

  1. `collect_ledger` / `collect_mileage` read the database and produce plain
     entry objects (signed integer amounts, one currency).
  2. `to_qfx` / `to_qif` / `to_csv` / `mileage_to_csv` are pure formatters over
     those entries — deterministic and easy to test.

Design notes
------------
* Amounts are rendered from integer minor units with the currency's exponent
  (`format_minor`), never via float: "25.00", "-12.34", JPY "1200".
* By default platform fees are exported as their own negative line
  (`split_platform_fees=True`), so gross income and fees land in separate
  categories — what Schedule C wants. Set False to export net deposits.
* Mileage is exported only as CSV. QIF/QFX describe cash movements; importing
  a non-cash mileage deduction as a bank transaction would corrupt account
  balances in Quicken/QuickBooks.
* QFX `FITID` is the record's UUID, so re-importing the same export does not
  create duplicates in Quicken.
* Quicken validates `INTU.BID` against its institution list for Web Connect.
  It is configurable and omitted by default (plain OFX, which QuickBooks and
  most tools accept); there is no universally valid value.
* Entries in a currency other than the export currency use their pinned
  `base_amount_minor` when the export currency is the base currency;
  otherwise they are skipped and reported in `warnings`.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from core.config import UnitSystem, normalize_currency
from core.financial.money import format_minor

EntryKind = Literal["income", "fee", "expense"]
CsvDialect = Literal["generic", "quickbooks_3col", "quickbooks_4col"]

INCOME_CATEGORY = {
    "earning": "Business Income:Gig Earnings",
    "tip": "Business Income:Tips",
    "bonus": "Business Income:Bonuses",
    "incentive": "Business Income:Incentives",
    "reimbursement": "Business Income:Reimbursements",
    "adjustment": "Business Income:Adjustments",
    "refund": "Business Income:Refunds",
}
FEE_CATEGORY = "Business Expenses:Platform Fees"
EXPENSE_CATEGORY = {
    "vehicle": "Business Expenses:Auto:Vehicle",
    "fuel": "Business Expenses:Auto:Fuel",
    "maintenance": "Business Expenses:Auto:Maintenance",
    "insurance": "Business Expenses:Auto:Insurance",
    "parking_tolls": "Business Expenses:Auto:Parking & Tolls",
    "phone": "Business Expenses:Phone",
    "internet": "Business Expenses:Internet",
    "equipment": "Business Expenses:Equipment",
    "supplies": "Business Expenses:Supplies",
    "software": "Business Expenses:Software",
    "platform_fees": FEE_CATEGORY,
    "meals": "Business Expenses:Meals",
    "travel": "Business Expenses:Travel",
    "home_office": "Business Expenses:Home Office",
    "professional_services": "Business Expenses:Professional Services",
    "education": "Business Expenses:Education",
    "other": "Business Expenses:Other",
}


class ExportError(ValueError):
    pass


@dataclass(frozen=True)
class LedgerEntry:
    id: str                     # stable unique id (FITID)
    posted_on: date
    amount_minor: int           # + money in, − money out
    currency: str
    kind: EntryKind
    category: str               # Quicken-style "Parent:Child"
    payee: str
    memo: str = ""


@dataclass(frozen=True)
class MileageEntry:
    id: str
    started_at: str             # ISO-8601 UTC
    distance_m: int
    purpose: str
    description: str = ""
    start_location: str = ""
    end_location: str = ""
    vehicle: str = ""
    platform: str = ""


@dataclass
class Ledger:
    entries: list[LedgerEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Collection from the database
# ---------------------------------------------------------------------------


def _ratio(value: int, numerator: int, denominator: int) -> int:
    """value × numerator / denominator, rounded half-up, without floats."""
    exact = Decimal(value) * numerator / denominator
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _pick_amount(amount: int, row_currency: str, base_amount: int | None, export_currency: str, base_currency: str) -> int | None:
    if row_currency == export_currency:
        return amount
    if export_currency == base_currency and base_amount is not None:
        return base_amount
    return None


def collect_ledger(
    db: Any,
    start: date,
    end: date,
    *,
    currency: str,
    base_currency: str | None = None,
    split_platform_fees: bool = True,
    include_income: bool = True,
    include_expenses: bool = True,
) -> Ledger:
    """Entries with dates in [start, end] (inclusive), sorted by date then id."""
    if end < start:
        raise ExportError("end date is before start date")
    currency = normalize_currency(currency)
    base_currency = normalize_currency(base_currency or currency)
    lo, hi = start.isoformat(), date.fromordinal(end.toordinal() + 1).isoformat()
    ledger = Ledger()
    skipped = 0

    with db.connection() as conn:
        if include_income:
            for r in conn.execute(
                """SELECT t.id, t.occurred_at, t.kind, t.gross_amount_minor, t.platform_fee_minor,
                          t.currency, t.base_amount_minor, t.description, t.external_ref, p.name AS platform
                     FROM transactions t JOIN platforms p ON p.id = t.platform_id
                    WHERE t.deleted_at IS NULL AND t.occurred_at >= ? AND t.occurred_at < ?""",
                (lo, hi),
            ):
                gross = _pick_amount(r["gross_amount_minor"], r["currency"], r["base_amount_minor"], currency, base_currency)
                if gross is None:
                    skipped += 1
                    continue
                fee = r["platform_fee_minor"]
                if r["currency"] != currency and r["gross_amount_minor"]:
                    fee = _ratio(fee, gross, r["gross_amount_minor"])  # proportional FX conversion
                posted = date.fromisoformat(r["occurred_at"][:10])
                memo = " ".join(filter(None, [r["description"], f"ref {r['external_ref']}" if r["external_ref"] else None]))
                amount = gross if split_platform_fees else gross - fee
                ledger.entries.append(LedgerEntry(
                    r["id"], posted, amount, currency, "income",
                    INCOME_CATEGORY.get(r["kind"], "Business Income"), r["platform"], memo,
                ))
                if split_platform_fees and fee:
                    ledger.entries.append(LedgerEntry(
                        f"{r['id']}-fee", posted, -fee, currency, "fee", FEE_CATEGORY, r["platform"],
                        f"Platform fee {memo}".strip(),
                    ))
        if include_expenses:
            for r in conn.execute(
                """SELECT id, occurred_at, category, amount_minor, currency, base_amount_minor, vendor, description
                     FROM expenses WHERE deleted_at IS NULL AND occurred_at >= ? AND occurred_at < ?""",
                (lo, hi),
            ):
                amount = _pick_amount(r["amount_minor"], r["currency"], r["base_amount_minor"], currency, base_currency)
                if amount is None:
                    skipped += 1
                    continue
                ledger.entries.append(LedgerEntry(
                    r["id"], date.fromisoformat(r["occurred_at"][:10]), -amount, currency, "expense",
                    EXPENSE_CATEGORY.get(r["category"], "Business Expenses:Other"),
                    r["vendor"] or r["description"] or "Expense", r["description"] or "",
                ))

    if skipped:
        ledger.warnings.append(f"{skipped} record(s) in other currencies without a {currency} amount were skipped")
    ledger.entries.sort(key=lambda e: (e.posted_on, e.id))
    return ledger


def collect_mileage(db: Any, start: date, end: date, *, business_only: bool = False) -> list[MileageEntry]:
    lo, hi = start.isoformat(), date.fromordinal(end.toordinal() + 1).isoformat()
    sql = """SELECT m.id, m.started_at, m.distance_m, m.purpose, m.description, m.start_location,
                    m.end_location, m.vehicle_label, p.name AS platform
               FROM mileage_logs m LEFT JOIN platforms p ON p.id = m.platform_id
              WHERE m.deleted_at IS NULL AND m.started_at >= ? AND m.started_at < ?"""
    if business_only:
        sql += " AND m.purpose = 'business'"
    with db.connection() as conn:
        rows = conn.execute(sql + " ORDER BY m.started_at, m.id", (lo, hi)).fetchall()
    return [
        MileageEntry(r["id"], r["started_at"], r["distance_m"], r["purpose"], r["description"] or "",
                     r["start_location"] or "", r["end_location"] or "", r["vehicle_label"] or "", r["platform"] or "")
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Text sanitizing
# ---------------------------------------------------------------------------


def _one_line(text: str) -> str:
    return re.sub(r"[\r\n\t\x00-\x1f]+", " ", text or "").strip()


def _ascii(text: str) -> str:
    """Transliterate to ASCII for OFX/QIF consumers ('Café' -> 'Cafe')."""
    normalized = unicodedata.normalize("NFKD", _one_line(text))
    return normalized.encode("ascii", "ignore").decode("ascii")


def _check_currency(entries: Sequence[LedgerEntry], currency: str) -> None:
    wrong = {e.currency for e in entries if e.currency != currency}
    if wrong:
        raise ExportError(f"Entries in {sorted(wrong)} can't go in a {currency} export")


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def to_csv(entries: Sequence[LedgerEntry], *, currency: str, dialect: CsvDialect = "generic") -> str:
    """CSV text.

    generic          date(ISO), type, category, payee, memo, amount, currency, id
    quickbooks_3col  Date(MM/DD/YYYY), Description, Amount          (QBO bank upload)
    quickbooks_4col  Date(MM/DD/YYYY), Description, Credit, Debit   (QBO bank upload)
    """
    currency = normalize_currency(currency)
    _check_currency(entries, currency)
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    fmt = lambda v: format_minor(v, currency)  # noqa: E731
    us = lambda d: d.strftime("%m/%d/%Y")  # noqa: E731

    if dialect == "generic":
        w.writerow(["date", "type", "category", "payee", "memo", "amount", "currency", "id"])
        for e in entries:
            w.writerow([e.posted_on.isoformat(), e.kind, e.category, _one_line(e.payee), _one_line(e.memo),
                        fmt(e.amount_minor), currency, e.id])
    elif dialect == "quickbooks_3col":
        w.writerow(["Date", "Description", "Amount"])
        for e in entries:
            w.writerow([us(e.posted_on), _one_line(f"{e.payee} - {e.memo}" if e.memo else e.payee), fmt(e.amount_minor)])
    elif dialect == "quickbooks_4col":
        w.writerow(["Date", "Description", "Credit", "Debit"])
        for e in entries:
            desc = _one_line(f"{e.payee} - {e.memo}" if e.memo else e.payee)
            credit = fmt(e.amount_minor) if e.amount_minor > 0 else ""
            debit = fmt(-e.amount_minor) if e.amount_minor < 0 else ""
            w.writerow([us(e.posted_on), desc, credit, debit])
    else:
        raise ExportError(f"Unknown CSV dialect '{dialect}'")
    return out.getvalue()


def mileage_to_csv(
    entries: Sequence[MileageEntry],
    *,
    unit_system: UnitSystem = UnitSystem.IMPERIAL,
    rate_lookup: Any | None = None,
    currency: str = "USD",
) -> str:
    """Mileage log CSV (IRS-style: date, distance, purpose, route, description).

    `rate_lookup` is an optional TaxTable; when given, business trips include
    the standard-mileage deduction for that trip's date.
    """
    from core.financial.tax import mileage_deduction  # local import: avoid cycle

    unit = unit_system.distance_unit
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    header = ["date", f"distance_{unit}", "distance_m", "purpose", "description", "start", "end", "vehicle", "platform"]
    if rate_lookup is not None:
        header.append(f"deduction_{currency.lower()}")
    w.writerow(header)
    for e in entries:
        day = date.fromisoformat(e.started_at[:10])
        # One decimal place for display, rounded half-up from exact meters.
        shown = (Decimal(e.distance_m) / Decimal(str(unit_system.meters_per_distance_unit))).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP)
        row = [day.isoformat(), str(shown), e.distance_m, e.purpose, _one_line(e.description),
               _one_line(e.start_location), _one_line(e.end_location), _one_line(e.vehicle), _one_line(e.platform)]
        if rate_lookup is not None:
            ded = mileage_deduction({day: e.distance_m}, rate_lookup) if e.purpose == "business" else 0
            row.append(format_minor(ded, currency))
        w.writerow(row)
    return out.getvalue()


# ---------------------------------------------------------------------------
# QIF
# ---------------------------------------------------------------------------


def to_qif(
    entries: Sequence[LedgerEntry],
    *,
    currency: str,
    account_type: Literal["Bank", "Cash", "CCard"] = "Bank",
    include_category_list: bool = True,
) -> str:
    """Quicken Interchange Format. Dates MM/DD/YYYY; CRLF line endings."""
    currency = normalize_currency(currency)
    _check_currency(entries, currency)
    lines: list[str] = []
    if include_category_list:
        categories: dict[str, str] = {}
        for e in entries:
            parts = e.category.split(":")
            for i in range(1, len(parts) + 1):
                categories.setdefault(":".join(parts[:i]), "I" if e.kind == "income" else "E")
        if categories:
            lines.append("!Type:Cat")
            for name in sorted(categories):
                lines += [f"N{_ascii(name)}", categories[name], "^"]
    lines.append(f"!Type:{account_type}")
    for e in entries:
        lines.append(f"D{e.posted_on.strftime('%m/%d/%Y')}")
        lines.append(f"T{format_minor(e.amount_minor, currency)}")
        lines.append(f"P{_ascii(e.payee)[:64]}")
        if e.memo:
            lines.append(f"M{_ascii(e.memo)[:255]}")
        lines.append(f"L{_ascii(e.category)}")
        lines.append("^")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# QFX / OFX 1.02 (SGML)
# ---------------------------------------------------------------------------


def _ofx_escape(text: str) -> str:
    return _ascii(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_qfx(
    entries: Sequence[LedgerEntry],
    *,
    currency: str,
    account_id: str,
    start: date,
    end: date,
    bank_id: str = "000000000",
    account_type: Literal["CHECKING", "SAVINGS", "CREDITLINE", "MONEYMRKT"] = "CHECKING",
    intu_bid: str | None = None,
    generated_at: datetime | None = None,
    ledger_balance_minor: int | None = None,
) -> str:
    """OFX 1.02 SGML statement, importable by Quicken (as .qfx) and QuickBooks/others (.ofx).

    `ledger_balance_minor` defaults to the net of the exported entries, since
    iFlow Core tracks activity rather than a bank balance.
    """
    currency = normalize_currency(currency)
    _check_currency(entries, currency)
    if not re.fullmatch(r"[A-Za-z0-9-]{1,22}", account_id):
        raise ExportError("account_id must be 1-22 letters, digits or '-'")
    now = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = now.strftime("%Y%m%d%H%M%S")
    balance = sum(e.amount_minor for e in entries) if ledger_balance_minor is None else ledger_balance_minor

    header = "\r\n".join([
        "OFXHEADER:100", "DATA:OFXSGML", "VERSION:102", "SECURITY:NONE", "ENCODING:USASCII",
        "CHARSET:1252", "COMPRESSION:NONE", "OLDFILEUID:NONE", "NEWFILEUID:NONE",
    ])
    body: list[str] = [
        "<OFX>",
        "<SIGNONMSGSRSV1>", "<SONRS>",
        "<STATUS>", "<CODE>0", "<SEVERITY>INFO", "</STATUS>",
        f"<DTSERVER>{stamp}", "<LANGUAGE>ENG",
    ]
    if intu_bid:
        if not re.fullmatch(r"\d{1,10}", intu_bid):
            raise ExportError("intu_bid must be numeric")
        body.append(f"<INTU.BID>{intu_bid}")
    body += [
        "</SONRS>", "</SIGNONMSGSRSV1>",
        "<BANKMSGSRSV1>", "<STMTTRNRS>", "<TRNUID>1",
        "<STATUS>", "<CODE>0", "<SEVERITY>INFO", "</STATUS>",
        "<STMTRS>", f"<CURDEF>{currency}",
        "<BANKACCTFROM>", f"<BANKID>{_ofx_escape(bank_id)}", f"<ACCTID>{account_id}", f"<ACCTTYPE>{account_type}",
        "</BANKACCTFROM>",
        "<BANKTRANLIST>", f"<DTSTART>{start.strftime('%Y%m%d')}", f"<DTEND>{end.strftime('%Y%m%d')}",
    ]
    for e in entries:
        trntype = "CREDIT" if e.amount_minor >= 0 else ("FEE" if e.kind == "fee" else "DEBIT")
        body += [
            "<STMTTRN>",
            f"<TRNTYPE>{trntype}",
            f"<DTPOSTED>{e.posted_on.strftime('%Y%m%d')}120000",
            f"<TRNAMT>{format_minor(e.amount_minor, currency)}",
            f"<FITID>{_ofx_escape(e.id)[:255]}",
            f"<NAME>{_ofx_escape(e.payee)[:32] or 'Unknown'}",
        ]
        memo = " | ".join(filter(None, [e.category, e.memo]))
        if memo:
            body.append(f"<MEMO>{_ofx_escape(memo)[:255]}")
        body.append("</STMTTRN>")
    body += [
        "</BANKTRANLIST>",
        "<LEDGERBAL>", f"<BALAMT>{format_minor(balance, currency)}", f"<DTASOF>{end.strftime('%Y%m%d')}",
        "</LEDGERBAL>",
        "</STMTRS>", "</STMTTRNRS>", "</BANKMSGSRSV1>",
        "</OFX>",
    ]
    return header + "\r\n\r\n" + "\r\n".join(body) + "\r\n"


# ---------------------------------------------------------------------------
# File writing
# ---------------------------------------------------------------------------

_ENCODINGS: Mapping[str, str] = {"qfx": "ascii", "ofx": "ascii", "qif": "cp1252", "csv": "utf-8-sig"}


@dataclass(frozen=True)
class ExportReport:
    path: Path
    format: str
    entries: int
    warnings: tuple[str, ...]


def export_ledger(
    db: Any,
    path: str | Path,
    *,
    fmt: Literal["qfx", "ofx", "qif", "csv"],
    start: date,
    end: date,
    currency: str,
    base_currency: str | None = None,
    csv_dialect: CsvDialect = "generic",
    split_platform_fees: bool = True,
    account_id: str = "RUBYSOHO",
    intu_bid: str | None = None,
) -> ExportReport:
    """Collect entries for a date range and write them in the chosen format.

    CSV is written as UTF-8 with BOM (Excel-friendly); QIF as Windows-1252;
    QFX/OFX as ASCII, as their headers declare.
    """
    ledger = collect_ledger(db, start, end, currency=currency, base_currency=base_currency,
                            split_platform_fees=split_platform_fees)
    if fmt in ("qfx", "ofx"):
        text = to_qfx(ledger.entries, currency=currency, account_id=account_id, start=start, end=end,
                      intu_bid=intu_bid if fmt == "qfx" else None)
    elif fmt == "qif":
        text = to_qif(ledger.entries, currency=currency)
    elif fmt == "csv":
        text = to_csv(ledger.entries, currency=currency, dialect=csv_dialect)
    else:
        raise ExportError(f"Unknown format '{fmt}'")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode(_ENCODINGS[fmt], errors="replace"))
    return ExportReport(target, fmt, len(ledger.entries), tuple(ledger.warnings))


def export_mileage(
    db: Any,
    path: str | Path,
    *,
    start: date,
    end: date,
    unit_system: UnitSystem = UnitSystem.IMPERIAL,
    rate_table: Any | None = None,
    business_only: bool = False,
) -> ExportReport:
    entries = collect_mileage(db, start, end, business_only=business_only)
    text = mileage_to_csv(entries, unit_system=unit_system, rate_lookup=rate_table)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8-sig"))
    return ExportReport(target, "csv", len(entries), ())
