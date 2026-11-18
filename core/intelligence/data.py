"""
Read one period of activity from the local database.

Everything happens on the device: rows are read from the (encrypted)
database, converted to the base currency with the same rules as the tax
summary, and handed to the pure analysis modules. Nothing here performs I/O
beyond the database.

Inclusion rules
---------------
Earnings: every live `transactions` row except reimbursements (pass-through
money, not pay for work). Foreign-currency rows without a pinned base amount
are skipped and counted, like the tax engine does.

Trips: completed business trips only. Auto-detected trips still waiting for
review are left out (they might be personal) and counted.

Time of day: statement imports that only carry a date are stored as local
(or UTC) midnight. Treating those as midnight earnings would invent a
"midnight peak", so they stay in daily/weekly/monthly totals but are marked
`time_known=False` and kept out of hour-of-day analysis.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from core.intelligence.models import Activity, EarningEvent, TripRecord
from core.timeutil import parse_iso, to_iso_utc, zone

EXCLUDED_KINDS = ("reimbursement",)


def local_bounds_utc(start: date, end: date, tz: str) -> tuple[str, str]:
    """UTC ISO strings for [start 00:00 local, end+1 00:00 local)."""
    z = zone(tz)
    lo = datetime.combine(start, time(0), tzinfo=z)
    hi = datetime.combine(end + timedelta(days=1), time(0), tzinfo=z)
    return to_iso_utc(lo), to_iso_utc(hi)


def _ratio(value: int, num: int, den: int) -> int:
    return int((Decimal(value) * num / den).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_base(gross: int, fee: int, row_currency: str, base_amount: int | None, currency: str) -> tuple[int, int] | None:
    """(gross, fee) in the base currency, or None when it can't be converted.
    `base_amount_minor` pins the converted *gross*; the fee is scaled by the same rate."""
    if row_currency == currency:
        return gross, fee
    if base_amount is None or gross == 0:
        return None
    return base_amount, _ratio(fee, base_amount, gross)


def _midnight(d: datetime) -> bool:
    return d.hour == 0 and d.minute == 0 and d.second == 0 and d.microsecond == 0


def classify_time(raw: str, at: datetime, tz: str) -> tuple[date, bool]:
    """(calendar day, time_known). Date-only rows (no time part, or exactly
    midnight local or UTC to the millisecond) have no usable time of day; their
    day is the date that was written, whichever midnight it was stored at."""
    local = at.astimezone(zone(tz))
    utc = at.astimezone(timezone.utc)
    if len(raw.strip()) <= 10 or (_midnight(utc) and not _midnight(local)):
        return utc.date(), False
    if _midnight(local):
        return local.date(), False
    return local.date(), True


def load_activity(db: Any, *, start: date, end: date, tz: str, currency: str) -> Activity:
    if end < start:
        raise ValueError("end must not be before start")
    lo, hi = local_bounds_utc(start, end, tz)
    # Earnings: query one extra day each side, then keep rows by calendar day, so a
    # date-only row stored at UTC midnight lands on the date that was written.
    wide_lo, wide_hi = local_bounds_utc(start - timedelta(days=1), end + timedelta(days=1), tz)
    act = Activity(start=start, end=end, tz=tz, currency=currency.upper())
    placeholders = ",".join("?" for _ in EXCLUDED_KINDS)
    with db.connection() as conn:
        rows = conn.execute(
            f"""SELECT occurred_at, gross_amount_minor, platform_fee_minor, currency, base_amount_minor, platform_id,
                       kind
                  FROM transactions
                 WHERE deleted_at IS NULL AND kind NOT IN ({placeholders})
                   AND julianday(occurred_at) >= julianday(?) AND julianday(occurred_at) < julianday(?)
                 ORDER BY julianday(occurred_at)""",
            (*EXCLUDED_KINDS, wide_lo, wide_hi),
        ).fetchall()
        for r in rows:
            converted = to_base(r["gross_amount_minor"], r["platform_fee_minor"], r["currency"],
                                r["base_amount_minor"], act.currency)
            if converted is None:
                act.excluded_unconverted += 1
                continue
            at = parse_iso(r["occurred_at"])
            day, timed = classify_time(r["occurred_at"], at, tz)
            if not start <= day <= end:
                continue
            act.untimed_earnings += not timed
            act.earnings.append(EarningEvent(at=at, day=day, gross_minor=converted[0], fee_minor=converted[1],
                                             platform_id=r["platform_id"], kind=r["kind"], time_known=timed))

        trips = conn.execute(
            """SELECT id, started_at, ended_at, distance_m, platform_id, review_status
                 FROM mileage_logs
                WHERE deleted_at IS NULL AND purpose = 'business' AND trip_state = 'completed'
                  AND julianday(started_at) >= julianday(?) AND julianday(started_at) < julianday(?)
                ORDER BY julianday(started_at)""",
            (lo, hi),
        ).fetchall()
        for t in trips:
            if t["review_status"] == "needs_review":
                act.excluded_unreviewed += 1
                continue
            act.trips.append(TripRecord(id=t["id"], started_at=parse_iso(t["started_at"]),
                                        ended_at=parse_iso(t["ended_at"]) if t["ended_at"] else None,
                                        meters=int(t["distance_m"]), platform_id=t["platform_id"]))
    return act
