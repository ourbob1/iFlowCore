"""
Grounded tax Q&A, deterministic first.

The app sends its own computed figures (see core/ai/tax_assistant.py in the
app); it never sends transactions, vendors or locations. Answering goes in
three layers, because small CPU models are unreliable with numbers:

  1. Topic gate: questions with no tax/money vocabulary are declined before
     any model runs.
  2. Quick answers: common questions (the app's suggested ones among them)
     are answered from templates over the facts: exact and instant.
  3. LLM fallback for everything else, then a grounding check: every number
     in the answer must appear in the facts (or be one of a few fixed,
     well-known rates). If not, the answer is replaced by a plain summary of
     the user's real figures.
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

MAX_CONTEXT_CHARS = 8000          # rendered facts including field notes (~2 KB of the budget)
MAX_QUESTION_CHARS = 500
DISCLAIMER = "Planning estimate, not tax advice."

SYSTEM_PROMPT = (
    "You are the tax helper inside iFlow Core, an app for US gig workers and independent contractors. "
    "Answer the user's question using ONLY the FACTS provided (computed by the app's tax engine for this user) "
    "and widely known, general US self-employment tax rules. Rules:\n"
    "1. Quote figures exactly as they appear in FACTS. Prefer the precomputed fields (still_to_set_aside, "
    "set_aside_per_month_to_catch_up, next_estimated_payment, covered_so_far) over doing your own arithmetic; "
    "if you must calculate, show it briefly.\n"
    "2. Each fact's meaning is given in brackets; use the right one (e.g. effective vs marginal rate).\n"
    "3. If FACTS don't contain what's needed, say so and suggest what to enter in the app or to ask a tax professional.\n"
    "4. Never invent numbers, dates, deadlines, forms, or rules. Never recommend illegal or aggressive strategies.\n"
    "5. If the question is not about the user's taxes, income, expenses or mileage, reply only: "
    "\"I can only help with questions about your taxes in iFlow Core.\"\n"
    "6. Be brief: at most 5 short sentences or bullets, plain language, no markdown tables.\n"
    "7. This is a planning estimate, not tax advice."
)

# Meanings for the fields the app sends (core/ai/tax_assistant.py). Unknown keys are passed through as-is.
FIELD_NOTES = {
    "as_of": "today's date",
    "gross_receipts": "income from platforms before fees, this year so far",
    "platform_fees": "fees platforms kept",
    "deductible_expenses": "business expenses after business-use share and limits",
    "business_miles": "business miles driven this year",
    "mileage_deduction": "standard mileage deduction for those miles",
    "net_profit": "gross receipts minus fees, expenses and mileage (Schedule C profit)",
    "self_employment_tax": "Social Security + Medicare on self-employment earnings (15.3% on 92.35% of net profit, "
                           "Social Security part capped)",
    "se_tax_deductible_half": "half of SE tax, deducted from income",
    "adjusted_gross_income": "AGI",
    "qbi_deduction": "qualified business income deduction (up to 20%)",
    "federal_taxable_income": "income the federal brackets apply to",
    "federal_income_tax": "federal income tax, not counting SE tax",
    "federal_marginal_rate": "federal bracket rate on the next dollar earned",
    "state_income_tax": "state income tax estimate",
    "total_estimated_tax": "SE tax + federal + state income tax for the year",
    "effective_rate": "total estimated tax divided by net profit plus other income",
    "balance_due": "total estimated tax minus tax already withheld",
    "quarterly_payments": "the year's four estimated payments; status past/upcoming relative to as_of",
    "next_estimated_payment": "the next estimated payment due date and amount",
    "tax_reserve_set_aside": "money the user has put aside for taxes this year",
    "estimated_payments_made": "estimated tax payments already sent this year",
    "covered_so_far": "set aside plus already paid",
    "still_to_set_aside": "how much more to put aside to cover the total estimated tax",
    "months_left_in_tax_year": "months left including this one",
    "set_aside_per_month_to_catch_up": "still_to_set_aside spread over the months left",
    "notes": "caveats from the tax engine",
}

_INJECTION = re.compile(r"(?i)\b(ignore|disregard)\b.{0,40}\b(instructions|rules|above|system)\b")


class QuestionError(ValueError):
    pass


def clean_question(question: str) -> str:
    q = " ".join((question or "").split())
    if not q:
        raise QuestionError("Question is empty")
    if len(q) > MAX_QUESTION_CHARS:
        raise QuestionError(f"Question is longer than {MAX_QUESTION_CHARS} characters")
    return q


def render_facts(context: Mapping[str, Any]) -> str:
    """Stable, compact rendering of the app's facts; capped in size."""
    lines = []
    for key in sorted(context):
        value = context[key]
        if isinstance(value, (dict, list)):
            value = json.dumps(value, separators=(", ", ": "), ensure_ascii=False)
        note = FIELD_NOTES.get(key)
        lines.append(f"- {key}: {value}" + (f" [{note}]" if note else ""))
    text = "\n".join(lines)
    if len(text) > MAX_CONTEXT_CHARS:
        raise QuestionError("Context is too large")
    return text


def user_prompt(question: str, context: Mapping[str, Any]) -> str:
    return f"FACTS:\n{render_facts(context)}\n\nQUESTION: {question}"


def looks_like_injection(question: str) -> bool:
    return bool(_INJECTION.search(question))


def tidy_answer(raw: str) -> str:
    text = raw.strip()
    text = re.sub(r"(?is)^(answer|assistant)\s*:\s*", "", text)
    return text[:2000].strip()


# --- layer 1: topic gate ---------------------------------------------------------------------

OFF_TOPIC_REPLY = "I can only help with questions about your taxes, income, expenses and mileage in iFlow Core."

_TOPIC = re.compile(
    r"(?i)\b(tax\w*|irs|deduct\w*|write[- ]?offs?|set[- ]?aside|save|saving|reserve|owe|owing|pay\w*|quarter\w*|"
    r"estimat\w*|income|profit|earn\w*|expense\w*|spend\w*|spent|mile\w*|mileage|refund|1099\w*|schedule\s*c|"
    r"self[- ]?employ\w*|se\b|rate|bracket|withh\w*|due|deadline|penalt\w*|qbi|agi|business|gig|fees?|"
    r"receipts?|money|dollars?|how\s+much|social\s+security|medicare|state|federal|filing|w-?2)\b")


def is_on_topic(question: str) -> bool:
    return bool(_TOPIC.search(question))


# --- layer 2: quick answers ---------------------------------------------------------------------

def _fmt_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{d:%B} {d.day}, {d.year}"


def _has(facts: Mapping[str, Any], *keys: str) -> bool:
    return all(k in facts and facts[k] not in (None, "") for k in keys)


_HYPOTHETICAL = re.compile(r"(?i)\b(what\s+if|what\s+happens|if\s+i|would\s+i|should\s+i\s+switch|instead|"
                           r"miss(ed)?|late|skip(ped)?|compare|better|worth)\b")
_DEDUCT = re.compile(r"(?i)\b((can|could|may)\s+i\s+(deduct|write[- ]?off|claim)|deductible|write[- ]?offs?)\b")
_VEHICLE = re.compile(r"(?i)\b(car|vehicle|gas|fuel|oil|tires?|repairs?|insurance|lease|maintenance)\b")


def deduction_answer(question: str, facts: Mapping[str, Any]) -> str:
    """General, accurate rules for "can I deduct X?" (no model involved)."""
    text = ("Costs you have for your gig work are generally deductible on Schedule C; only the business-use "
            "share counts, and purely personal costs don't. In iFlow Core, add it as an expense and set its "
            "business-use %. Meals are only 50% deductible. Keep the receipt.")
    if _VEHICLE.search(question) and "standard" in str(facts.get("vehicle_method", "standard")):
        text += (" You're using the standard mileage rate, which already covers gas, repairs, insurance and "
                 "depreciation, so don't also deduct those; parking and tolls for business trips still count.")
    return text


def quick_answer(question: str, facts: Mapping[str, Any]) -> str | None:
    """Template answers for the questions people ask most. None = not recognized
    (hypotheticals and comparisons always go to the model)."""
    q = question.lower()
    f = facts
    if _DEDUCT.search(q):
        return deduction_answer(q, f)
    if _HYPOTHETICAL.search(q):
        return None
    per_month = re.search(r"(each|per|a|every)\s+month|monthly", q)
    set_aside = re.search(r"set[- ]?aside|put\s+(away|aside)|reserve|save\s+(up\s+)?for|saving\s+for|save\s+each", q)

    if set_aside and per_month and _has(f, "set_aside_per_month_to_catch_up", "still_to_set_aside"):
        if f["still_to_set_aside"].startswith("0.00") or f["still_to_set_aside"].startswith("0 "):
            return f"You're covered: what you've set aside and paid ({f.get('covered_so_far', 'so far')}) already " \
                   f"meets your estimated tax of {f.get('total_estimated_tax', 'this year')}."
        return (f"Set aside about {f['set_aside_per_month_to_catch_up']} a month. That spreads the "
                f"{f['still_to_set_aside']} still needed over the {f.get('months_left_in_tax_year', 'remaining')} "
                f"month(s) left in the tax year.")
    if set_aside and _has(f, "still_to_set_aside", "total_estimated_tax"):
        covered = f.get("covered_so_far")
        return (f"You still need to set aside {f['still_to_set_aside']}. Your estimated tax for the year is "
                f"{f['total_estimated_tax']}" + (f" and you've covered {covered} so far." if covered else "."))
    if re.search(r"\bnext\s+(estimated\s+|quarterly\s+|tax\s+)?payment|when\b.*\b(due|pay)|deadline", q) \
            and _has(f, "next_estimated_payment"):
        nxt = f["next_estimated_payment"]
        return f"Your next estimated payment is due {_fmt_date(nxt['due'])}: about {nxt['amount']}."
    if re.search(r"self[- ]?employment|\bse\s+tax", q) and _has(f, "self_employment_tax", "net_profit"):
        half = f" Half of it ({f['se_tax_deductible_half']}) is deducted from your income." \
            if _has(f, "se_tax_deductible_half") else ""
        return (f"Self-employment tax is Social Security (12.4%) plus Medicare (2.9%), 15.3% in total, charged on "
                f"92.35% of your net profit. Your net profit is {f['net_profit']}, so your self-employment tax is "
                f"{f['self_employment_tax']}. As a gig worker you pay both the employee and employer halves, which "
                f"is why it feels high.{half}")
    if re.search(r"effective", q) and _has(f, "effective_rate", "total_estimated_tax"):
        return (f"Your effective rate is {f['effective_rate']}: total estimated tax of {f['total_estimated_tax']} "
                f"divided by your income.")
    if re.search(r"marginal|bracket", q) and _has(f, "federal_marginal_rate"):
        return f"Your federal marginal rate (the bracket your next dollar falls in) is {f['federal_marginal_rate']}."
    if re.search(r"how\s+much\b.*\b(tax|owe)|total\s+tax|what\s+do\s+i\s+owe", q) and _has(f, "total_estimated_tax"):
        parts = [f"self-employment tax {f['self_employment_tax']}" if _has(f, "self_employment_tax") else "",
                 f"federal income tax {f['federal_income_tax']}" if _has(f, "federal_income_tax") else "",
                 f"state income tax {f['state_income_tax']}" if _has(f, "state_income_tax") else ""]
        detail = ", ".join(p for p in parts if p)
        return f"Your estimated tax for {f.get('tax_year', 'this year')} is {f['total_estimated_tax']}" + \
            (f" ({detail})." if detail else ".")
    return None


# --- layer 3: grounding check ------------------------------------------------------------------

# Fixed, widely published rates the model may mention without them being in FACTS.
KNOWN_CONSTANTS = frozenset({"15.3", "12.4", "2.9", "0.9", "92.35", "50", "20", "100"})
_NUMBER = re.compile(r"(?<![\w.])\$?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)%?")


def _norm(num: str) -> str | None:
    try:
        return str(Decimal(num.replace(",", "")).normalize())
    except InvalidOperation:
        return None


def _numbers(text: str) -> set[str]:
    return {n for n in (_norm(m) for m in _NUMBER.findall(text)) if n is not None}


def ungrounded_numbers(answer: str, facts: Mapping[str, Any]) -> list[str]:
    """Numbers in the answer that don't appear in the facts. Single digits
    (counts like "4 months") and fixed rates are allowed."""
    rendered = json.dumps(facts, ensure_ascii=False)
    allowed = _numbers(rendered) | {_norm(c) for c in KNOWN_CONSTANTS}
    for iso in re.findall(r"\d{4}-\d{2}-\d{2}", rendered):          # dates may be written out in words
        y, m, d = iso.split("-")
        allowed |= {_norm(y), _norm(m), _norm(d)}
    return sorted(n for n in _numbers(answer) if n not in allowed and not (n.isdigit() and int(n) < 10))


def fallback_answer(facts: Mapping[str, Any]) -> str:
    parts = []
    if _has(facts, "total_estimated_tax"):
        parts.append(f"estimated tax for the year: {facts['total_estimated_tax']}")
    if _has(facts, "still_to_set_aside"):
        parts.append(f"still to set aside: {facts['still_to_set_aside']}")
    if _has(facts, "next_estimated_payment"):
        nxt = facts["next_estimated_payment"]
        parts.append(f"next payment: {nxt['amount']} due {_fmt_date(nxt['due'])}")
    summary = "; ".join(parts)
    return ("I couldn't answer that reliably from your numbers, so I won't guess. "
            + (f"Here's what your figures show: {summary}. " if summary else "")
            + "For this question, a tax professional is the safest source.")
