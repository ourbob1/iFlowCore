"""Timestamp helpers shared across modules.

Storage convention (schema.sql): ISO-8601 UTC with millisecond precision and
a 'Z' suffix. Business rules that depend on the *calendar date* — tax years,
the July 1 mileage-rate change — must use the user's local date, not the UTC
date: a trip at 9 pm on June 30 in New York is 01:00 UTC on July 1.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone '{name}'") from exc


def to_iso_utc(dt: datetime) -> str:
    """Aware datetime → storage string. Naive datetimes are rejected."""
    if dt.tzinfo is None:
        raise ValueError("Naive datetime; attach a timezone before storing")
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def parse_iso(value: str) -> datetime:
    """Storage string (or any ISO-8601) → aware UTC datetime.

    Date-only values ("2026-09-01") are treated as midnight UTC, matching how
    SQLite's datetime() reads them.
    """
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def local_date(value: str | datetime, tz_name: str = "UTC") -> date:
    """Calendar date of a stored timestamp in the given timezone."""
    dt = parse_iso(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        raise ValueError("Naive datetime")
    return dt.astimezone(zone(tz_name)).date()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
