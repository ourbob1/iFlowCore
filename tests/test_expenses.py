"""Expense auto-tagging, deductibility, and receipt attachments."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.financial.expenses import (
    EXPENSE_CATEGORIES,
    AutoTagger,
    ExpenseError,
    KeywordRule,
    attach_receipt,
    create_expense,
    deductible_amount,
    detach_receipt,
    list_receipts,
    load_vendor_history,
    retag_uncategorized,
    verify_receipts,
)

from .conftest import new_id

ROOT = Path(__file__).resolve().parents[1]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n%fake receipt\n"


def test_categories_match_schema_check_constraint():
    schema = (ROOT / "database" / "schema.sql").read_text()
    block = re.search(r"category\s+TEXT NOT NULL DEFAULT 'other' CHECK \(category IN \((.*?)\)\)", schema, re.S)
    assert block, "expenses.category CHECK not found"
    assert set(re.findall(r"'([a-z_]+)'", block.group(1))) == set(EXPENSE_CATEGORIES)


@pytest.mark.parametrize("vendor,description,expected", [
    ("Shell Oil 57442", None, "fuel"),
    ("CHEVRON #0021", "", "fuel"),
    ("Electrify America", "DC fast charge", "fuel"),
    ("Jiffy Lube", "Oil change", "maintenance"),
    ("Shell Car Wash", None, "maintenance"),         # higher-priority, longer match beats "shell"
    ("AT&T", "monthly bill", "phone"),
    ("T-Mobile", None, "phone"),
    ("Staples", "printer paper", "supplies"),
    ("E-ZPass NY", None, "parking_tolls"),
    ("SpotHero", "garage", "parking_tolls"),
    ("GEICO", "auto policy", "insurance"),
    ("Checkr", "background check renewal", "platform_fees"),
    ("Gridwise", "Pro subscription", "software"),
    (None, "insulated delivery bag", "equipment"),
    ("Xfinity", None, "internet"),
])
def test_builtin_tagging(vendor, description, expected):
    result = AutoTagger().tag(vendor, description)
    assert result.category == expected, result
    assert result.source == "builtin" and result.matched_keyword


def test_word_boundaries_prevent_false_positives():
    # "bp" must not match inside "bpm", "tire" not inside "entire".
    assert AutoTagger().tag("BPM Music", "entire album").category == "other"


def test_unmatched_defaults_to_other_with_zero_confidence():
    result = AutoTagger().tag("Zyxwv LLC", "misc")
    assert (result.category, result.confidence, result.source) == ("other", 0.0, "default")


def test_user_rules_override_builtins():
    tagger = AutoTagger(user_rules=[KeywordRule("equipment", ("shell",))])
    assert tagger.tag("Shell", None).category == "equipment"
    assert tagger.tag("Shell", None).source == "user"


def test_learned_history_beats_builtin_but_not_user():
    tagger = AutoTagger(vendor_history={"Costco": "supplies"})
    assert tagger.tag("COSTCO", None).category == "supplies"
    assert AutoTagger(vendor_history={"Costco": "supplies"}, user_rules=[KeywordRule("fuel", ("costco",))]).tag(
        "Costco", None).category == "fuel"


def test_invalid_rules_rejected():
    with pytest.raises(ExpenseError):
        KeywordRule("groceries", ("aldi",))
    with pytest.raises(ExpenseError):
        KeywordRule("fuel", ("   ",))


@pytest.mark.parametrize("amount,category,bp,expected", [
    (10000, "phone", 6000, 6000),     # 60% business use
    (10000, "meals", 10000, 5000),    # meals 50%
    (3333, "meals", 10000, 1667),     # half-up
    (999, "meals", 5000, 250),        # 499.5→500 business, then 250
    (10000, "fuel", 0, 0),
])
def test_deductible_amount(amount, category, bp, expected):
    assert deductible_amount(amount, category, bp) == expected


# --- database-backed ----------------------------------------------------------------


def test_create_expense_autotags(db):
    eid, result = create_expense(db, amount_minor=4210, currency="usd", occurred_at="2026-09-02T09:00:00Z",
                                 vendor="Chevron #12")
    assert result.category == "fuel"
    with db.connection() as conn:
        row = conn.execute("SELECT category, currency, amount_minor FROM expenses WHERE id = ?", (eid,)).fetchone()
    assert (row["category"], row["currency"], row["amount_minor"]) == ("fuel", "USD", 4210)


def test_create_expense_rejects_float_money(db):
    with pytest.raises(ExpenseError):
        create_expense(db, amount_minor=42.10, currency="USD", occurred_at="2026-09-02")  # type: ignore[arg-type]


def test_vendor_history_and_retag(db):
    for _ in range(2):
        create_expense(db, amount_minor=500, currency="USD", occurred_at="2026-09-01", vendor="Joe's Corner", category="supplies")
    create_expense(db, amount_minor=500, currency="USD", occurred_at="2026-09-01", vendor="Joe's Corner", category="meals")
    assert load_vendor_history(db)["joes corner"] == "supplies"

    create_expense(db, amount_minor=700, currency="USD", occurred_at="2026-09-03", vendor="joes corner", category="other")
    assert retag_uncategorized(db) == 1
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM expenses WHERE category = 'other'").fetchone()[0] == 0


def _expense(db):
    return create_expense(db, amount_minor=1999, currency="USD", occurred_at="2026-09-02", vendor="Staples")[0]


def test_managed_receipt_copy_dedupe_and_primary_uri(db, tmp_path):
    src = tmp_path / "IMG_0001.PNG"
    src.write_bytes(PNG)
    store = tmp_path / "receipts"
    eid = _expense(db)

    first = attach_receipt(db, src, expense_id=eid, store_dir=store)
    assert Path(first.file_path).is_file() and Path(first.file_path).parent.parent == store.resolve()
    assert first.mime_type == "image/png" and not first.deduplicated

    second = attach_receipt(db, src, expense_id=eid, store_dir=store)   # same bytes again
    assert second.deduplicated and second.file_path == first.file_path
    src.unlink()                                                        # original gone; managed copy survives
    assert verify_receipts(db).missing == []

    with db.connection() as conn:
        assert conn.execute("SELECT receipt_uri FROM expenses WHERE id = ?", (eid,)).fetchone()[0] == first.file_path
    assert len(list_receipts(db, expense_id=eid)) == 2


def test_linked_receipt_verification(db, tmp_path, platform_id):
    with db.transaction() as conn:
        tid = new_id()
        conn.execute("INSERT INTO transactions (id, platform_id, gross_amount_minor, currency, occurred_at) "
                     "VALUES (?, ?, 1000, 'USD', '2026-09-01')", (tid, platform_id))
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(PDF)
    info = attach_receipt(db, doc, transaction_id=tid, mode="linked")
    assert info.file_path == str(doc.resolve())
    doc.write_bytes(PDF + b"edited")
    assert verify_receipts(db).modified == [info.id]
    doc.unlink()
    assert verify_receipts(db).missing == [info.id]


def test_detach_moves_primary_uri_to_next_receipt(db, tmp_path):
    eid = _expense(db)
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    a.write_bytes(PDF + b"a")
    b.write_bytes(PDF + b"b")
    ra = attach_receipt(db, a, expense_id=eid, mode="linked")
    rb = attach_receipt(db, b, expense_id=eid, mode="linked")
    detach_receipt(db, ra.id)
    with db.connection() as conn:
        assert conn.execute("SELECT receipt_uri FROM expenses WHERE id = ?", (eid,)).fetchone()[0] == rb.file_path
    assert [r["id"] for r in list_receipts(db, expense_id=eid)] == [rb.id]


@pytest.mark.parametrize("name,content,message", [
    ("malware.exe", b"MZ", "Unsupported"),
    ("fake.png", b"MZ\x90\x00 not a png", "does not look like"),
    ("empty.jpg", b"", "empty"),
])
def test_receipt_file_validation(db, tmp_path, name, content, message):
    path = tmp_path / name
    path.write_bytes(content)
    with pytest.raises(ExpenseError, match=message):
        attach_receipt(db, path, expense_id=_expense(db), mode="linked")


def test_receipt_needs_exactly_one_live_parent(db, tmp_path):
    doc = tmp_path / "r.pdf"
    doc.write_bytes(PDF)
    with pytest.raises(ExpenseError, match="exactly one"):
        attach_receipt(db, doc, mode="linked")
    with pytest.raises(ExpenseError, match="No live"):
        attach_receipt(db, doc, expense_id=new_id(), mode="linked")
    with pytest.raises(ExpenseError, match="store_dir"):
        attach_receipt(db, doc, expense_id=_expense(db))
