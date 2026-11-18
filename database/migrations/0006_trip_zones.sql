-- =============================================================================
-- Migration 0006 — Phase 6: coarse trip zones for Personal Intelligence.
-- =============================================================================

-- trip_zones: the geohash CELL (never coordinates) a trip started and ended in,
-- recorded by the motion sentry at trip end. Precision 6 ≈ 1.2 km × 0.6 km.
-- Cells inside the user's home radius are never stored (NULL).
--
-- LOCAL ONLY: not synced, not exported, never sent to any server. On mobile
-- "platform" storage the values are sealed with the device key
-- ({"sealed": ...} JSON), like sensitive device settings.
CREATE TABLE trip_zones (
    trip_id     TEXT PRIMARY KEY NOT NULL REFERENCES mileage_logs (id) ON UPDATE RESTRICT ON DELETE CASCADE,
    precision   INTEGER NOT NULL CHECK (precision BETWEEN 4 AND 7),
    start_zone  TEXT CHECK (start_zone IS NULL OR length(start_zone) BETWEEN 4 AND 400),
    end_zone    TEXT CHECK (end_zone IS NULL OR length(end_zone) BETWEEN 4 AND 400),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
