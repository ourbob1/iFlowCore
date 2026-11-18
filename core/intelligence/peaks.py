"""
Peak Earning Hours: the user's own most lucrative weekly windows.

Method
------
1. Baseline: the user's overall net per worked hour for the period.
2. Per hour-of-week slot, a *shrunk* rate:

       shrunk = (net × 3600 + PRIOR_S × baseline) / (seconds + PRIOR_S)

   i.e. each slot starts with PRIOR_S (one hour) of "average" evidence.
   A single lucky 20-minute order barely moves it; a slot worked for dozens
   of hours is judged on its own record. This keeps sparse data honest.
3. A slot is *hot* when it has enough evidence (worked in ≥ `min_weeks`
   different weeks, ≥ `min_seconds` in total) and its shrunk rate is at least
   `lift_bp` of the baseline (default 115%).
4. Consecutive hot slots merge into windows, wrapping across midnight and
   from Sunday into Monday, so "Fri 9 PM – 1 AM" is one window.
5. Windows are ranked by extra money over the baseline
   (window net − baseline × window hours), which favours windows that are
   both better *and* worked often.

Confidence grows with the number of distinct weeks behind a window.
With too little history the result says so instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.intelligence.models import WEEKDAY_NAMES
from core.intelligence.rates import div_round, per_hour, ratio_bp
from core.intelligence.temporal import HourCell, HourGrid, hour_label

PRIOR_S = 3600
DEFAULT_LIFT_BP = 11_500
DEFAULT_MIN_WEEKS = 3
DEFAULT_MIN_SECONDS = 30 * 60
MIN_TOTAL_SECONDS = 10 * 3600      # below this, overall history is too thin to call peaks
MAX_WINDOW_HOURS = 12


@dataclass(frozen=True)
class PeakWindow:
    weekday: int                   # local weekday the window starts on (0 = Monday)
    start_hour: int                # local, inclusive
    hours: int                     # length; may run past midnight
    net_minor: int
    seconds: int
    per_hour_minor: int
    lift_bp: int                   # window rate vs baseline (12000 = 120%)
    extra_minor: int               # money above what the same hours earn at the baseline rate
    weeks: int                     # fewest distinct weeks behind any slot in the window
    confidence: str                # low | medium | high

    @property
    def end_hour(self) -> int:
        return (self.start_hour + self.hours) % 24

    @property
    def label(self) -> str:
        return f"{WEEKDAY_NAMES[self.weekday]} {part_of_day(self.start_hour, self.hours)}"

    @property
    def time_range(self) -> str:
        return f"{hour_label(self.start_hour)} – {hour_label(self.end_hour)}"

    @property
    def slots(self) -> list[tuple[int, int]]:
        out = []
        for i in range(self.hours):
            idx = (self.weekday * 24 + self.start_hour + i) % 168
            out.append((idx // 24, idx % 24))
        return out


@dataclass(frozen=True)
class PeakResult:
    windows: tuple[PeakWindow, ...]
    baseline_per_hour_minor: int | None
    status: str                    # ok | no_time_data | not_enough_history | no_standouts
    message: str


def part_of_day(start_hour: int, hours: int = 1) -> str:
    mid = (start_hour + hours / 2) % 24
    if 5 <= mid < 11:
        return "morning"
    if 11 <= mid < 14:
        return "midday"
    if 14 <= mid < 17:
        return "afternoon"
    if 17 <= mid < 21:
        return "evening"
    return "night" if mid >= 21 or mid < 3 else "late night"


def confidence_for(weeks: int) -> str:
    return "high" if weeks >= 8 else "medium" if weeks >= 4 else "low"


def shrunk_rate(cell: HourCell, baseline: int) -> int:
    return div_round(cell.net_minor * 3600 + PRIOR_S * baseline, cell.seconds + PRIOR_S)


def find_peaks(grid: HourGrid, *, lift_bp: int = DEFAULT_LIFT_BP, min_weeks: int = DEFAULT_MIN_WEEKS,
               min_seconds: int = DEFAULT_MIN_SECONDS, limit: int = 5) -> PeakResult:
    baseline = grid.per_hour_minor
    if baseline is None:
        return PeakResult((), None, "no_time_data",
                          "Log trips (or turn on trip detection) so hours worked can be measured.")
    if grid.total_seconds < MIN_TOTAL_SECONDS or max(c.weeks for c in grid.cells) < min_weeks:
        return PeakResult((), baseline, "not_enough_history",
                          f"Peaks appear after about {MIN_TOTAL_SECONDS // 3600} hours of work over "
                          f"{min_weeks} or more weeks.")
    if baseline <= 0:
        return PeakResult((), baseline, "no_standouts", "No hours stand out above your average yet.")

    threshold = div_round(baseline * lift_bp, 10_000)
    hot = [c.weeks >= min_weeks and c.seconds >= min_seconds and shrunk_rate(c, baseline) >= threshold
           for c in grid.cells]
    if not any(hot):
        return PeakResult((), baseline, "no_standouts", "No hours stand out above your average yet.")

    runs: list[list[HourCell]] = []
    if all(hot):
        runs = [grid.cells[i:i + MAX_WINDOW_HOURS] for i in range(0, 168, MAX_WINDOW_HOURS)]
    else:
        first_cold = hot.index(False)       # start scanning after a cold slot so wrapped runs stay whole
        current: list[HourCell] = []
        for step in range(1, 169):
            i = (first_cold + step) % 168
            if hot[i]:
                current.append(grid.cells[i])
                if len(current) == MAX_WINDOW_HOURS:
                    runs.append(current)
                    current = []
            elif current:
                runs.append(current)
                current = []
        if current:
            runs.append(current)

    windows = []
    for run in runs:
        net = sum(c.net_minor for c in run)
        secs = sum(c.seconds for c in run)
        rate = per_hour(net, secs) or 0
        weeks = min(c.weeks for c in run)
        windows.append(PeakWindow(
            weekday=run[0].weekday, start_hour=run[0].hour, hours=len(run), net_minor=net, seconds=secs,
            per_hour_minor=rate, lift_bp=ratio_bp(rate, baseline) or 0,
            extra_minor=net - div_round(baseline * secs, 3600), weeks=weeks, confidence=confidence_for(weeks),
        ))
    windows.sort(key=lambda w: (w.extra_minor, w.lift_bp), reverse=True)
    return PeakResult(tuple(windows[:limit]), baseline, "ok",
                      f"Your best {min(limit, len(windows))} window(s), compared with your average hour.")
