-- =============================================================================
-- Migration 0009 — Phase 10.3: end-to-end encrypted sync, device-local bookkeeping.
-- None of these tables are synced themselves.
--   * sync_versions: the server version each record had when this device last
--     saw it (the `base_version` for the next push of that record).
--   * sync_conflicts: when two devices edit the same record, the newest edit
--     wins and the other copy is kept here for review (JSON, stays on device).
-- The pull cursor lives in sync_meta ('pull_cursor:<account id>').
-- =============================================================================

CREATE TABLE sync_versions (
    table_name      TEXT NOT NULL CHECK (length(table_name) BETWEEN 1 AND 64),
    record_id       TEXT NOT NULL,
    server_version  INTEGER NOT NULL CHECK (server_version >= 1),
    PRIMARY KEY (table_name, record_id)
) WITHOUT ROWID;

CREATE TABLE sync_conflicts (
    id              TEXT PRIMARY KEY NOT NULL,
    table_name      TEXT NOT NULL,
    record_id       TEXT NOT NULL,
    kept            TEXT NOT NULL CHECK (kept IN ('local', 'remote')),
    local_json      TEXT NOT NULL,
    remote_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at     TEXT
);
CREATE INDEX ix_sync_conflicts_open ON sync_conflicts (created_at) WHERE resolved_at IS NULL;

-- When two devices created the same real-world thing before syncing (same platform name, VIN,
-- imported statement line …), the row with the smaller id survives everywhere; the other is
-- soft-deleted and its id maps here, so children that still point at it are re-pointed.
CREATE TABLE sync_aliases (
    table_name  TEXT NOT NULL,
    old_id      TEXT NOT NULL,
    new_id      TEXT NOT NULL,
    PRIMARY KEY (table_name, old_id)
) WITHOUT ROWID;
