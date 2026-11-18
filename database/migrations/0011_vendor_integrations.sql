-- =============================================================================
-- Migration 0011 — Phase 11: platform API integrations (Integration Studio).
-- Signed integration schemas from the same bundle as statement formats. Local
-- configuration, not user data: not synced. Credentials are NOT stored here
-- (they live in the sealed device setting `integration_credentials`).
-- =============================================================================

CREATE TABLE vendor_integrations (
    id                TEXT PRIMARY KEY NOT NULL CHECK (length(id) BETWEEN 3 AND 50),
    version           INTEGER NOT NULL CHECK (version >= 1),
    name              TEXT NOT NULL,
    platform          TEXT NOT NULL,
    schema_json       TEXT NOT NULL,
    bundle_issued_at  TEXT,
    installed_at      TEXT NOT NULL,
    enabled           INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
) WITHOUT ROWID;
