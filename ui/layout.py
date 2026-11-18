"""
Responsive layout rules (Material 3 window size classes).

    compact   < 600 dp   phones portrait      → bottom NavigationBar, 1 column
    medium    < 840 dp   phones landscape,    → NavigationRail, 2 columns
                         small tablets
    expanded  ≥ 840 dp   tablets, desktops    → extended NavigationRail, 3–4 columns

Views ask `LayoutSpec` how many columns to use and how big touch targets
must be; they never check the platform directly, so one codebase adapts to
any window.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ui.theme import TOUCH_TARGET

SizeClass = Literal["compact", "medium", "expanded"]

COMPACT_MAX = 600
MEDIUM_MAX = 840
WIDE_MIN = 1200


@dataclass(frozen=True)
class LayoutSpec:
    width: int
    size_class: SizeClass
    touch: bool

    @property
    def nav(self) -> Literal["bar", "rail", "rail_extended"]:
        if self.size_class == "compact":
            return "bar"
        return "rail" if self.size_class == "medium" else "rail_extended"

    @property
    def columns(self) -> int:
        if self.size_class == "compact":
            return 1
        if self.size_class == "medium":
            return 2
        return 4 if self.width >= WIDE_MIN else 3

    @property
    def kpi_col(self) -> dict[str, int]:
        """ResponsiveRow `col` for KPI tiles (12-column grid)."""
        return {"xs": 6, "md": 6, "lg": 3}

    @property
    def min_target(self) -> int:
        return TOUCH_TARGET if self.touch else 36

    @property
    def padding(self) -> int:
        return 12 if self.size_class == "compact" else 24

    @property
    def chart_height(self) -> int:
        return 220 if self.size_class == "compact" else 300


def size_class_for(width: float | None) -> SizeClass:
    w = int(width or 0)
    if w < COMPACT_MAX:
        return "compact"
    return "medium" if w < MEDIUM_MAX else "expanded"


def layout_for(width: float | None, platform: str | None = None) -> LayoutSpec:
    touch = (platform or "").lower() in ("android", "ios")
    w = int(width or 0) or (390 if touch else 1280)
    return LayoutSpec(w, size_class_for(w), touch)
