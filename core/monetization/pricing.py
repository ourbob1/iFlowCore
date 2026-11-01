"""
Dynamic CPC Sliding-Scale Calculator.

Calculates tailored Cost-Per-Click (CPC) rates in integer cents based on
advertiser firmographic attributes (industry, company size, and revenue tier).
All math operates in integer cents (or basis points) to prevent floating-point inaccuracies.
"""

from __future__ import annotations

from typing import Final

# Baseline CPC in integer cents (e.g., $0.50)
BASE_CPC_CENTS: Final[int] = 50

# Minimum and maximum allowable CPC bounds in integer cents
MIN_CPC_CENTS: Final[int] = 10
MAX_CPC_CENTS: Final[int] = 500

# Industry weights in basis points (10,000 bp = 1.0x multiplier)
INDUSTRY_MULTIPLIERS_BP: Final[dict[str, int]] = {
    "insurance": 12_000,       # 1.2x (high-intent financial)
    "tax services": 12_000,    # 1.2x
    "fleet leasing": 11_500,   # 1.15x
    "banking": 12_500,         # 1.25x
    "legal": 12_000,           # 1.2x
    "fuel & energy": 10_500,   # 1.05x
    "maintenance": 10_000,     # 1.0x baseline
    "telecom": 10_000,         # 1.0x
    "education": 9_500,        # 0.95x
    "general": 10_000,         # 1.0x
}

# Company size volume discount/adjustment in basis points (10,000 bp = 1.0x multiplier)
COMPANY_SIZE_MULTIPLIERS_BP: Final[dict[str, int]] = {
    "1-10": 10_500,    # 1.05x (small business, lower volume commitment)
    "11-50": 10_000,   # 1.0x (standard baseline)
    "51-250": 9_000,   # 0.90x (10% volume incentive)
    "250+": 8_000,     # 0.80x (20% enterprise volume incentive)
}

# Revenue tier scaling multipliers in basis points (10,000 bp = 1.0x multiplier)
REVENUE_TIER_MULTIPLIERS_BP: Final[dict[str, int]] = {
    "<1m": 10_500,     # 1.05x
    "1m-10m": 10_000,  # 1.0x
    "10m-50m": 9_200,  # 0.92x (8% enterprise scale incentive)
    "50m+": 8_500,     # 0.85x (15% enterprise scale incentive)
}


def _normalize_key(val: str) -> str:
    """Normalize input strings for key lookup."""
    return (val or "").strip().lower()


def calculate_dynamic_cpc(industry: str, company_size: str, revenue_tier: str) -> int:
    """
    Returns calculated CPC in integer cents based on a dynamic sliding scale.

    Multipliers are represented in basis points (1 bp = 0.01%, 10,000 bp = 1.0x)
    to perform pure integer arithmetic.

    Final CPC = Base CPC * (industry_bp / 10,000) * (size_bp / 10,000) * (revenue_bp / 10,000)
    """
    ind_key = _normalize_key(industry)
    size_key = _normalize_key(company_size)
    rev_key = _normalize_key(revenue_tier)

    # Resolve multipliers with defaults
    ind_bp = INDUSTRY_MULTIPLIERS_BP.get(ind_key, 10_000)
    size_bp = COMPANY_SIZE_MULTIPLIERS_BP.get(size_key, 10_000)
    rev_bp = REVENUE_TIER_MULTIPLIERS_BP.get(rev_key, 10_000)

    # Compute numerator: base_cents * ind_bp * size_bp * rev_bp
    # Divisor is (10_000 ** 3) = 1_000_000_000_000
    numerator = BASE_CPC_CENTS * ind_bp * size_bp * rev_bp
    divisor = 10_000 * 10_000 * 10_000

    # Half-up integer rounding
    calculated_cents = (numerator + (divisor // 2)) // divisor

    # Enforce lower and upper bounds
    return max(MIN_CPC_CENTS, min(calculated_cents, MAX_CPC_CENTS))
