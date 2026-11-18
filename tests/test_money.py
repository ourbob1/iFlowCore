"""Integer money handling: parsing, rounding, allocation, conversion, formatting."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.financial.money import (
    ROUND_HALF_EVEN,
    MoneyError,
    allocate,
    apply_bp,
    convert,
    format_minor,
    from_decimal,
    multiply,
    parse_amount,
    to_decimal,
)


@pytest.mark.parametrize("text,currency,expected", [
    ("12.34", "USD", 1234),
    ("$1,234.56", "USD", 123456),
    ("  $0.05 ", "USD", 5),
    ("-$5.00", "USD", -500),
    ("$-5.00", "USD", -500),
    ("5.00-", "USD", -500),
    ("(12.50)", "USD", -1250),
    ("($12.50)", "USD", -1250),
    ("−12.50", "USD", -1250),          # unicode minus
    ("12.50 DR", "USD", -1250),
    ("12.50 CR", "USD", 1250),
    ("USD 99", "USD", 9900),
    ("+7", "USD", 700),
    (".5", "USD", 50),
    ("¥1,200", "JPY", 1200),
    ("1.234", "KWD", 1234),                 # 3-decimal currency
    ("0", "USD", 0),
])
def test_parse_amount(text, currency, expected):
    assert parse_amount(text, currency) == expected


def test_parse_european_format():
    assert parse_amount("1.234,56 €", "EUR", decimal_separator=",") == 123456
    assert parse_amount("-0,99", "EUR", decimal_separator=",") == -99


@pytest.mark.parametrize("bad", ["", "abc", "1.2.3", "$", "12,34.5x", "--5"])
def test_parse_rejects_garbage(bad):
    with pytest.raises(MoneyError):
        parse_amount(bad, "USD")


def test_strict_parsing_rejects_sub_minor_precision():
    with pytest.raises(MoneyError, match="decimal places"):
        parse_amount("12.345", "USD")
    with pytest.raises(MoneyError):
        parse_amount("1200.5", "JPY")


def test_opt_in_rounding_is_half_up():
    assert parse_amount("12.345", "USD", rounding="ROUND_HALF_UP") == 1235
    assert parse_amount("12.344", "USD", rounding="ROUND_HALF_UP") == 1234
    assert parse_amount("-12.345", "USD", rounding="ROUND_HALF_UP") == -1235


def test_parse_rejects_bool_and_float():
    with pytest.raises(MoneyError):
        parse_amount(True, "USD")
    with pytest.raises(MoneyError):
        parse_amount(1.5, "USD")  # type: ignore[arg-type]


def test_classic_float_trap_is_exact():
    # 0.1 + 0.2 != 0.3 in floats; integer minor units never drift.
    assert parse_amount("0.10", "USD") + parse_amount("0.20", "USD") == parse_amount("0.30", "USD")
    assert sum(parse_amount("0.01", "USD") for _ in range(100_000)) == 100_000


@pytest.mark.parametrize("amount,bp,expected", [
    (10000, 1530, 1530),     # $100 × 15.30%
    (333, 5000, 167),        # 166.5 → 167 half-up
    (-333, 5000, -167),      # symmetric for negatives
    (1, 4999, 0),
    (1, 5000, 1),
    (123456789, 2200, 27160494),
])
def test_apply_bp(amount, bp, expected):
    assert apply_bp(amount, bp) == expected


def test_apply_bp_half_even_option():
    assert apply_bp(250, 5000, rounding=ROUND_HALF_EVEN) == 125     # 125.0 exact
    assert apply_bp(5, 5000, rounding=ROUND_HALF_EVEN) == 2         # 2.5 → 2


def test_apply_bp_requires_int():
    with pytest.raises(MoneyError):
        apply_bp(12.5, 1000)  # type: ignore[arg-type]


@pytest.mark.parametrize("amount,weights,expected", [
    (100, [1, 1, 1], [34, 33, 33]),
    (-100, [1, 1, 1], [-34, -33, -33]),
    (5, [1, 1, 1, 1], [2, 1, 1, 1]),
    (1000, [7000, 3000], [700, 300]),
    (0, [1, 2], [0, 0]),
    (101, [1, 0, 1], [51, 0, 50]),
])
def test_allocate_never_loses_a_cent(amount, weights, expected):
    shares = allocate(amount, weights)
    assert shares == expected
    assert sum(shares) == amount


def test_allocate_rejects_bad_weights():
    with pytest.raises(MoneyError):
        allocate(100, [])
    with pytest.raises(MoneyError):
        allocate(100, [0, 0])
    with pytest.raises(MoneyError):
        allocate(100, [1, -1])


def test_convert_handles_exponents():
    assert convert(1000, "JPY", "USD", "0.0067") == 670
    assert convert(1000, "USD", "JPY", "149.5") == 1495
    assert convert(1000, "USD", "KWD", "0.3071") == 3071
    assert convert(12345, "EUR", "EUR", "1.5") == 12345
    with pytest.raises(MoneyError):
        convert(100, "USD", "EUR", 0.9)  # type: ignore[arg-type]
    with pytest.raises(MoneyError):
        convert(100, "USD", "EUR", "0")


def test_multiply_rejects_float():
    assert multiply(1000, "0.725") == 725
    with pytest.raises(MoneyError):
        multiply(1000, 0.725)  # type: ignore[arg-type]


def test_decimal_roundtrip_and_format():
    assert to_decimal(123456, "USD") == Decimal("1234.56")
    assert from_decimal(Decimal("1234.56"), "USD", rounding=None) == 123456
    assert format_minor(123456, "USD") == "1234.56"
    assert format_minor(-5, "USD") == "-0.05"
    assert format_minor(1200, "JPY") == "1200"
    assert format_minor(1234, "KWD") == "1.234"
    assert format_minor(123456789, "USD", thousands=True) == "1,234,567.89"
