"""QFX / QIF / CSV export formatting and ledger collection."""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timezone

import pytest

from core.config import UnitSystem
from core.financial.exports import (
    ExportError,
    LedgerEntry,
    MileageEntry,
    collect_ledger,
    export_ledger,
    export_mileage,
    mileage_to_csv,
    to_csv,
    to_qfx,
    to_qif,
)
from core.financial.tax import TaxTableRegistry

from .conftest import new_id

ENTRIES = [
    LedgerEntry("t-1", date(2026, 9, 1), 2500, "USD", "income", "Business Income:Gig Earnings", "Uber", "Trip & tip"),
    LedgerEntry("t-1-fee", date(2026, 9, 1), -360, "USD", "fee", "Business Expenses:Platform Fees", "Uber", "Platform fee"),
    LedgerEntry("e-1", date(2026, 9, 2), -4210, "USD", "expense", "Business Expenses:Auto:Fuel", "Café <Shell>", ""),
]


# --- CSV ------------------------------------------------------------------------------


def test_generic_csv():
    rows = list(csv.reader(io.StringIO(to_csv(ENTRIES, currency="USD"))))
    assert rows[0] == ["date", "type", "category", "payee", "memo", "amount", "currency", "id"]
    assert rows[1] == ["2026-09-01", "income", "Business Income:Gig Earnings", "Uber", "Trip & tip", "25.00", "USD", "t-1"]
    assert rows[3][5] == "-42.10"


def test_quickbooks_3_and_4_column_layouts():
    three = list(csv.reader(io.StringIO(to_csv(ENTRIES, currency="USD", dialect="quickbooks_3col"))))
    assert three[0] == ["Date", "Description", "Amount"]
    assert three[1] == ["09/01/2026", "Uber - Trip & tip", "25.00"]
    four = list(csv.reader(io.StringIO(to_csv(ENTRIES, currency="USD", dialect="quickbooks_4col"))))
    assert four[0] == ["Date", "Description", "Credit", "Debit"]
    assert four[1][2:] == ["25.00", ""]
    assert four[2][2:] == ["", "3.60"]


def test_csv_uses_crlf_and_escapes_newlines():
    entry = LedgerEntry("x", date(2026, 1, 1), 100, "USD", "income", "C", "Pay\nee", "line1\r\nline2")
    text = to_csv([entry], currency="USD")
    assert text.endswith("\r\n")
    assert "Pay ee" in text and "line1 line2" in text


def test_currency_mismatch_rejected():
    with pytest.raises(ExportError):
        to_csv([LedgerEntry("x", date(2026, 1, 1), 1, "EUR", "income", "C", "P")], currency="USD")


def test_jpy_amounts_have_no_decimals():
    entry = LedgerEntry("j", date(2026, 1, 1), 1200, "JPY", "income", "C", "P")
    assert ",1200," in to_csv([entry], currency="JPY")


# --- QIF ------------------------------------------------------------------------------


def test_qif_structure():
    text = to_qif(ENTRIES, currency="USD")
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    lines = text.split("\r\n")
    assert lines[0] == "!Type:Cat"
    assert "NBusiness Expenses:Auto:Fuel" in lines
    bank = lines[lines.index("!Type:Bank"):]
    assert bank[1:7] == ["D09/01/2026", "T25.00", "PUber", "MTrip & tip", "LBusiness Income:Gig Earnings", "^"]
    assert "T-42.10" in bank
    assert "PCafe <Shell>" in bank              # transliterated to ASCII for Quicken
    assert text.count("^") == text.count("^\r\n")


def test_qif_category_types():
    lines = to_qif(ENTRIES, currency="USD").split("\r\n")
    i = lines.index("NBusiness Income")
    assert lines[i + 1] == "I"
    j = lines.index("NBusiness Expenses")
    assert lines[j + 1] == "E"


# --- QFX ------------------------------------------------------------------------------

GEN = datetime(2026, 9, 26, 15, 0, 0, tzinfo=timezone.utc)


def _qfx(**kw):
    return to_qfx(ENTRIES, currency="USD", account_id="RUBYSOHO", start=date(2026, 9, 1), end=date(2026, 9, 30),
                  generated_at=GEN, **kw)


def test_qfx_header_and_envelope():
    text = _qfx()
    head, body = text.split("\r\n\r\n", 1)
    assert head.split("\r\n")[:3] == ["OFXHEADER:100", "DATA:OFXSGML", "VERSION:102"]
    assert body.startswith("<OFX>") and body.rstrip().endswith("</OFX>")
    assert "<DTSERVER>20260926150000" in body
    assert "<CURDEF>USD" in body and "<ACCTID>RUBYSOHO" in body
    assert "<DTSTART>20260901" in body and "<DTEND>20260930" in body
    assert "INTU.BID" not in body
    text.encode("ascii")                           # must be pure ASCII


def test_qfx_transactions():
    body = _qfx()
    blocks = re.findall(r"<STMTTRN>(.*?)</STMTTRN>", body, re.S)
    assert len(blocks) == 3
    assert "<TRNTYPE>CREDIT" in blocks[0] and "<TRNAMT>25.00" in blocks[0] and "<FITID>t-1" in blocks[0]
    assert "<TRNTYPE>FEE" in blocks[1] and "<TRNAMT>-3.60" in blocks[1]
    assert "<TRNTYPE>DEBIT" in blocks[2]
    assert "<NAME>Cafe &lt;Shell&gt;" in blocks[2]  # escaped + transliterated
    assert "Trip &amp; tip" in blocks[0]
    assert "<BALAMT>-20.70" in body                 # 25.00 − 3.60 − 42.10


def test_qfx_name_truncated_to_32_chars():
    long = LedgerEntry("z", date(2026, 9, 1), 100, "USD", "income", "C", "X" * 50)
    body = to_qfx([long], currency="USD", account_id="A", start=date(2026, 9, 1), end=date(2026, 9, 1), generated_at=GEN)
    assert "<NAME>" + "X" * 32 + "\r\n" in body


def test_qfx_intu_bid_and_validation():
    assert "<INTU.BID>12345" in _qfx(intu_bid="12345")
    with pytest.raises(ExportError):
        _qfx(intu_bid="abc")
    with pytest.raises(ExportError):
        to_qfx(ENTRIES, currency="USD", account_id="bad id!", start=date(2026, 9, 1), end=date(2026, 9, 1))


# --- mileage --------------------------------------------------------------------------


def test_mileage_csv_units_and_deduction():
    fed, _ = TaxTableRegistry.load().get("US", 2026)
    trips = [
        MileageEntry("m1", "2026-05-01T09:00:00.000Z", 16093, "business", "Airport run", "Home", "JFK"),
        MileageEntry("m2", "2026-08-01T09:00:00.000Z", 16093, "business"),
        MileageEntry("m3", "2026-08-02T09:00:00.000Z", 5000, "commute"),
    ]
    rows = list(csv.reader(io.StringIO(mileage_to_csv(trips, rate_lookup=fed))))
    assert rows[0][:3] == ["date", "distance_mi", "distance_m"]
    assert rows[1][1] == "10.0" and rows[1][-1] == "7.25"     # 10 mi × 72.5¢
    assert rows[2][-1] == "7.60"                              # after the July rate change
    assert rows[3][-1] == "0.00"                              # commuting isn't deductible
    metric = list(csv.reader(io.StringIO(mileage_to_csv(trips, unit_system=UnitSystem.METRIC))))
    assert metric[0][1] == "distance_km" and metric[1][1] == "16.1"


# --- database round trip ----------------------------------------------------------------


def _seed(db, platform_id):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO transactions (id, platform_id, kind, gross_amount_minor, platform_fee_minor, currency, "
            "occurred_at, description, external_ref) VALUES (?, ?, 'earning', 2500, 360, 'USD', '2026-09-01T12:00:00Z', "
            "'UberX', 'trip-1')", (new_id(), platform_id))
        conn.execute(
            "INSERT INTO transactions (id, platform_id, kind, gross_amount_minor, currency, base_amount_minor, occurred_at) "
            "VALUES (?, ?, 'tip', 1000, 'EUR', 1100, '2026-09-02T12:00:00Z')", (new_id(), platform_id))
        conn.execute(
            "INSERT INTO transactions (id, platform_id, kind, gross_amount_minor, currency, occurred_at) "
            "VALUES (?, ?, 'tip', 700, 'GBP', '2026-09-03T12:00:00Z')", (new_id(), platform_id))
        conn.execute(
            "INSERT INTO expenses (id, category, amount_minor, currency, vendor, occurred_at) "
            "VALUES (?, 'fuel', 4210, 'USD', 'Shell', '2026-09-30T23:00:00Z')", (new_id(),))
        conn.execute(
            "INSERT INTO expenses (id, category, amount_minor, currency, vendor, occurred_at) "
            "VALUES (?, 'fuel', 999, 'USD', 'Outside range', '2026-10-01T00:00:00Z')", (new_id(),))
        conn.execute("INSERT INTO mileage_logs (id, started_at, distance_m, purpose) VALUES (?, '2026-09-05', 16093, 'business')",
                     (new_id(),))


def test_collect_ledger(db, platform_id):
    _seed(db, platform_id)
    ledger = collect_ledger(db, date(2026, 9, 1), date(2026, 9, 30), currency="USD")
    amounts = [(e.kind, e.amount_minor) for e in ledger.entries]
    assert amounts == [("income", 2500), ("fee", -360), ("income", 1100), ("expense", -4210)]
    assert ledger.warnings and "skipped" in ledger.warnings[0]            # GBP row without base amount

    net = collect_ledger(db, date(2026, 9, 1), date(2026, 9, 30), currency="USD", split_platform_fees=False)
    assert [e.amount_minor for e in net.entries if e.kind == "income"] == [2140, 1100]

    with pytest.raises(ExportError):
        collect_ledger(db, date(2026, 9, 30), date(2026, 9, 1), currency="USD")


@pytest.mark.parametrize("fmt", ["qfx", "ofx", "qif", "csv"])
def test_export_ledger_writes_files(db, platform_id, tmp_path, fmt):
    _seed(db, platform_id)
    report = export_ledger(db, tmp_path / f"out.{fmt}", fmt=fmt, start=date(2026, 9, 1), end=date(2026, 9, 30),
                           currency="USD", intu_bid="12345")
    data = report.path.read_bytes()
    assert report.entries == 4 and data
    if fmt == "csv":
        assert data.startswith(b"\xef\xbb\xbf")                        # BOM for Excel
    if fmt in ("qfx", "ofx"):
        data.decode("ascii")
        assert (b"INTU.BID" in data) == (fmt == "qfx")


def test_export_mileage(db, platform_id, tmp_path):
    _seed(db, platform_id)
    fed, _ = TaxTableRegistry.load().get("US", 2026)
    report = export_mileage(db, tmp_path / "miles.csv", start=date(2026, 9, 1), end=date(2026, 9, 30), rate_table=fed)
    assert report.entries == 1
    assert "10.0" in report.path.read_text(encoding="utf-8-sig")
