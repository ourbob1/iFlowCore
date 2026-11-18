"""
Offline VIN validation (ISO 3779 / 49 CFR 565).

Runs before any network call so typos never cost a paid DataOne lookup.
"""

from __future__ import annotations

import re

_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")   # I, O, Q are never used

_TRANSLITERATION = {
    **{str(d): d for d in range(10)},
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}
_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)

# Position-10 model-year codes; the cycle repeats every 30 years.
_YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"


class VinError(ValueError):
    pass


def normalize_vin(vin: str) -> str:
    """Upper-case, strip spaces/dashes, and validate characters and length."""
    if not isinstance(vin, str):
        raise VinError("VIN must be a string")
    cleaned = re.sub(r"[\s-]", "", vin).upper()
    if len(cleaned) != 17:
        raise VinError(f"VIN must be 17 characters (got {len(cleaned)})")
    if not _VIN_RE.match(cleaned):
        bad = sorted({c for c in cleaned if c in "IOQ" or not c.isalnum()})
        raise VinError(f"VIN contains invalid characters {bad}")
    return cleaned


def check_digit(vin: str) -> str:
    """Compute the North American check digit (position 9) for a 17-char VIN."""
    total = sum(_TRANSLITERATION[c] * w for c, w in zip(vin, _WEIGHTS))
    remainder = total % 11
    return "X" if remainder == 10 else str(remainder)


def validate_vin(vin: str, *, require_check_digit: bool = True) -> str:
    """Normalize and validate. The check digit is mandatory for vehicles sold in
    North America; pass `require_check_digit=False` for other markets."""
    cleaned = normalize_vin(vin)
    if require_check_digit and cleaned[8] != check_digit(cleaned):
        raise VinError(f"VIN check digit mismatch (position 9 is {cleaned[8]}, expected {check_digit(cleaned)})")
    return cleaned


def model_year_candidates(vin: str, *, max_year: int | None = None) -> tuple[int, ...]:
    """Possible model years from position 10 (newest first).

    For light vehicles, a letter in position 7 indicates 2010+ and a digit
    indicates 1980–2009; we use that to pick the right 30-year cycle.
    Years after `max_year` (default: next calendar year) are excluded.
    """
    from datetime import date

    cleaned = normalize_vin(vin)
    code = cleaned[9]
    if code not in _YEAR_CODES:
        return ()
    limit = max_year if max_year is not None else date.today().year + 1
    base = 1980 + _YEAR_CODES.index(code)
    years = tuple(y for y in (base + 60, base + 30, base) if y <= limit)
    if cleaned[6].isalpha():
        return tuple(y for y in years if y >= 2010)
    return tuple(y for y in years if y < 2010)
