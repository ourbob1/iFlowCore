"""
Plain data shared by the Personal Intelligence modules.

Conventions (same as the rest of the app):
  * money: integer minor units of the user's base currency
  * distance: integer meters
  * time: aware UTC datetimes in; every calendar decision (hour of day,
    weekday, week, month, year) uses the user's local timezone
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
HOURS_PER_WEEK = 168


@dataclass(frozen=True)
class EarningEvent:
    """One income row from `transactions`, converted to the base currency."""

    at: datetime                 # aware UTC
    day: date                    # local calendar date (UTC date for UTC-midnight date-only rows)
    gross_minor: int
    fee_minor: int
    platform_id: str | None
    kind: str
    time_known: bool = True      # False for date-only imports (stored as local/UTC midnight)

    @property
    def net_minor(self) -> int:
        return self.gross_minor - self.fee_minor


@dataclass(frozen=True)
class TripRecord:
    """One completed, confirmed business trip from `mileage_logs`."""

    id: str
    started_at: datetime         # aware UTC
    ended_at: datetime | None    # None: manual entry without an end time
    meters: int
    platform_id: str | None

    @property
    def duration_s(self) -> int | None:
        if self.ended_at is None:
            return None
        return max(0, int((self.ended_at - self.started_at).total_seconds()))


@dataclass(frozen=True)
class Interval:
    start: datetime              # aware UTC
    end: datetime

    @property
    def seconds(self) -> int:
        return max(0, int((self.end - self.start).total_seconds()))


@dataclass(frozen=True)
class Session:
    """A stretch of work: trips and earnings with no gap longer than the threshold."""

    start: datetime
    end: datetime
    trips: int
    earnings: int                # number of earning events inside

    @property
    def seconds(self) -> int:
        return max(0, int((self.end - self.start).total_seconds()))


@dataclass
class Activity:
    """Everything the engine reads for one period, already filtered and converted."""

    start: date                  # inclusive local date
    end: date                    # inclusive local date
    tz: str
    currency: str
    earnings: list[EarningEvent] = field(default_factory=list)
    trips: list[TripRecord] = field(default_factory=list)
    excluded_unconverted: int = 0     # foreign-currency rows without a base amount
    excluded_unreviewed: int = 0      # auto-detected trips still awaiting review
    untimed_earnings: int = 0         # date-only rows (kept in totals, left out of hour-of-day views)

    @property
    def empty(self) -> bool:
        return not self.earnings and not self.trips

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1
