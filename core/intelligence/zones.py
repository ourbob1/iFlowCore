"""
Coarse trip zones: where trips start and end, without keeping coordinates.

When the motion sentry finishes a trip, `ZoneStore.record` turns the first
and last GPS points into geohash cells (precision 6 ≈ 1.2 km × 0.6 km in the
mid-latitudes) and throws the points away. What is kept, in `trip_zones`:

  * only the cell id: an area shared by thousands of addresses
  * nothing at all near a private place: points within the home radius (or
    any other private zone) plus a margin are dropped, so the cell containing
    home is never written
  * never synced or exported: `trip_zones` isn't a sync table, and no code
    path sends it anywhere
  * sealed on phones without SQLCipher: with a device key the value is
    AES-GCM sealed and bound to its trip and column, like sensitive settings;
    without one (sealing required but unavailable) nothing is stored

Users can wipe the table from Settings (`forget_all`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from core.fleet.geo import GeoPoint, geohash, haversine_m

DEFAULT_PRECISION = 6
PRIVACY_MARGIN_M = 750          # beyond the private radius: a precision-6 cell is ~1.2 km across


@dataclass(frozen=True)
class PrivateZone:
    center: GeoPoint
    radius_m: int


class ZoneStore:
    def __init__(self, db: Any, *, sealer: Any | None = None, require_sealing: bool = False) -> None:
        self.db = db
        self.sealer = sealer
        self.require_sealing = require_sealing
        self.unreadable = 0

    # -- writing ---------------------------------------------------------------------

    @staticmethod
    def is_private(point: GeoPoint, private: Sequence[PrivateZone], margin_m: int = PRIVACY_MARGIN_M) -> bool:
        return any(haversine_m(point, z.center) <= z.radius_m + margin_m for z in private)

    def cell_for(self, point: GeoPoint | None, *, precision: int, private: Sequence[PrivateZone]) -> str | None:
        if point is None or self.is_private(point, private):
            return None
        cell = geohash(point, precision)
        if any(cell == geohash(z.center, precision) for z in private):
            return None                         # outside the margin but still in home's own cell
        return cell

    def _encode(self, cell: str | None, trip_id: str, column: str) -> str | None:
        if cell is None:
            return None
        if self.sealer is not None:
            return json.dumps({"sealed": self.sealer.seal(cell, context=f"trip_zones.{trip_id}.{column}")})
        return cell

    def record(self, trip_id: str, start: GeoPoint | None, end: GeoPoint | None, *,
               precision: int = DEFAULT_PRECISION, private: Sequence[PrivateZone] = ()) -> bool:
        """Store the trip's cells. Returns False when nothing was stored."""
        if not 4 <= precision <= 7:
            raise ValueError("precision must be 4..7")
        if self.sealer is None and self.require_sealing:
            return False                        # never write location in the clear on unsealed storage
        s_cell = self.cell_for(start, precision=precision, private=private)
        e_cell = self.cell_for(end, precision=precision, private=private)
        if s_cell is None and e_cell is None:
            return False
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM mileage_logs WHERE id = ?", (trip_id,)).fetchone() is None:
                return False
            conn.execute(
                "INSERT INTO trip_zones (trip_id, precision, start_zone, end_zone) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (trip_id) DO UPDATE SET precision = excluded.precision, "
                "start_zone = excluded.start_zone, end_zone = excluded.end_zone",
                (trip_id, precision, self._encode(s_cell, trip_id, "start"), self._encode(e_cell, trip_id, "end")),
            )
        return True

    # -- reading ---------------------------------------------------------------------

    def _decode(self, raw: str | None, trip_id: str, column: str) -> str | None:
        if raw is None:
            return None
        if raw.startswith("{"):
            from core.crypto import SealingError

            try:
                token = json.loads(raw)["sealed"]
                if self.sealer is None:
                    raise SealingError("no key")
                return self.sealer.open(token, context=f"trip_zones.{trip_id}.{column}")
            except (SealingError, KeyError, ValueError, TypeError):
                self.unreadable += 1
                return None
        return raw

    def load(self, trip_ids: Iterable[str] | None = None) -> dict[str, tuple[str | None, str | None]]:
        """trip_id → (start cell, end cell) for live trips."""
        self.unreadable = 0
        wanted = set(trip_ids) if trip_ids is not None else None
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT z.trip_id, z.start_zone, z.end_zone FROM trip_zones z "
                "JOIN mileage_logs m ON m.id = z.trip_id WHERE m.deleted_at IS NULL"
            ).fetchall()
        out = {}
        for r in rows:
            if wanted is not None and r["trip_id"] not in wanted:
                continue
            out[r["trip_id"]] = (self._decode(r["start_zone"], r["trip_id"], "start"),
                                 self._decode(r["end_zone"], r["trip_id"], "end"))
        return out

    def count(self) -> int:
        with self.db.connection() as conn:
            return conn.execute("SELECT count(*) FROM trip_zones").fetchone()[0]

    def forget_all(self) -> int:
        with self.db.transaction() as conn:
            return conn.execute("DELETE FROM trip_zones").rowcount


def private_zones_from_settings(settings: Any) -> list[PrivateZone]:
    """Home is private. Work zones aren't suppressed: they're where the work happens."""
    zones = []
    for z in [settings.home] if settings.home else []:
        zones.append(PrivateZone(GeoPoint(float(z["lat"]), float(z["lng"])), int(z.get("radius_m", 300))))
    return zones
