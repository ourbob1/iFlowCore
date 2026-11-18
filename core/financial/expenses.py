"""
Expense categorization and receipt attachments.

Auto-tagging
------------
`AutoTagger` assigns one of the schema's expense categories from a vendor
name and/or description using keyword rules:

  1. user rules (highest priority — explicit choices always win)
  2. learned vendor history (what this user filed that vendor under before)
  3. built-in keyword rules

Keywords match on word boundaries, case-insensitively, after light
normalization ("AT&T", "at & t", "ATT" are all found). Among competing rule
matches the highest priority wins, then the longest (most specific) keyword:
"shell car wash" beats "shell". Every result says *why* it was chosen, so the
UI can show "Fuel — matched 'chevron'" and the user can correct it.

Receipts
--------
`attach_receipt` links a local file (photo or PDF) to an expense or income
transaction. Two storage modes:

  * managed (default) — the file is copied into the app's receipt store and
    named by its SHA-256, so moving/deleting the original can't break the
    link and duplicate uploads are stored once.
  * linked — only the path is recorded (for users who keep receipts in their
    own folder structure). `verify_receipts` reports missing/changed files.

NOTE: SQLCipher encrypts the database, not files beside it. Managed receipts
are plain files in the app data directory until encrypted blob storage lands
(tracked for the security phase).
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

from core.financial.money import apply_bp

# Must mirror the CHECK constraint on expenses.category in schema.sql
# (tests/test_expenses.py asserts they stay in sync).
EXPENSE_CATEGORIES = (
    "vehicle", "fuel", "maintenance", "insurance", "parking_tolls",
    "phone", "internet", "equipment", "supplies", "software",
    "platform_fees", "meals", "travel", "home_office",
    "professional_services", "education", "other",
)

# Categories replaced by the standard mileage deduction when a user chooses
# that method (US rules: you deduct miles × rate *instead of* actual car costs;
# parking and tolls remain separately deductible).
VEHICLE_OPERATING_CATEGORIES = frozenset({"vehicle", "fuel", "maintenance", "insurance"})

# Share of the expense that is deductible by category, in basis points.
# US: business meals are 50% deductible.
CATEGORY_DEDUCTIBLE_BP: Mapping[str, int] = {"meals": 5000}


class ExpenseError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Auto-tagger
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    text = text.casefold().replace("&", " and ").replace("'", "")
    text = re.sub(r"[^0-9a-z]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class KeywordRule:
    category: str
    keywords: tuple[str, ...]
    priority: int = 0
    source: Literal["builtin", "user", "learned"] = "builtin"

    def __post_init__(self) -> None:
        if self.category not in EXPENSE_CATEGORIES:
            raise ExpenseError(f"Unknown category '{self.category}'")
        normalized = tuple(k for k in (_normalize(k) for k in self.keywords) if k)
        if not normalized:
            raise ExpenseError("A rule needs at least one keyword")
        object.__setattr__(self, "keywords", normalized)


@dataclass(frozen=True)
class TagResult:
    category: str
    confidence: float          # 0..1 heuristic, for UI hints only
    matched_keyword: str | None
    source: str                # builtin | user | learned | default


# Built-in rules tuned for gig work. Priority breaks ties between categories
# (e.g. "shell car wash" should be maintenance, not fuel).
BUILTIN_RULES: tuple[KeywordRule, ...] = (
    KeywordRule("fuel", (
        "shell", "chevron", "exxon", "exxonmobil", "mobil", "bp", "arco", "sunoco", "marathon",
        "speedway", "valero", "citgo", "circle k", "phillips 66", "conoco", "sinclair",
        "quiktrip", "racetrac", "sheetz", "loves", "pilot", "flying j", "casey s",
        "costco gas", "sams club fuel", "gas station", "gasoline", "fuel", "diesel",
        "ev charging", "supercharger", "chargepoint", "electrify america", "evgo",
    )),
    KeywordRule("maintenance", (
        "oil change", "jiffy lube", "valvoline", "midas", "meineke", "pep boys", "firestone",
        "goodyear", "discount tire", "tire", "tires", "autozone", "advance auto", "oreilly auto",
        "napa auto", "mechanic", "auto repair", "car wash", "detailing", "brake", "alignment",
        "windshield", "safelite", "smog", "inspection",
    ), priority=5),
    KeywordRule("insurance", (
        "geico", "progressive", "state farm", "allstate", "liberty mutual", "farmers insurance",
        "usaa", "nationwide", "insurance", "rideshare insurance",
    )),
    KeywordRule("vehicle", ("car payment", "auto loan", "lease payment", "dmv", "registration", "hertz", "hyrecar", "getaround")),
    KeywordRule("parking_tolls", (
        "parking", "parkmobile", "spothero", "paybyphone", "toll", "tolls", "ez pass", "e zpass",
        "ezpass", "fastrak", "sunpass", "txtag", "ipass", "peach pass", "good to go",
    ), priority=3),
    KeywordRule("phone", (
        "verizon wireless", "verizon", "at and t", "att", "t mobile", "tmobile", "sprint",
        "mint mobile", "cricket", "boost mobile", "google fi", "us cellular",
        "phone mount", "phone bill",
    )),
    KeywordRule("internet", ("comcast", "xfinity", "spectrum", "cox communications", "frontier", "starlink", "fios", "internet")),
    KeywordRule("equipment", ("dash cam", "dashcam", "phone charger", "hot bag", "insulated bag", "delivery bag", "ladder", "drill", "toolkit", "power bank", "bike", "e bike", "helmet")),
    KeywordRule("supplies", ("staples", "office depot", "officemax", "supplies", "cleaning", "wipes", "water bottles", "gum", "mints", "paper towels", "hand sanitizer")),
    KeywordRule("software", ("gridwise", "stride", "everlance", "quickbooks", "turbotax", "adobe", "google workspace", "microsoft 365", "dropbox", "icloud", "subscription", "app store")),
    KeywordRule("platform_fees", ("background check", "checkr", "platform fee", "service fee", "taskrabbit registration", "instant pay fee", "cash out fee")),
    KeywordRule("meals", ("restaurant", "cafe", "coffee", "starbucks", "dunkin", "mcdonalds", "chipotle", "subway", "lunch", "dinner", "meal")),
    KeywordRule("travel", ("airbnb", "hotel", "marriott", "hilton", "airlines", "delta", "united airlines", "southwest", "amtrak", "greyhound")),
    KeywordRule("professional_services", ("cpa", "accountant", "tax prep", "h and r block", "attorney", "lawyer", "legal")),
    KeywordRule("education", ("course", "udemy", "coursera", "training", "certification", "workshop")),
)


class AutoTagger:
    """Keyword-based expense categorizer with user overrides and learning."""

    def __init__(
        self,
        rules: Iterable[KeywordRule] = BUILTIN_RULES,
        *,
        user_rules: Iterable[KeywordRule] = (),
        vendor_history: Mapping[str, str] | None = None,
        default_category: str = "other",
    ) -> None:
        if default_category not in EXPENSE_CATEGORIES:
            raise ExpenseError(f"Unknown default category '{default_category}'")
        self._rules = [*(
            KeywordRule(r.category, r.keywords, r.priority + 1000, "user") for r in user_rules
        ), *rules]
        self._vendor_history = {_normalize(k): v for k, v in (vendor_history or {}).items()}
        self._default = default_category
        # Pre-compile one regex per keyword (word boundaries on normalized text).
        self._compiled = [
            (rule, kw, re.compile(rf"(?:^| ){re.escape(kw)}(?: |$)")) for rule in self._rules for kw in rule.keywords
        ]

    def tag(self, vendor: str | None = None, description: str | None = None) -> TagResult:
        vendor_norm = _normalize(vendor or "")
        text = _normalize(" ".join(filter(None, [vendor, description])))

        best: tuple[int, int, KeywordRule, str] | None = None
        for rule, kw, rx in self._compiled:
            if rx.search(text):
                key = (rule.priority, len(kw))
                if best is None or key > best[:2]:
                    best = (*key, rule, kw)

        if best and best[2].source == "user":
            return TagResult(best[2].category, 0.99, best[3], "user")
        if vendor_norm and vendor_norm in self._vendor_history:
            return TagResult(self._vendor_history[vendor_norm], 0.95, vendor_norm, "learned")
        if best:
            confidence = 0.9 if len(best[3]) >= 5 else 0.75
            return TagResult(best[2].category, confidence, best[3], "builtin")
        return TagResult(self._default, 0.0, None, "default")


def load_vendor_history(db: Any, *, min_count: int = 1) -> dict[str, str]:
    """Most frequent category per vendor from the user's existing expenses.

    Ties go to the most recently used category.
    """
    sql = """
        SELECT vendor, category, count(*) AS n, max(updated_at) AS last_used
          FROM expenses
         WHERE deleted_at IS NULL AND vendor IS NOT NULL AND trim(vendor) <> ''
         GROUP BY lower(trim(vendor)), category
    """
    best: dict[str, tuple[int, str, str]] = {}
    with db.connection() as conn:
        for row in conn.execute(sql):
            key = _normalize(row["vendor"])
            candidate = (row["n"], row["last_used"], row["category"])
            if row["n"] >= min_count and (key not in best or candidate > best[key]):
                best[key] = candidate
    return {k: v[2] for k, v in best.items()}


# ---------------------------------------------------------------------------
# Expense records
# ---------------------------------------------------------------------------


def create_expense(
    db: Any,
    *,
    amount_minor: int,
    currency: str,
    occurred_at: str,
    vendor: str | None = None,
    description: str | None = None,
    category: str | None = None,
    tagger: AutoTagger | None = None,
    platform_id: str | None = None,
    business_use_bp: int = 10_000,
    base_amount_minor: int | None = None,
    payment_method: str | None = None,
    device_id: str | None = None,
) -> tuple[str, TagResult]:
    """Insert an expense, auto-categorizing when `category` is None."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise ExpenseError("amount_minor must be int minor units")
    if category is None:
        result = (tagger or AutoTagger()).tag(vendor, description)
    else:
        if category not in EXPENSE_CATEGORIES:
            raise ExpenseError(f"Unknown category '{category}'")
        result = TagResult(category, 1.0, None, "user")

    expense_id = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO expenses (id, platform_id, category, amount_minor, currency, base_amount_minor,
                   business_use_bp, occurred_at, vendor, description, payment_method, last_modified_device)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (expense_id, platform_id, result.category, amount_minor, currency.upper(), base_amount_minor,
             business_use_bp, occurred_at, vendor, description, payment_method, device_id),
        )
    return expense_id, result


def deductible_amount(amount_minor: int, category: str, business_use_bp: int) -> int:
    """Deductible portion: business-use share, then any category limit (meals 50%)."""
    business = apply_bp(amount_minor, business_use_bp)
    return apply_bp(business, CATEGORY_DEDUCTIBLE_BP.get(category, 10_000))


def retag_uncategorized(db: Any, tagger: AutoTagger | None = None) -> int:
    """Re-run tagging on expenses still filed as 'other'. Returns rows changed."""
    tagger = tagger or AutoTagger(vendor_history=load_vendor_history(db))
    changed = 0
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT id, vendor, description FROM expenses WHERE category = 'other' AND deleted_at IS NULL"
        ).fetchall()
        for row in rows:
            result = tagger.tag(row["vendor"], row["description"])
            if result.category != "other":
                conn.execute("UPDATE expenses SET category = ? WHERE id = ?", (result.category, row["id"]))
                changed += 1
    return changed


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------

ALLOWED_RECEIPT_TYPES: Mapping[str, str] = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    ".heic": "image/heic", ".heif": "image/heif", ".pdf": "application/pdf",
    ".gif": "image/gif", ".tif": "image/tiff", ".tiff": "image/tiff",
}
MAX_RECEIPT_BYTES = 25 * 1024 * 1024

# File signatures, so a renamed .exe can't pose as a receipt.
_MAGIC: Mapping[str, tuple[bytes, ...]] = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "application/pdf": (b"%PDF-",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/tiff": (b"II*\x00", b"MM\x00*"),
}


@dataclass(frozen=True)
class ReceiptInfo:
    id: str
    file_path: str
    sha256: str
    mime_type: str
    size_bytes: int
    storage_mode: str
    deduplicated: bool = False


@dataclass
class ReceiptCheck:
    ok: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _sniff_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = ALLOWED_RECEIPT_TYPES.get(suffix)
    if mime is None:
        guessed = mimetypes.guess_type(path.name)[0]
        raise ExpenseError(f"Unsupported receipt type '{suffix or guessed}'. Allowed: {sorted(ALLOWED_RECEIPT_TYPES)}")
    signatures = _MAGIC.get(mime)
    if signatures:
        with path.open("rb") as fh:
            head = fh.read(16)
        if not any(head.startswith(sig) for sig in signatures):
            raise ExpenseError(f"{path.name} does not look like a real {mime} file")
    if mime == "image/webp":
        with path.open("rb") as fh:
            head = fh.read(12)
        if not (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
            raise ExpenseError(f"{path.name} does not look like a real image/webp file")
    return mime


def attach_receipt(
    db: Any,
    file_path: str | Path,
    *,
    expense_id: str | None = None,
    transaction_id: str | None = None,
    store_dir: str | Path | None = None,
    mode: Literal["managed", "linked"] = "managed",
    device_id: str | None = None,
) -> ReceiptInfo:
    """Link a local receipt file to exactly one expense or income transaction.

    In managed mode `store_dir` is required (normally `<data_dir>/receipts`).
    The first receipt attached to an expense also becomes its `receipt_uri`.
    """
    if (expense_id is None) == (transaction_id is None):
        raise ExpenseError("Provide exactly one of expense_id or transaction_id")
    if mode not in ("managed", "linked"):
        raise ExpenseError("mode must be 'managed' or 'linked'")

    src = Path(file_path).expanduser()
    if not src.is_file():
        raise ExpenseError(f"Receipt file not found: {src}")
    size = src.stat().st_size
    if size == 0:
        raise ExpenseError("Receipt file is empty")
    if size > MAX_RECEIPT_BYTES:
        raise ExpenseError(f"Receipt exceeds {MAX_RECEIPT_BYTES // (1024 * 1024)} MB")
    mime = _sniff_mime(src)
    digest = _sha256(src)

    deduplicated = False
    if mode == "managed":
        if store_dir is None:
            raise ExpenseError("store_dir is required for managed receipts")
        store = Path(store_dir).expanduser()
        target = store / digest[:2] / f"{digest}{src.suffix.lower()}"
        if target.exists():
            deduplicated = True
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".part")
            shutil.copyfile(src, tmp)
            if _sha256(tmp) != digest:          # guard against a file changing mid-copy
                tmp.unlink(missing_ok=True)
                raise ExpenseError("Receipt changed while being copied; try again")
            tmp.replace(target)
        stored_path = str(target.resolve())
    else:
        stored_path = str(src.resolve())

    receipt_id = str(uuid.uuid4())
    with db.transaction() as conn:
        parent_table = "expenses" if expense_id else "transactions"
        parent_id = expense_id or transaction_id
        if conn.execute(
            f"SELECT 1 FROM {parent_table} WHERE id = ? AND deleted_at IS NULL", (parent_id,)
        ).fetchone() is None:
            raise ExpenseError(f"No live {parent_table[:-1]} with id {parent_id}")
        conn.execute(
            """INSERT INTO receipts (id, expense_id, transaction_id, file_path, storage_mode, original_filename,
                   mime_type, size_bytes, sha256, last_modified_device)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (receipt_id, expense_id, transaction_id, stored_path, mode, src.name, mime, size, digest, device_id),
        )
        if expense_id:
            conn.execute(
                "UPDATE expenses SET receipt_uri = ? WHERE id = ? AND receipt_uri IS NULL",
                (stored_path, expense_id),
            )
    return ReceiptInfo(receipt_id, stored_path, digest, mime, size, mode, deduplicated)


def list_receipts(db: Any, *, expense_id: str | None = None, transaction_id: str | None = None) -> list[dict[str, Any]]:
    column, value = ("expense_id", expense_id) if expense_id else ("transaction_id", transaction_id)
    if value is None:
        raise ExpenseError("Provide expense_id or transaction_id")
    with db.connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM receipts WHERE {column} = ? AND deleted_at IS NULL ORDER BY created_at", (value,)
        ).fetchall()
    return [dict(r) for r in rows]


def detach_receipt(db: Any, receipt_id: str) -> None:
    """Soft-delete the link (the managed file may be shared, so it is kept)."""
    with db.transaction() as conn:
        row = conn.execute("SELECT expense_id, file_path FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
        if row is None:
            raise ExpenseError(f"No receipt {receipt_id}")
        conn.execute(
            "UPDATE receipts SET deleted_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?", (receipt_id,)
        )
        if row["expense_id"]:
            nxt = conn.execute(
                "SELECT file_path FROM receipts WHERE expense_id = ? AND deleted_at IS NULL ORDER BY created_at LIMIT 1",
                (row["expense_id"],),
            ).fetchone()
            conn.execute(
                "UPDATE expenses SET receipt_uri = ? WHERE id = ? AND receipt_uri IS ?",
                (nxt["file_path"] if nxt else None, row["expense_id"], row["file_path"]),
            )


def verify_receipts(db: Any) -> ReceiptCheck:
    """Check every live receipt still exists and is unchanged (hash match)."""
    check = ReceiptCheck()
    with db.connection() as conn:
        rows = conn.execute("SELECT id, file_path, sha256 FROM receipts WHERE deleted_at IS NULL").fetchall()
    for row in rows:
        path = Path(row["file_path"])
        if not path.is_file():
            check.missing.append(row["id"])
        elif _sha256(path) != row["sha256"]:
            check.modified.append(row["id"])
        else:
            check.ok.append(row["id"])
    return check
