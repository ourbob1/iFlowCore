"""
Provider-neutral vehicle models.

Units follow the project rules: money in integer minor units, distances in
meters, and engine displacement in integer milliliters (cc).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Iterable, Literal, Mapping

FuelType = Literal["gasoline", "diesel", "hybrid", "plug_in_hybrid", "electric", "flex_fuel", "hydrogen", "other"]
DriveType = Literal["fwd", "rwd", "awd", "4wd", "other"]

METERS_PER_MILE_INT_NUM = 1609344      # meters × 1000 per mile, for exact int math


def miles_to_meters(miles: int | str) -> int:
    """Exact integer conversion (rounded half-up to the meter)."""
    thousandths = int(miles) * METERS_PER_MILE_INT_NUM
    return (thousandths + 500) // 1000


# ---------------------------------------------------------------------------
# Normalizers shared by providers
# ---------------------------------------------------------------------------


def normalize_fuel(*texts: str | None) -> FuelType | None:
    blob = " ".join(t for t in texts if t).casefold()
    if not blob.strip():
        return None
    if "plug-in" in blob or "phev" in blob:
        return "plug_in_hybrid"
    if re.search(r"\b(bev|battery electric|electric)\b", blob) and "hybrid" not in blob and "gasoline" not in blob:
        return "electric"
    if "hybrid" in blob or "hev" in blob or ("electric" in blob and "gasoline" in blob):
        return "hybrid"
    if "flex" in blob or "e85" in blob or "ethanol" in blob:
        return "flex_fuel"
    if "diesel" in blob:
        return "diesel"
    if "hydrogen" in blob or "fuel cell" in blob:
        return "hydrogen"
    if "gas" in blob or "petrol" in blob:
        return "gasoline"
    return "other"


def normalize_drive(text: str | None) -> DriveType | None:
    if not text:
        return None
    t = text.casefold()
    if "4wd" in t or "4x4" in t or "four" in t:
        return "4wd"
    if "awd" in t or "all-wheel" in t or "all wheel" in t:
        return "awd"
    if "fwd" in t or "front" in t:
        return "fwd"
    if "rwd" in t or "rear" in t:
        return "rwd"
    return "other"


# Normalized equipment keys → phrases found in provider equipment text.
# Extend here (or at runtime via EquipmentMapper(extra=...)) — callers only
# ever see the stable keys on the left.
EQUIPMENT_PATTERNS: Mapping[str, tuple[str, ...]] = {
    "backup_camera": ("backup camera", "rear view camera", "rearview camera", "rear camera", "rear visibility"),
    "blind_spot_monitor": ("blind spot", "blind-spot"),
    "adaptive_cruise": ("adaptive cruise", "radar cruise", "dynamic radar cruise"),
    "forward_collision_warning": ("forward collision", "pre-collision", "precollision"),
    "automatic_emergency_braking": ("automatic emergency braking", "crash imminent braking", "autonomous emergency braking", "pedestrian detection"),
    "lane_keep_assist": ("lane keep", "lane keeping", "lane centering", "lane tracing"),
    "lane_departure_warning": ("lane departure",),
    "apple_carplay": ("apple carplay", "carplay"),
    "android_auto": ("android auto",),
    "bluetooth": ("bluetooth", "hands-free"),
    "navigation": ("navigation",),
    "usb_charging": ("usb",),
    "wireless_charging": ("wireless charging", "qi charging"),
    "heated_seats": ("heated front seats", "heated seats", "heated driver"),
    "leather_seats": ("leather",),
    "sunroof": ("sunroof", "moonroof"),
    "third_row_seating": ("third row", "3rd row"),
    "keyless_entry": ("keyless entry", "smart key", "proximity key"),
    "remote_start": ("remote start",),
    "tow_package": ("tow package", "trailer hitch", "towing package"),
    "all_wheel_drive": ("all-wheel drive", "all wheel drive", "awd"),
}


class EquipmentMapper:
    """Map free-text equipment lines to normalized keys."""

    def __init__(self, extra: Mapping[str, Iterable[str]] | None = None) -> None:
        patterns = {k: tuple(v) for k, v in EQUIPMENT_PATTERNS.items()}
        for key, phrases in (extra or {}).items():
            patterns[key] = (*patterns.get(key, ()), *phrases)
        self._compiled = {
            key: re.compile("|".join(rf"(?<![a-z]){re.escape(p.casefold())}(?![a-z])" for p in phrases))
            for key, phrases in patterns.items()
        }

    def map_text(self, text: str) -> set[str]:
        t = text.casefold()
        return {key for key, rx in self._compiled.items() if rx.search(t)}

    def map_items(self, items: Iterable[str]) -> frozenset[str]:
        found: set[str] = set()
        for item in items:
            found |= self.map_text(item)
        return frozenset(found)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaintenanceItem:
    service: str
    category: str = "general"
    interval_meters: int | None = None
    interval_months: int | None = None
    severe_interval_meters: int | None = None
    severe_interval_months: int | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if self.interval_meters is None and self.interval_months is None:
            raise ValueError(f"Maintenance item '{self.service}' needs a distance or time interval")


@dataclass(frozen=True)
class DueItem:
    item: MaintenanceItem
    status: Literal["overdue", "due_soon", "ok"]
    remaining_meters: int | None
    remaining_days: int | None


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    days_in_month = [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return date(year, month, min(d.day, days_in_month[month - 1]))


def maintenance_due(
    items: Iterable[MaintenanceItem],
    *,
    odometer_m: int,
    today: date,
    last_service: Mapping[str, tuple[int, date]] | None = None,
    severe: bool = False,
    soon_meters: int = 804672,     # 500 miles
    soon_days: int = 30,
) -> list[DueItem]:
    """Evaluate each schedule item against the current odometer and date.

    `last_service` maps service name → (odometer_m, date) when last done;
    items never done are measured from zero / `today`. Gig drivers put on
    heavy miles, so `severe=True` (the "severe service" schedule) is the
    sensible default for full-time rideshare/delivery use.
    """
    last_service = last_service or {}
    out: list[DueItem] = []
    for item in items:
        interval_m = (item.severe_interval_meters if severe and item.severe_interval_meters else item.interval_meters)
        interval_mo = (item.severe_interval_months if severe and item.severe_interval_months else item.interval_months)
        done_m, done_d = last_service.get(item.service, (0, None))

        rem_m = (done_m + interval_m - odometer_m) if interval_m else None
        rem_d = ((_add_months(done_d, interval_mo) - today).days) if (interval_mo and done_d) else None

        if (rem_m is not None and rem_m <= 0) or (rem_d is not None and rem_d <= 0):
            status = "overdue"
        elif (rem_m is not None and rem_m <= soon_meters) or (rem_d is not None and rem_d <= soon_days):
            status = "due_soon"
        else:
            status = "ok"
        out.append(DueItem(item, status, rem_m, rem_d))
    order = {"overdue": 0, "due_soon": 1, "ok": 2}
    out.sort(key=lambda d: (order[d.status], d.remaining_meters if d.remaining_meters is not None else 1 << 62))
    return out


@dataclass(frozen=True)
class VehicleStyle:
    """One trim/style candidate for a VIN (DataOne may return several)."""

    style_id: str
    name: str
    year: int | None = None
    make: str | None = None
    model: str | None = None
    trim: str | None = None
    body_type: str | None = None
    doors: int | None = None
    drive_type: DriveType | None = None
    fuel_type: FuelType | None = None
    engine_cylinders: int | None = None
    engine_displacement_ml: int | None = None
    seating_capacity: int | None = None
    mpg_city: int | None = None
    mpg_highway: int | None = None
    mpg_combined: int | None = None
    msrp_minor: int | None = None
    currency: str = "USD"
    equipment: frozenset[str] = frozenset()
    equipment_raw: tuple[str, ...] = ()
    maintenance: tuple[MaintenanceItem, ...] = ()


@dataclass(frozen=True)
class VehicleProfile:
    """Merged view of a decoded vehicle."""

    vin: str
    year: int | None = None
    make: str | None = None
    model: str | None = None
    trim: str | None = None
    series: str | None = None
    body_type: str | None = None
    doors: int | None = None
    drive_type: DriveType | None = None
    fuel_type: FuelType | None = None
    engine_cylinders: int | None = None
    engine_displacement_ml: int | None = None
    seating_capacity: int | None = None
    mpg_city: int | None = None
    mpg_highway: int | None = None
    mpg_combined: int | None = None
    msrp_minor: int | None = None
    currency: str = "USD"
    style_id: str | None = None
    equipment: frozenset[str] = frozenset()
    maintenance: tuple[MaintenanceItem, ...] = ()
    trim_candidates: tuple[VehicleStyle, ...] = ()
    sources: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return " ".join(str(p) for p in (self.year, self.make, self.model, self.trim) if p)

    def overlay(self, style: VehicleStyle, source: str) -> "VehicleProfile":
        """Return a copy with non-empty fields from `style` taking precedence."""
        updates = {}
        for name in ("year", "make", "model", "trim", "body_type", "doors", "drive_type", "fuel_type",
                     "engine_cylinders", "engine_displacement_ml", "seating_capacity", "mpg_city",
                     "mpg_highway", "mpg_combined", "msrp_minor"):
            value = getattr(style, name)
            if value not in (None, ""):
                updates[name] = value
        if style.msrp_minor is not None:
            updates["currency"] = style.currency
        return replace(
            self,
            **updates,
            style_id=style.style_id,
            equipment=self.equipment | style.equipment,
            maintenance=style.maintenance or self.maintenance,
            sources=(*self.sources, source) if source not in self.sources else self.sources,
        )
