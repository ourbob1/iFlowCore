"""
Design tokens.

Chart colors follow the validated reference palette (categorical slots 1–2,
checked with the dataviz validator in both modes: CVD ΔE ≥ 24, normal-vision
ΔE ≥ 31, ≥ 3:1 contrast on each surface). Status colors are reserved for
state and always ship with an icon + label. Text never wears series colors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Mode = Literal["light", "dark"]
Status = Literal["good", "warning", "serious", "critical"]

TOUCH_TARGET = 48          # Material minimum touch target (dp)
SPACE = 8                  # base spacing unit
RADIUS = 12


@dataclass(frozen=True)
class Palette:
    surface: str
    surface_raised: str
    grid: str
    text_primary: str
    text_secondary: str
    text_muted: str
    series_income: str      # categorical slot 1 (blue)
    series_expense: str     # categorical slot 2 (orange)
    # Heatmap buckets: sequential blue used as an *ordinal* ramp (5 discrete steps, low → high). Lowest step
    # clears 2:1 on the card surface (light 250 = 2.11:1; dark 550 = 2.34:1); each mode picks its own steps.
    heat: tuple[str, ...] = ()
    heat_empty: str = ""         # "no activity" cell: a surface step, distinct from the lowest magnitude


LIGHT = Palette(
    surface="#fcfcfb", surface_raised="#ffffff", grid="#e7e6e2",
    text_primary="#0b0b0b", text_secondary="#52514e", text_muted="#75746f",
    series_income="#2a78d6", series_expense="#eb6834",
    heat=("#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#0d366b"), heat_empty="#f0efec",
)
DARK = Palette(
    surface="#1a1a19", surface_raised="#242423", grid="#383835",
    text_primary="#ffffff", text_secondary="#c3c2b7", text_muted="#9a998f",
    series_income="#3987e5", series_expense="#d95926",
    heat=("#1c5cab", "#2a78d6", "#5598e7", "#86b6ef", "#cde2fb"), heat_empty="#2e2e2c",
)

STATUS_COLOR: dict[Status, str] = {
    "good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b",
}
STATUS_ICON: dict[Status, str] = {
    "good": "CHECK_CIRCLE", "warning": "INFO", "serious": "WARNING_AMBER", "critical": "ERROR",
}
STATUS_LABEL: dict[Status, str] = {
    "good": "On track", "warning": "Heads up", "serious": "Due soon", "critical": "Action needed",
}


def palette(mode: Mode) -> Palette:
    return DARK if mode == "dark" else LIGHT
