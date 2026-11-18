"""
Personal Intelligence (Phase 6): historical trends, peak earning hours, and
coarse-zone heatmaps, computed entirely on the device from the local
(encrypted) database. Nothing in this package talks to the network.

    data.py      read one period: earnings (base currency), confirmed business trips
    sessions.py  work sessions (hours worked) and exact local-hour time splitting
    temporal.py  hour-of-week grid, day/week/month trends
    peaks.py     peak earning windows with evidence-weighted (shrunk) rates
    zones.py     coarse geohash cells for trip ends: home-suppressed, sealed on mobile, never synced
    spatial.py   earnings ↔ drop-off matching, zone ranking, relative heat grid
    metrics.py   per hour / per mile / after-car-cost efficiency
    engine.py    build_report(): everything the Insights view needs
"""

from core.intelligence.engine import PERIODS, IntelligenceReport, build_report, period_bounds
from core.intelligence.zones import PrivateZone, ZoneStore, private_zones_from_settings

__all__ = ["PERIODS", "IntelligenceReport", "PrivateZone", "ZoneStore", "build_report", "period_bounds",
           "private_zones_from_settings"]
