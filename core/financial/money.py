"""
Integer money primitives.

Every amount in iFlow Core is an `int` of minor units (cents, pence, yen, fils)
paired with an ISO 4217 code. Floats never touch money. When a calculation
needs fractions (a tax rate, a business-use percentage, an FX rate), it goes
through `Decimal` and is rounded back to an int exactly once, here, with an
explicit rounding mode.

Rounding policy
---------------
* `ROUND_HALF_UP` is the default. It matches how the IRS and most tax
  authorities round, and what users expect on a statement.
* Parsing is **strict** by default: "12.345" USD raises instead of silently
  rounding. Importers that receive over-precise data (some platforms export
  fractions of a cent) opt in with `rounding=...`.
* Splitting an amount (`allocate`) never creates or loses a cent — the
  remainder is distributed with the largest-remainder method.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable, Sequence

from core.config import minor_units, normalize_currency

__all__ = [
    "ROUND_HALF_UP",
    "ROUND_HALF_EVEN",
    "MoneyError",
    "parse_amount",
    "to_decimal",
    "from_decimal",
    "apply_bp",
    "multiply",
    "allocate",
    "format_minor",
    "BP_SCALE",
    "sum_minor",
    "convert",
]

BP_SCALE = 10_000  # basis points in 100%


class MoneyError(ValueError):
    """An amount could not be parsed or represented exactly."""


# Symbols stripped during parsing. Currency *codes* are also stripped.
_SYMBOLS = "$€£¥₹₩₦₱₪฿₫₺₴₽¢"
_CODE_RE = re.compile(r"\b[A-Z]{3}\b")


def _quantum(exponent: int) -> Decimal:
    return Decimal(1).scaleb(-exponent)  # 2 -> Decimal('0.01')


def to_decimal(amount_minor: int, currency: str) -> Decimal:
    """Minor units → Decimal major units (1234, 'USD') → Decimal('12.34')."""
    _require_int(amount_minor)
    return Decimal(amount_minor).scaleb(-minor_units(currency))


def from_decimal(value: Decimal, currency: str, *, rounding: str | None = ROUND_HALF_UP) -> int:
    """Decimal major units → int minor units.

    `rounding=None` means strict: raise if `value` has more precision than
    the currency allows.
    """
    exponent = minor_units(currency)
    scaled = value.scaleb(exponent)
    if rounding is None:
        if scaled != scaled.to_integral_value():
            raise MoneyError(f"{value} has more than {exponent} decimal places for {currency}")
        return int(scaled)
    return int(scaled.quantize(Decimal(1), rounding=rounding))


def parse_amount(
    text: str | int | Decimal,
    currency: str,
    *,
    decimal_separator: str = ".",
    rounding: str | None = None,
) -> int:
    """Parse a human/exported amount into int minor units.

    Handles: currency symbols and codes, thousands separators, leading/trailing
    minus, parentheses negatives "(12.50)", trailing "CR"/"DR", unicode minus,
    and European "1.234,56" when `decimal_separator=","`.

        parse_amount("$1,234.56", "USD")          -> 123456
        parse_amount("(12.50)", "USD")            -> -1250
        parse_amount("1.234,56 €", "EUR", decimal_separator=",") -> 123456
        parse_amount("¥1,200", "JPY")             -> 1200
    """
    currency = normalize_currency(currency)
    if isinstance(text, bool):
        raise MoneyError("Boolean is not an amount")
    if isinstance(text, int):
        return from_decimal(Decimal(text), currency, rounding=rounding)
    if isinstance(text, Decimal):
        return from_decimal(text, currency, rounding=rounding)
    if not isinstance(text, str):
        raise MoneyError(f"Unsupported amount type {type(text).__name__}")
    if decimal_separator not in (".", ","):
        raise ValueError("decimal_separator must be '.' or ','")

    raw = text
    s = text.strip().replace("−", "-").replace(" ", " ")
    if not s:
        raise MoneyError("Empty amount")

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    upper = s.upper()
    if upper.endswith("CR"):
        s = s[:-2].strip()
    elif upper.endswith("DR"):
        negative, s = True, s[:-2].strip()

    s = _CODE_RE.sub("", s)
    s = "".join(ch for ch in s if ch not in _SYMBOLS).strip()

    # Sign may sit before or after the symbol/number: "-$5", "$-5", "5-".
    if s.startswith("-"):
        negative, s = not negative, s[1:].strip()
    elif s.endswith("-"):
        negative, s = not negative, s[:-1].strip()
    elif s.startswith("+"):
        s = s[1:].strip()

    thousands = "," if decimal_separator == "." else "."
    s = s.replace(" ", "").replace("'", "").replace(thousands, "")
    if decimal_separator == ",":
        s = s.replace(",", ".")

    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", s):
        raise MoneyError(f"Cannot parse amount {raw!r}")
    try:
        value = Decimal(s)
    except InvalidOperation as exc:  # pragma: no cover - regex guards this
        raise MoneyError(f"Cannot parse amount {raw!r}") from exc

    minor = from_decimal(value, currency, rounding=rounding)
    return -minor if negative else minor


def apply_bp(amount_minor: int, bp: int, *, rounding: str = ROUND_HALF_UP) -> int:
    """amount × (bp / 10000), rounded to a whole minor unit.

        apply_bp(10000, 1530)  -> 1530    ($100 × 15.30%)
        apply_bp(333, 5000)    -> 167     (half-up)
    """
    _require_int(amount_minor)
    _require_int(bp)
    value = Decimal(amount_minor) * Decimal(bp) / Decimal(BP_SCALE)
    return int(value.quantize(Decimal(1), rounding=rounding))


def multiply(amount_minor: int, factor: Decimal | int | str, *, rounding: str = ROUND_HALF_UP) -> int:
    """amount × arbitrary Decimal factor (FX rates, mileage rates)."""
    _require_int(amount_minor)
    if isinstance(factor, float):
        raise MoneyError("Pass factors as Decimal or str, never float")
    value = Decimal(amount_minor) * Decimal(factor)
    return int(value.quantize(Decimal(1), rounding=rounding))


def allocate(amount_minor: int, weights: Sequence[int]) -> list[int]:
    """Split an amount by integer weights without losing a minor unit.

        allocate(100, [1, 1, 1]) -> [34, 33, 33]
        allocate(-100, [1, 1, 1]) -> [-34, -33, -33]
    """
    _require_int(amount_minor)
    if not weights or any(w < 0 for w in weights) or sum(weights) == 0:
        raise MoneyError("weights must be non-negative with a positive total")
    sign = -1 if amount_minor < 0 else 1
    total = abs(amount_minor)
    weight_sum = sum(weights)
    shares = [total * w // weight_sum for w in weights]
    remainders = [(total * w % weight_sum, -i) for i, w in enumerate(weights)]
    for _, neg_index in sorted(remainders, reverse=True)[: total - sum(shares)]:
        shares[-neg_index] += 1
    return [sign * s for s in shares]


def convert(
    amount_minor: int,
    from_currency: str,
    to_currency: str,
    rate: Decimal | str | int,
    *,
    rounding: str = ROUND_HALF_UP,
) -> int:
    """Convert minor units between currencies.

    `rate` = units of `to_currency` per ONE unit of `from_currency` (major
    units, the way FX rates are quoted). Minor-unit exponents are handled:

        convert(1000, "JPY", "USD", "0.0067")  -> 670   (¥1,000 → $6.70)
        convert(1000, "USD", "JPY", "149.5")   -> 1495  ($10.00 → ¥1,495)
    """
    _require_int(amount_minor)
    if isinstance(rate, float):
        raise MoneyError("Pass FX rates as Decimal or str, never float")
    rate = Decimal(rate)
    if rate <= 0:
        raise MoneyError("FX rate must be positive")
    if normalize_currency(from_currency) == normalize_currency(to_currency):
        return amount_minor
    shift = minor_units(to_currency) - minor_units(from_currency)
    value = Decimal(amount_minor) * rate * Decimal(1).scaleb(shift)
    return int(value.quantize(Decimal(1), rounding=rounding))


def format_minor(amount_minor: int, currency: str, *, thousands: bool = False) -> str:
    """Plain decimal string for files: (123456, 'USD') -> '1234.56', JPY -> '1234'."""
    exponent = minor_units(currency)
    value = to_decimal(amount_minor, currency).quantize(_quantum(exponent))
    return f"{value:,}" if thousands else f"{value}"


def sum_minor(values: Iterable[int]) -> int:
    total = 0
    for v in values:
        _require_int(v)
        total += v
    return total


def _require_int(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoneyError(f"Money must be int minor units, got {type(value).__name__}")
