-- =============================================================================
-- Migration 0005 — Phase 4: auto-detected trips, device settings, demand-feed
-- cache, and tax set-aside tracking.
-- =============================================================================

-- Trips detected by the motion sentry are saved for the user to review.
ALTER TABLE mileage_logs ADD COLUMN detection TEXT NOT NULL DEFAULT 'manual'
    CHECK (detection IN ('manual', 'auto'));
ALTER TABLE mileage_logs ADD COLUMN review_status TEXT NOT NULL DEFAULT 'confirmed'
    CHECK (review_status IN ('confirmed', 'needs_review'));
CREATE INDEX ix_mileage_needs_review ON mileage_logs (started_at)
    WHERE review_status = 'needs_review' AND deleted_at IS NULL;


-- device_settings: per-device preferences that are sensitive or device-specific
-- (home location, feed URLs, sentry thresholds). Lives inside the encrypted DB
-- rather than preferences.json. Not synced.
CREATE TABLE device_settings (
    key         TEXT PRIMARY KEY NOT NULL CHECK (length(key) BETWEEN 1 AND 100),
    value_json  TEXT NOT NULL CHECK (json_valid(value_json)),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);


-- demand_feed_cache: last successful payload per feed so the radar works offline.
-- Not synced.
CREATE TABLE demand_feed_cache (
    source_key    TEXT PRIMARY KEY NOT NULL CHECK (length(source_key) BETWEEN 1 AND 300),
    payload_json  TEXT NOT NULL CHECK (json_valid(payload_json)),
    fetched_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at    TEXT NOT NULL CHECK (datetime(expires_at) IS NOT NULL)
);


-- tax_set_asides: money moved into a tax reserve, estimated payments made, or
-- reserve withdrawals. Drives the dashboard's set-aside alerts. Synced.
CREATE TABLE tax_set_asides (
    id                    TEXT PRIMARY KEY NOT NULL DEFAULT (
                              lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
                              substr(lower(hex(randomblob(2))), 2) || '-' ||
                              substr('89ab', 1 + (abs(random()) % 4), 1) ||
                              substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))),
    tax_year              INTEGER NOT NULL CHECK (tax_year BETWEEN 2000 AND 2100),
    kind                  TEXT NOT NULL CHECK (kind IN ('reserve', 'payment', 'withdrawal')),
    amount_minor          INTEGER NOT NULL CHECK (typeof(amount_minor) = 'integer' AND amount_minor > 0),
    currency              TEXT NOT NULL DEFAULT 'USD' CHECK (length(currency) = 3 AND currency = upper(currency)),
    occurred_on           TEXT NOT NULL CHECK (date(occurred_on) IS NOT NULL),
    note                  TEXT,

    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at            TEXT,
    version               INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    sync_status           TEXT NOT NULL DEFAULT 'pending' CHECK (sync_status IN ('pending', 'synced', 'conflict')),
    last_synced_at        TEXT,
    last_modified_device  TEXT,

    CHECK (length(id) = 36 AND id = lower(id))
);

CREATE INDEX ix_tax_set_asides_year ON tax_set_asides (tax_year, occurred_on) WHERE deleted_at IS NULL;
CREATE INDEX ix_tax_set_asides_sync_pending ON tax_set_asides (updated_at) WHERE sync_status <> 'synced';

CREATE TRIGGER trg_tax_set_asides_immutable BEFORE UPDATE OF id, created_at ON tax_set_asides
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'tax_set_asides: id and created_at are immutable'); END;

CREATE TRIGGER trg_tax_set_asides_touch AFTER UPDATE ON tax_set_asides
WHEN NEW.updated_at IS OLD.updated_at AND NEW.sync_status IS OLD.sync_status
BEGIN
    UPDATE tax_set_asides
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
           version = OLD.version + 1,
           sync_status = 'pending'
     WHERE id = NEW.id;
END;
