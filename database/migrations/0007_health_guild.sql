-- =============================================================================
-- Migration 0007 — Phase 7: iFlow Health & Guild.
-- Association Health Plan (AHP) memberships and the premium reserve ledger.
-- Both synced like tax_set_asides (to the user's own sync server only).
-- =============================================================================

-- health_plans: an AHP (or any health plan) the user pays a monthly premium into.
CREATE TABLE health_plans (
    id                    TEXT PRIMARY KEY NOT NULL DEFAULT (
                              lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
                              substr(lower(hex(randomblob(2))), 2) || '-' ||
                              substr('89ab', 1 + (abs(random()) % 4), 1) ||
                              substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))),
    association           TEXT NOT NULL CHECK (length(trim(association)) BETWEEN 1 AND 120),
    plan_name             TEXT NOT NULL CHECK (length(trim(plan_name)) BETWEEN 1 AND 120),
    monthly_premium_minor INTEGER NOT NULL CHECK (typeof(monthly_premium_minor) = 'integer' AND monthly_premium_minor > 0),
    currency              TEXT NOT NULL DEFAULT 'USD' CHECK (length(currency) = 3 AND currency = upper(currency)),
    due_day               INTEGER NOT NULL DEFAULT 1 CHECK (due_day BETWEEN 1 AND 28),
    members_covered       INTEGER NOT NULL DEFAULT 1 CHECK (members_covered BETWEEN 1 AND 20),
    coverage_start        TEXT NOT NULL CHECK (date(coverage_start) IS NOT NULL),
    coverage_end          TEXT CHECK (coverage_end IS NULL OR date(coverage_end) IS NOT NULL),
    withholding_bp        INTEGER CHECK (withholding_bp IS NULL OR withholding_bp BETWEEN 1 AND 10000),
    is_active             INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),

    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at            TEXT,
    version               INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    sync_status           TEXT NOT NULL DEFAULT 'pending' CHECK (sync_status IN ('pending', 'synced', 'conflict')),
    last_synced_at        TEXT,
    last_modified_device  TEXT,

    CHECK (length(id) = 36 AND id = lower(id)),
    CHECK (coverage_end IS NULL OR date(coverage_end) >= date(coverage_start))
);

CREATE INDEX ix_health_plans_active ON health_plans (is_active) WHERE deleted_at IS NULL;
CREATE INDEX ix_health_plans_sync_pending ON health_plans (updated_at) WHERE sync_status <> 'synced';

CREATE TRIGGER trg_health_plans_immutable BEFORE UPDATE OF id, created_at ON health_plans
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'health_plans: id and created_at are immutable'); END;

CREATE TRIGGER trg_health_plans_touch AFTER UPDATE ON health_plans
WHEN NEW.updated_at IS OLD.updated_at AND NEW.sync_status IS OLD.sync_status
BEGIN
    UPDATE health_plans
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), version = OLD.version + 1, sync_status = 'pending'
     WHERE id = NEW.id;
END;


-- health_premium_entries: money set aside for premiums (reserve), premiums paid to the
-- pool (payment), or reserve taken back out (withdrawal). Mirrors tax_set_asides.
CREATE TABLE health_premium_entries (
    id                    TEXT PRIMARY KEY NOT NULL DEFAULT (
                              lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
                              substr(lower(hex(randomblob(2))), 2) || '-' ||
                              substr('89ab', 1 + (abs(random()) % 4), 1) ||
                              substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))),
    plan_id               TEXT NOT NULL REFERENCES health_plans (id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    kind                  TEXT NOT NULL CHECK (kind IN ('reserve', 'payment', 'withdrawal')),
    amount_minor          INTEGER NOT NULL CHECK (typeof(amount_minor) = 'integer' AND amount_minor > 0),
    currency              TEXT NOT NULL DEFAULT 'USD' CHECK (length(currency) = 3 AND currency = upper(currency)),
    occurred_on           TEXT NOT NULL CHECK (date(occurred_on) IS NOT NULL),
    covers_month          TEXT CHECK (covers_month IS NULL OR (length(covers_month) = 7 AND date(covers_month || '-01') IS NOT NULL)),
    note                  TEXT,

    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at            TEXT,
    version               INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    sync_status           TEXT NOT NULL DEFAULT 'pending' CHECK (sync_status IN ('pending', 'synced', 'conflict')),
    last_synced_at        TEXT,
    last_modified_device  TEXT,

    CHECK (length(id) = 36 AND id = lower(id)),
    CHECK (kind <> 'payment' OR covers_month IS NOT NULL)
);

CREATE INDEX ix_health_entries_plan ON health_premium_entries (plan_id, occurred_on) WHERE deleted_at IS NULL;
CREATE INDEX ix_health_entries_sync_pending ON health_premium_entries (updated_at) WHERE sync_status <> 'synced';

CREATE TRIGGER trg_health_entries_immutable BEFORE UPDATE OF id, created_at ON health_premium_entries
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'health_premium_entries: id and created_at are immutable'); END;

CREATE TRIGGER trg_health_entries_touch AFTER UPDATE ON health_premium_entries
WHEN NEW.updated_at IS OLD.updated_at AND NEW.sync_status IS OLD.sync_status
BEGIN
    UPDATE health_premium_entries
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), version = OLD.version + 1, sync_status = 'pending'
     WHERE id = NEW.id;
END;
