-- =============================================================================
-- Migration 0010 — Phase 11: vendor statement formats (Parser Studio).
-- Signed schemas published by iFlow Core and installed on this device. Local
-- configuration, not user data: not synced (no sync columns).
-- =============================================================================

CREATE TABLE vendor_parsers (
    id                TEXT PRIMARY KEY NOT NULL CHECK (length(id) BETWEEN 3 AND 50),
    version           INTEGER NOT NULL CHECK (version >= 1),
    name              TEXT NOT NULL,
    platform          TEXT NOT NULL,
    schema_json       TEXT NOT NULL,
    bundle_issued_at  TEXT,
    installed_at      TEXT NOT NULL,
    enabled           INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
) WITHOUT ROWID;
