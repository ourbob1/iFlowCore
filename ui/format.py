"""
Display formatting. Inputs are always integers in storage units (minor
units, meters, basis points); floats never enter. These are the only places
where numbers become strings for the user.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from core.config import UnitSystem, minor_units
from core.financial.money import format_minor

CURRENCY_SYMBOLS = {
    "USD": "$", "CAD": "CA$", "AUD": "A$", "NZD": "NZ$", "MXN": "MX$", "EUR": "€", "GBP": "£",
    "JPY": "¥", "CNY": "CN¥", "INR": "₹", "KRW": "₩", "BRL": "R$", "CHF": "CHF ", "ZAR": "R",
}
MINUS = "−"


def symbol(currency: str) -> str:
    return CURRENCY_SYMBOLS.get(currency.upper(), currency.upper() + " ")


def money(amount_minor: int, currency: str, *, cents: bool = True) -> str:
    """12345 USD → "$123.45"; -500 → "−$5.00"; cents=False → "$123"."""
    sign = MINUS if amount_minor < 0 else ""
    value = abs(amount_minor)
    if not cents and minor_units(currency) > 0:
        scale = 10 ** minor_units(currency)
        whole = (value + scale // 2) // scale
        return f"{sign}{symbol(currency)}{whole:,}"
    return f"{sign}{symbol(currency)}{format_minor(value, currency, thousands=True)}"


def money_compact(amount_minor: int, currency: str) -> str:
    """Axis labels: $950, $12.5k, $1.2M (half-up, trailing .0 dropped)."""
    sign = MINUS if amount_minor < 0 else ""
    major = Decimal(abs(amount_minor)).scaleb(-minor_units(currency))
    for threshold, suffix in ((Decimal(1_000_000), "M"), (Decimal(1_000), "k")):
        if major >= threshold:
            scaled = (major / threshold).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            text = f"{scaled:f}".rstrip("0").rstrip(".")
            return f"{sign}{symbol(currency)}{text}{suffix}"
    return f"{sign}{symbol(currency)}{major.quantize(Decimal(1), rounding=ROUND_HALF_UP):f}"


def percent_bp(bp: int, *, decimals: int = 1) -> str:
    """2250 → "22.5%"."""
    value = (Decimal(bp) / 100).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    text = f"{value:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text.replace('-', MINUS)}%"


def distance(meters: int, units: UnitSystem, *, decimals: int = 1) -> str:
    value = (Decimal(meters) / Decimal(str(units.meters_per_distance_unit))).quantize(
        Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    return f"{value:,f} {units.distance_unit}"


def relative_minutes(minutes: int) -> str:
    if minutes <= 0:
        return "now"
    if minutes < 60:
        return f"in {minutes} min"
    hours, mins = divmod(minutes, 60)
    return f"in {hours} h" if mins == 0 else f"in {hours} h {mins} min"


def days_until(target: date, today: date) -> str:
    delta = (target - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return f"in {delta} days" if delta > 0 else f"{-delta} days ago"


def local_time(dt: datetime, tz: str) -> str:
    from core.timeutil import zone

    local = dt.astimezone(zone(tz))
    hour12 = local.hour % 12 or 12
    # Built by hand: "%-d"/"%-I" are not supported by strftime on Windows.
    return f"{local:%a %b} {local.day}, {hour12}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"


def duration(seconds: int) -> str:
    td = timedelta(seconds=max(0, seconds))
    hours, rem = divmod(int(td.total_seconds()), 3600)
    return f"{hours} h {rem // 60} min" if hours else f"{rem // 60} min"


def parse_distance(raw: str, units: UnitSystem) -> int | None:
    """User-typed distance ("12,345.6") in display units → integer meters (None if blank)."""
    from decimal import InvalidOperation

    cleaned = (raw or "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError("Enter a number") from None
    if not value.is_finite():
        raise ValueError("Enter a number")
    if value < 0:
        raise ValueError("Distance can't be negative")
    meters = value * Decimal(str(units.meters_per_distance_unit))
    return int(meters.quantize(Decimal(1), rounding=ROUND_HALF_UP))
