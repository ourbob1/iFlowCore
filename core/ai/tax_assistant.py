"""
Tax questions answered by the self-hosted model, grounded in the app's own
numbers.

The deterministic tax engine (core.financial.tax) stays the source of truth.
The model only *explains*: we send it the year's computed figures, never raw
transactions, vendors, trips or locations, and the server's prompt tells it to
answer from those figures alone and to say when it can't. Every answer comes
back with the "planning estimate, not tax advice" notice.

Small models are unreliable at arithmetic, so every figure a user is likely
to ask about (what's still to set aside, per month, the next payment) is
computed here in integer minor units and sent ready to quote.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from core.ai.client import AIServiceClient
from core.financial.money import format_minor
from core.financial.tax import TaxEstimate, YearSummary

DISCLAIMER = "Planning estimate from your iFlow Core data, not tax advice. Check with a tax professional."
BP_PERCENT = 100          # 2200 bp → "22.00%"

STATUS_TEXT = {
    "single": "single", "married_joint": "married filing jointly", "married_separate": "married filing separately",
    "head_of_household": "head of household", "qualifying_surviving_spouse": "qualifying surviving spouse",
}


def _pct(bp: int) -> str:
    return f"{bp / BP_PERCENT:.2f}%"


def build_context(estimate: TaxEstimate, summary: YearSummary, *, today: date, currency: str,
                  reserved_minor: int | None = None, paid_minor: int | None = None) -> dict[str, Any]:
    """Aggregate figures only; amounts as plain decimal strings with the ISO code."""

    def m(minor: int) -> str:
        return f"{format_minor(minor, currency, thousands=True)} {currency}"

    facts: dict[str, Any] = {
        "as_of": today.isoformat(),
        "tax_year": estimate.tax_year,
        "country": "US",
        "state": (estimate.region or "not set").removeprefix("US-"),
        "filing_status": STATUS_TEXT.get(estimate.filing_status, estimate.filing_status),
        "gross_receipts": m(summary.gross_receipts),
        "platform_fees": m(summary.platform_fees),
        "deductible_expenses": m(summary.deductible_expenses),
        "vehicle_method": summary.vehicle_method.replace("_", " "),
        "business_miles": f"{summary.business_meters / 1609.344:,.0f}",
        "mileage_deduction": m(summary.mileage_deduction),
        "net_profit": m(estimate.net_profit),
        "self_employment_tax": m(estimate.se.total),
        "se_tax_deductible_half": m(estimate.se.deductible_half),
        "adjusted_gross_income": m(estimate.adjusted_gross_income),
        "standard_deduction": m(estimate.standard_deduction),
        "qbi_deduction": m(estimate.qbi_deduction),
        "federal_taxable_income": m(estimate.federal_taxable_income),
        "federal_income_tax": m(estimate.federal_income_tax),
        "federal_marginal_rate": _pct(estimate.federal_marginal_rate_bp),
        "state_income_tax": m(estimate.state_income_tax),
        "total_estimated_tax": m(estimate.total_tax),
        "effective_rate": _pct(estimate.effective_rate_bp),
        "balance_due": m(estimate.balance_due),
        "quarterly_payments": [
            {"due": d.isoformat(), "amount": m(a), "status": "past" if d < today else "upcoming"}
            for d, a in zip(estimate.quarterly_due_dates, estimate.quarterly_payments)
        ],
        "notes": list(estimate.notes) + list(estimate.provisional) + list(summary.warnings),
    }
    upcoming = [(d, a) for d, a in zip(estimate.quarterly_due_dates, estimate.quarterly_payments) if d >= today]
    if upcoming:
        facts["next_estimated_payment"] = {"due": upcoming[0][0].isoformat(), "amount": m(upcoming[0][1])}
    if reserved_minor is not None:
        facts["tax_reserve_set_aside"] = m(reserved_minor)
    if paid_minor is not None:
        facts["estimated_payments_made"] = m(paid_minor)
    if reserved_minor is not None or paid_minor is not None:
        covered = (reserved_minor or 0) + (paid_minor or 0)
        still = max(0, estimate.total_tax - covered)
        months_left = 12 - today.month + 1 if today.year == estimate.tax_year else 1
        facts["covered_so_far"] = m(covered)
        facts["still_to_set_aside"] = m(still)
        facts["months_left_in_tax_year"] = months_left
        facts["set_aside_per_month_to_catch_up"] = m(-(-still // months_left))     # round up to the minor unit
    return facts


SOURCE_LABEL = {
    "rules": "Calculated from your numbers",
    "ai": "AI-generated from your numbers; double-check before acting on it",
    "fallback": "The AI answer didn't match your numbers, so it was withheld",
    "guard": "",
}


def ask(client: AIServiceClient, question: str, context: dict[str, Any]) -> dict[str, Any]:
    """Returns {"answer", "disclaimer", "model", "grounded", "source", "source_label"}.
    The local disclaimer always wins over whatever the server sent."""
    data = client.ask_tax(question, context)
    model = str(data.get("model") or "")
    source = "fallback" if data.get("replaced") else model if model in ("rules", "guard") else "ai"
    return {
        "answer": data["answer"].strip(),
        "disclaimer": DISCLAIMER,
        "model": model,
        "grounded": bool(data.get("grounded", True)),
        "source": source,
        "source_label": SOURCE_LABEL[source],
    }
