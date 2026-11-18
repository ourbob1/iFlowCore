-- =============================================================================
-- Migration 0002 — Phase 2: import mapping profiles + receipt attachments
-- Follows every convention from schema.sql (UUIDs, sync columns, soft deletes,
-- touch triggers).
-- =============================================================================

CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY NOT NULL,
    date TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    description TEXT,
    category TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- -----------------------------------------------------------------------------
-- import_mappings: user-editable column mappings for statement ingestion.
-- mapping_json holds a serialized core.financial.ingestion.MappingProfile, so
-- users can add/adjust platforms without code or schema changes.
-- -----------------------------------------------------------------------------
CREATE TABLE import_mappings (
    id                    TEXT PRIMARY KEY NOT NULL DEFAULT (
                              lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
                              substr(lower(hex(randomblob(2))), 2) || '-' ||
                              substr('89ab', 1 + (abs(random()) % 4), 1) ||
                              substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))),
    name                  TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 120),
    platform_id           TEXT REFERENCES platforms (id) ON UPDATE RESTRICT ON DELETE SET NULL,
    source_format         TEXT NOT NULL CHECK (source_format IN ('csv', 'pdf')),
    mapping_version       INTEGER NOT NULL DEFAULT 1 CHECK (mapping_version >= 1),
    mapping_json          TEXT NOT NULL CHECK (json_valid(mapping_json)),

    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at            TEXT,
    version               INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    sync_status           TEXT NOT NULL DEFAULT 'pending' CHECK (sync_status IN ('pending', 'synced', 'conflict')),
    last_synced_at        TEXT,
    last_modified_device  TEXT,

    CHECK (length(id) = 36 AND id = lower(id))
);

CREATE UNIQUE INDEX ux_import_mappings_name_live ON import_mappings (name COLLATE NOCASE) WHERE deleted_at IS NULL;
CREATE INDEX ix_import_mappings_platform ON import_mappings (platform_id);
CREATE INDEX ix_import_mappings_sync_pending ON import_mappings (updated_at) WHERE sync_status <> 'synced';


-- -----------------------------------------------------------------------------
-- receipts: local files linked to an expense OR an income transaction.
-- Multiple receipts per record are allowed. sha256 detects moved/edited files
-- and de-duplicates the managed receipt store.
-- (expenses.receipt_uri from v1 is kept as the "primary receipt" shortcut.)
-- -----------------------------------------------------------------------------
CREATE TABLE receipts (
    id                    TEXT PRIMARY KEY NOT NULL DEFAULT (
                              lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
                              substr(lower(hex(randomblob(2))), 2) || '-' ||
                              substr('89ab', 1 + (abs(random()) % 4), 1) ||
                              substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))),
    expense_id            TEXT REFERENCES expenses (id) ON UPDATE RESTRICT ON DELETE CASCADE,
    transaction_id        TEXT REFERENCES transactions (id) ON UPDATE RESTRICT ON DELETE CASCADE,
    file_path             TEXT NOT NULL CHECK (length(file_path) > 0),
    storage_mode          TEXT NOT NULL DEFAULT 'managed' CHECK (storage_mode IN ('managed', 'linked')),
    original_filename     TEXT,
    mime_type             TEXT NOT NULL,
    size_bytes            INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256                TEXT NOT NULL CHECK (length(sha256) = 64),

    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at            TEXT,
    version               INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    sync_status           TEXT NOT NULL DEFAULT 'pending' CHECK (sync_status IN ('pending', 'synced', 'conflict')),
    last_synced_at        TEXT,
    last_modified_device  TEXT,

    CHECK (length(id) = 36 AND id = lower(id)),
    -- Exactly one parent record.
    CHECK ((expense_id IS NULL) <> (transaction_id IS NULL))
);

CREATE INDEX ix_receipts_expense ON receipts (expense_id);
CREATE INDEX ix_receipts_transaction ON receipts (transaction_id);
CREATE INDEX ix_receipts_sha256 ON receipts (sha256);
CREATE INDEX ix_receipts_sync_pending ON receipts (updated_at) WHERE sync_status <> 'synced';


-- ---- triggers (same pattern as schema.sql) ----------------------------------
CREATE TRIGGER trg_import_mappings_immutable BEFORE UPDATE OF id, created_at ON import_mappings
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'import_mappings: id and created_at are immutable'); END;

CREATE TRIGGER trg_import_mappings_touch AFTER UPDATE ON import_mappings
WHEN NEW.updated_at IS OLD.updated_at AND NEW.sync_status IS OLD.sync_status
BEGIN
    UPDATE import_mappings
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
           version = OLD.version + 1,
           sync_status = 'pending'
     WHERE id = NEW.id;
END;

CREATE TRIGGER trg_receipts_immutable BEFORE UPDATE OF id, created_at ON receipts
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'receipts: id and created_at are immutable'); END;

CREATE TRIGGER trg_receipts_touch AFTER UPDATE ON receipts
WHEN NEW.updated_at IS OLD.updated_at AND NEW.sync_status IS OLD.sync_status
BEGIN
    UPDATE receipts
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
           version = OLD.version + 1,
           sync_status = 'pending'
     WHERE id = NEW.id;
END;
