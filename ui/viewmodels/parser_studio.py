"""
Parser Studio view-model: the working copy of a statement-format schema being edited in the Admin
Console, plus the local test bench.

The working copy is plain data kept on the admin session (AdminSession.studio), so rebuilding the
page never loses edits. `to_schema()` runs the same validation the app runs when it installs a
bundle (core.financial.parsers.dynamic.ParserSchema), so a schema that passes here installs there.

Test Parse runs on this machine only: the pasted sample never goes to the server, and every amount
in the preview is integer cents.
"""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from core.admin.client import AdminError
from core.financial.ingestion import safe_pattern
from core.financial.money import MoneyError, apply_bp, parse_amount
from core.financial.parsers.dynamic import (DEFAULT_DATE_FORMATS, MONEY_ROLES, ROLES, ParserSchema, Preview,
                                            SchemaError, sample_headers, test_parse)
from ui.viewmodels.admin import AdminSession

# Studio layout: the four groups the Studio shows, in order
ROLE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Earnings", ("gross", "tips", "bonus")),
    ("Fees & pass-through", ("fees", "tolls")),
    ("Dates", ("occurred_at", "paid_out")),
    ("Identifiers", ("reference", "description")),
)
DELIMITERS = {"": "Detect", ",": "Comma", ";": "Semicolon", "\t": "Tab", "|": "Pipe"}
BP_ONE = 10_000

# "Amount is" presets (money roles). Basis points stay under the hood: 10000 = ×1.
AMOUNT_MODES = {"as-is": "As shown in the file", "flip": "Flip the sign (e.g. fees listed as negatives)",
                "percent": "A percentage of the value…"}
# "Pattern" presets: how to pull the amount out of a cell
FIRST_NUMBER = r"(-?\$?\d[\d,]*(?:\.\d+)?)"
PATTERN_MODES = {"whole": "Whole cell", "first-number": "First number in the cell",
                 "after-label": "Number after a label…", "custom": "Custom regex…"}
MAX_SAMPLE_FILE = 64 * 1024

SAMPLE_HINT = ("Date,Trip ID,Fare,Tip,Service Fee,Tolls\n"
               "2026-09-01 08:15,T-1001,$24.50,Tip: $5.00,6.13,2.25")


def blank_rule() -> dict[str, Any]:
    return {"columns": [], "combine": "first", "regex": "", "multiplier_bp": BP_ONE}


# --- presets: plain-language controls over regex and basis points ---------------------------------------


def amount_mode(bp: int) -> tuple[str, str]:
    """(mode, percent text) for a multiplier in basis points."""
    if bp == BP_ONE:
        return "as-is", ""
    if bp == -BP_ONE:
        return "flip", ""
    text = format(Decimal(bp) / 100, "f")
    return "percent", text.rstrip("0").rstrip(".") if "." in text else text


def bp_from(mode: str, percent: str) -> int:
    if mode == "as-is":
        return BP_ONE
    if mode == "flip":
        return -BP_ONE
    try:
        value = Decimal((percent or "").strip().rstrip("%").strip())
    except InvalidOperation:
        raise ValueError("Enter the percentage as a number, e.g. 80 or -12.5") from None
    if value != value.quantize(Decimal("0.01")) or not Decimal(-1000) <= value <= Decimal(1000):
        raise ValueError("Percentages go from -1000 to 1000, with at most two decimals")
    return int(value * 100)


def after_label(label: str) -> str:
    return re.escape(label.strip()) + r"\s*:?\s*" + FIRST_NUMBER


def pattern_mode(regex: str) -> tuple[str, str]:
    """(mode, label-or-custom text) for a stored regex."""
    if not regex:
        return "whole", ""
    if regex == FIRST_NUMBER:
        return "first-number", ""
    suffix = r"\s*:?\s*" + FIRST_NUMBER
    if regex.endswith(suffix):
        escaped = regex[: -len(suffix)]
        label = re.sub(r"\\(.)", r"\1", escaped)
        if re.escape(label) == escaped and label.strip():
            return "after-label", label
    return "custom", regex


def build_pattern(mode: str, text: str) -> str:
    if mode == "whole":
        return ""
    if mode == "first-number":
        return FIRST_NUMBER
    if mode == "after-label":
        if not text.strip():
            raise ValueError("Type the label that comes before the number, e.g. Tip")
        return after_label(text)
    return text.strip()


def first_row(sample: str, delimiter: str | None = None) -> dict[str, str]:
    """The sample's first data row, by header (for the per-column examples)."""
    text = (sample or "")[:MAX_SAMPLE_FILE].lstrip("\ufeff")
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return {}
    try:
        delim = delimiter or csv.Sniffer().sniff(lines[0], delimiters=",;\t|").delimiter
        header, row = list(csv.reader(io.StringIO("\n".join(lines[:2])), delimiter=delim))[:2]
    except (csv.Error, ValueError):
        return {}
    return {h.strip(): v for h, v in zip(header, row)}


def role_example(st: dict[str, Any], role: str) -> str | None:
    """'“Tip: $5.00” reads as $5.00' for the role's first column in the sample's first row."""
    rule = st["working"]["rules"][role]
    if not rule["columns"]:
        return None
    row = first_row(st["sample"], st["working"].get("delimiter") or None)
    column = rule["columns"][0]
    if column not in row:
        return None
    raw = row[column]
    value = raw
    if rule.get("regex"):
        try:
            m = safe_pattern(rule["regex"], what="pattern").search(raw[:1000])
        except Exception as exc:
            return f"Pattern problem: {exc}"
        if m is None:
            return f"\u201c{raw}\u201d has no match (this column would be skipped)"
        value = (m.group("value") if "value" in m.re.groupindex else m.group(1)) or ""
    if role not in MONEY_ROLES:
        return f"\u201c{raw}\u201d reads as {value.strip() or '(empty)'}"
    currency = (st["working"].get("currency") or "USD").upper()
    try:
        cents = parse_amount(value, currency, decimal_separator=st["working"].get("decimal_separator") or ".")
    except (MoneyError, ValueError):
        return f"\u201c{raw}\u201d isn't an amount"
    cents = apply_bp(cents, int(rule.get("multiplier_bp", BP_ONE)))
    from ui.format import money

    return f"\u201c{raw}\u201d reads as {money(cents, currency)}"


def load_sample_file(st: dict[str, Any], path: str) -> None:
    """Use (the start of) a statement file as the sample. Local only."""
    clear_messages(st)
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_SAMPLE_FILE)
    except OSError as exc:
        st["error"] = f"Couldn't open that file: {exc.strerror or exc}"
        return
    text = raw.decode("utf-8", errors="replace")
    if len(raw) == MAX_SAMPLE_FILE:
        text = text[: text.rfind("\n")] if "\n" in text else text     # don't end on half a row
        st["notice"] = "Loaded the first 64 KB of the file (enough for a sample)."
    st["sample"] = text
    read_headers(st)
    if st["headers"] and not st["notice"]:
        st["notice"] = f"Loaded the sample from {path.rsplit('/', 1)[-1]}. It stays on this computer."


def new_working_copy() -> dict[str, Any]:
    return {"id": "", "version": 1, "name": "", "platform": "", "timezone": "", "currency": "USD",
            "date_formats": ", ".join(DEFAULT_DATE_FORMATS), "detect_headers": "", "delimiter": "",
            "decimal_separator": ".", "notes": "", "rules": {r: blank_rule() for r in ROLES}}


def working_copy_from(schema: dict[str, Any]) -> dict[str, Any]:
    w = new_working_copy()
    for key in ("id", "version", "name", "platform", "currency", "notes"):
        if schema.get(key) not in (None, ""):
            w[key] = schema[key]
    w["timezone"] = schema.get("timezone") or ""
    w["delimiter"] = schema.get("delimiter") or ""
    w["decimal_separator"] = schema.get("decimal_separator") or "."
    w["date_formats"] = ", ".join(schema.get("date_formats") or DEFAULT_DATE_FORMATS)
    w["detect_headers"] = ", ".join(schema.get("detect_headers") or ())
    for role, rule in (schema.get("rules") or {}).items():
        if role in ROLES and isinstance(rule, dict):
            cols = rule.get("columns") or []
            w["rules"][role] = {"columns": [cols] if isinstance(cols, str) else list(cols),
                                "combine": rule.get("combine", "first"), "regex": rule.get("regex") or "",
                                "multiplier_bp": int(rule.get("multiplier_bp", BP_ONE))}
    return w


def _split(value: str) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def schema_dict(w: dict[str, Any]) -> dict[str, Any]:
    """The working copy as a schema document (unvalidated)."""
    rules: dict[str, Any] = {}
    for role, r in w["rules"].items():
        cols = [c for c in r.get("columns") or [] if c.strip()]
        if not cols:
            continue
        rule: dict[str, Any] = {"columns": cols}
        if r.get("combine", "first") != "first":
            rule["combine"] = r["combine"]
        if (r.get("regex") or "").strip():
            rule["regex"] = r["regex"].strip()
        if role in MONEY_ROLES and int(r.get("multiplier_bp", BP_ONE)) != BP_ONE:
            rule["multiplier_bp"] = int(r["multiplier_bp"])
        rules[role] = rule
    return {"format": 1, "id": (w["id"] or "").strip().lower(), "version": int(w["version"]),
            "name": (w["name"] or "").strip(), "platform": (w["platform"] or "").strip(), "rules": rules,
            "date_formats": _split(w["date_formats"]) or list(DEFAULT_DATE_FORMATS),
            "timezone": (w["timezone"] or "").strip() or None, "currency": (w["currency"] or "USD").strip().upper(),
            "decimal_separator": w["decimal_separator"] or ".", "delimiter": w["delimiter"] or None,
            "detect_headers": _split(w["detect_headers"]), "notes": (w["notes"] or "").strip()}


def to_schema(w: dict[str, Any]) -> ParserSchema:
    """Validate exactly as the app will when it installs the bundle. Raises SchemaError."""
    for role, rule in w["rules"].items():               # choices left half-done in the plain-language controls
        if not rule.get("columns"):
            continue
        mode, text = rule.get("ui_pattern") or ("", "")
        if mode in ("after-label", "custom") and not text.strip():
            raise SchemaError(f"{ROLES[role]['label']}: " + ("type the label that comes before the number"
                                                             if mode == "after-label" else "enter the regex"))
        amode, percent = rule.get("ui_amount") or ("", "")
        if amode == "percent" and not percent.strip():
            raise SchemaError(f"{ROLES[role]['label']}: enter the percentage")
    try:
        return ParserSchema.from_dict(schema_dict(w))
    except (TypeError, ValueError) as exc:          # e.g. a non-numeric version
        if isinstance(exc, SchemaError):
            raise
        raise SchemaError(str(exc)) from None


# --- session-level actions -------------------------------------------------------------------------------


def studio(s: AdminSession) -> dict[str, Any]:
    st = s.studio
    st.setdefault("working", new_working_copy())
    st.setdefault("open_id", None)                   # None: a new, unsaved format
    st.setdefault("sample", "")
    st.setdefault("headers", [])
    st.setdefault("preview", None)
    st.setdefault("error", None)
    st.setdefault("notice", None)
    st.setdefault("bundle", None)
    st.setdefault("history", [])
    return st


def clear_messages(st: dict[str, Any]) -> None:
    st["error"] = st["notice"] = None


def start_new(s: AdminSession) -> None:
    st = studio(s)
    st.update(working=new_working_copy(), open_id=None, preview=None, history=[], bundle=None)
    clear_messages(st)


def open_schema(s: AdminSession, schema_id: str) -> None:
    """Load a format for editing: its draft if there is one, else the next version of the published one."""
    st = studio(s)
    clear_messages(st)
    detail = s.client.parser(schema_id)
    versions = detail.get("versions") or []
    draft = next((v for v in versions if v["status"] == "draft"), None)
    published = next((v for v in versions if v["status"] == "published"), None)
    base = draft or published
    if base is None:
        raise AdminError("That format has no versions")
    w = working_copy_from(base["schema"])
    if draft is None and published is not None:
        w["version"] = int(published["version"]) + 1
        st["notice"] = f"Editing a new draft (v{w['version']}) based on published v{published['version']}."
    st.update(working=w, open_id=schema_id, preview=None, history=versions, bundle=None)


def read_headers(st: dict[str, Any]) -> list[str]:
    clear_messages(st)
    try:
        st["headers"] = sample_headers(st["sample"], st["working"].get("delimiter") or None)
    except Exception:                                 # csv.Sniffer raises on odd input
        st["headers"] = []
    if not st["headers"]:
        st["error"] = "Paste a sample that starts with a header line."
    return st["headers"]


def run_test(st: dict[str, Any]) -> Preview | None:
    clear_messages(st)
    st["preview"] = None
    try:
        schema = to_schema(st["working"])
    except SchemaError as exc:
        st["error"] = str(exc)
        return None
    try:
        st["preview"] = test_parse(schema, st["sample"])
    except Exception as exc:                          # malformed CSV: show it, don't crash the console
        st["error"] = f"Couldn't read the sample: {exc}"
        return None
    if not st["headers"]:
        st["headers"] = st["preview"].headers
    return st["preview"]


def save_draft(s: AdminSession) -> bool:
    st = studio(s)
    clear_messages(st)
    try:
        schema = to_schema(st["working"])
        s.client.save_parser(schema.to_dict())
    except SchemaError as exc:
        st["error"] = str(exc)
        return False
    except AdminError as exc:
        st["error"] = exc.message
        return False
    st["open_id"] = schema.id
    st["notice"] = f"Saved draft v{schema.version} of {schema.id}. Publish it to ship it to apps."
    _reload(s, schema.id)
    return True


def publish(s: AdminSession) -> bool:
    st = studio(s)
    clear_messages(st)
    schema_id = st["open_id"]
    if not schema_id:
        st["error"] = "Save a draft first."
        return False
    try:
        result = s.client.publish_parser(schema_id)
    except AdminError as exc:
        st["error"] = exc.message
        return False
    dist, problem = result.get("distribution"), result.get("distribution_error")
    if dist:
        st["notice"] = f"Published {schema_id} v{result.get('version')} and sent it to the CDN. Apps pick it up within a day."
    elif problem:
        st["notice"] = f"Published {schema_id} v{result.get('version')}."
        st["error"] = f"It isn't on the CDN yet: {problem} Fix that, then use \u201cSend to CDN\u201d."
    else:
        st["notice"] = (f"Published {schema_id} v{result.get('version')}. This server has no CDN target, so share "
                        "the signed bundle file instead.")
    _reload(s, schema_id)
    w = st["working"]
    w["version"] = int(result.get("version", w["version"])) + 1      # further edits become the next draft
    return True


def distribute(s: AdminSession) -> bool:
    st = studio(s)
    clear_messages(st)
    try:
        out = s.client.distribute_parsers()
    except AdminError as exc:
        st["error"] = exc.message
        return False
    st["notice"] = f"Sent {out['schemas']} format(s) to the CDN. Apps pick them up within a day."
    _reload(s, st["open_id"]) if st["open_id"] else _reload_list(s)
    return True


def _reload_list(s: AdminSession) -> None:
    try:
        s.data["parsers"] = s.client.parsers()
    except AdminError as exc:
        s.errors["parsers"] = exc.message


def export_bundle(s: AdminSession) -> bool:
    st = studio(s)
    clear_messages(st)
    try:
        st["bundle"] = s.client.parser_bundle()
    except AdminError as exc:
        st["error"] = exc.message
        return False
    return True


def _reload(s: AdminSession, schema_id: str) -> None:
    try:
        s.data["parsers"] = s.client.parsers()
        st = studio(s)
        st["history"] = s.client.parser(schema_id).get("versions") or []
    except AdminError as exc:
        s.errors["parsers"] = exc.message
