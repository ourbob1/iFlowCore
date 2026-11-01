-- =============================================================================
-- iFlow Core — local database schema, version 1 (baseline)
-- =============================================================================
--
-- Conventions (apply to every synced table)
-- -----------------------------------------------------------------------------
-- * Primary keys:  TEXT UUIDs (lower-case, 36 chars). The app generates them
--   (so rows can be created offline on any device without collisions); the
--   DEFAULT below is a UUIDv4 safety net for ad-hoc inserts.
-- * Money:         INTEGER minor units (cents, pence, yen...) + ISO 4217 code.
--   Never REAL. Exponent per currency lives in core/config.py.
--   `base_amount_minor` = amount converted to the user's base currency at
--   record time, so reports never depend on live FX rates.
-- * Rates:         INTEGER basis points (1 bp = 0.01%; 10000 bp = 100%).
-- * Distance:      INTEGER meters. Imperial/metric is a display concern only.
-- * Timestamps:    ISO-8601 UTC text, millisecond precision, 'Z' suffix.
-- * Deletes:       soft (`deleted_at` tombstone) so deletions can sync.
--
-- Sync tracking columns
-- -----------------------------------------------------------------------------
--   version          monotonically increasing per row; bumped on every local edit
--   sync_status      'pending'  = local change not yet pushed
--                    'synced'   = matches server as of last_synced_at
--                    'conflict' = server rejected; needs resolution
--   last_synced_at   when the row last matched the server
--   last_modified_device  device id that made the latest edit (set by app)
--
-- Auto-touch triggers
-- -----------------------------------------------------------------------------
-- A normal app UPDATE that does not itself set `updated_at` or `sync_status`
-- automatically gets: updated_at = now, version + 1, sync_status = 'pending'.
-- The sync engine bypasses this by writing `updated_at` / `sync_status`
-- explicitly (e.g. applying a server row, or marking a row as synced).
-- Requires PRAGMA recursive_triggers = OFF (SQLite default; enforced by
-- database/connection.py).
--
-- Do NOT add PRAGMAs or BEGIN/COMMIT here — the migration runner owns those.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- platforms: income sources (Uber, DoorDash, Upwork, a direct client, ...)
-- -----------------------------------------------------------------------------
CREATE TABLE platforms (
    id                    TEXT PRIMARY KEY NOT NULL DEFAULT (
                              lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
                              substr(lower(hex(randomblob(2))), 2) || '-' ||
                              substr('89ab', 1 + (abs(random()) % 4), 1) ||
                              substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))),
    name                  TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 120),
    kind                  TEXT NOT NULL DEFAULT 'other' CHECK (kind IN (
                              'rideshare', 'delivery', 'freelance', 'marketplace',
                              'task', 'direct_client', 'other')),
    default_currency      TEXT NOT NULL DEFAULT 'USD'
                              CHECK (length(default_currency) = 3 AND default_currency = upper(default_currency)),
    payout_schedule       TEXT CHECK (payout_schedule IN (
                              'instant', 'daily', 'weekly', 'biweekly', 'monthly', 'per_job', 'other')),
    is_active             INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    notes                 TEXT,

    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at            TEXT,
    version               INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    sync_status           TEXT NOT NULL DEFAULT 'pending' CHECK (sync_status IN ('pending', 'synced', 'conflict')),
    last_synced_at        TEXT,
    last_modified_device  TEXT,

    CHECK (length(id) = 36 AND id = lower(id))
);

-- Names unique among live rows (case-insensitive); a deleted "Uber" can be re-added.
CREATE UNIQUE INDEX ux_platforms_name_live ON platforms (name COLLATE NOCASE) WHERE deleted_at IS NULL;
CREATE INDEX ix_platforms_sync_pending ON platforms (updated_at) WHERE sync_status <> 'synced';


-- -----------------------------------------------------------------------------
-- tax_profiles: per-year, per-jurisdiction tax assumptions
-- -----------------------------------------------------------------------------
CREATE TABLE tax_profiles (
    id                    TEXT PRIMARY KEY NOT NULL DEFAULT (
                              lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
                              substr(lower(hex(randomblob(2))), 2) || '-' ||
                              substr('89ab', 1 + (abs(random()) % 4), 1) ||
                              substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))),
    tax_year              INTEGER NOT NULL CHECK (tax_year BETWEEN 2000 AND 2100),
    country_code          TEXT NOT NULL CHECK (length(country_code) = 2 AND country_code = upper(country_code)),  -- ISO 3166-1 alpha-2
    region_code           TEXT CHECK (region_code IS NULL OR region_code = upper(region_code)),                  -- ISO 3166-2, e.g. 'US-CA'
    filing_status         TEXT NOT NULL DEFAULT 'single' CHECK (filing_status IN (
                              'single', 'married_joint', 'married_separate',
                              'head_of_household', 'qualifying_surviving_spouse', 'other')),
    currency              TEXT NOT NULL DEFAULT 'USD'
                              CHECK (length(currency) = 3 AND currency = upper(currency)),
    income_tax_rate_bp    INTEGER NOT NULL DEFAULT 0 CHECK (income_tax_rate_bp BETWEEN 0 AND 10000),
    self_employment_rate_bp INTEGER NOT NULL DEFAULT 0 CHECK (self_employment_rate_bp BETWEEN 0 AND 10000),
    -- Standard mileage deduction rate, in minor units of `currency` per unit.
    mileage_rate_minor    INTEGER CHECK (mileage_rate_minor IS NULL OR mileage_rate_minor >= 0),
    mileage_rate_unit     TEXT CHECK (mileage_rate_unit IN ('mi', 'km')),
    quarterly_estimates   INTEGER NOT NULL DEFAULT 1 CHECK (quarterly_estimates IN (0, 1)),
    is_default            INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    notes                 TEXT,

    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at            TEXT,
    version               INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    sync_status           TEXT NOT NULL DEFAULT 'pending' CHECK (sync_status IN ('pending', 'synced', 'conflict')),
    last_synced_at        TEXT,
    last_modified_device  TEXT,

    CHECK (length(id) = 36 AND id = lower(id)),
    -- Rate and unit are set together or not at all.
    CHECK ((mileage_rate_minor IS NULL) = (mileage_rate_unit IS NULL))
);

-- One live profile per year + jurisdiction.
CREATE UNIQUE INDEX ux_tax_profiles_year_jurisdiction
    ON tax_profiles (tax_year, country_code, ifnull(region_code, ''))
    WHERE deleted_at IS NULL;
-- At most one default profile per year.
CREATE UNIQUE INDEX ux_tax_profiles_default_per_year
    ON tax_profiles (tax_year)
    WHERE is_default = 1 AND deleted_at IS NULL;
CREATE INDEX ix_tax_profiles_sync_pending ON tax_profiles (updated_at) WHERE sync_status <> 'synced';


-- -----------------------------------------------------------------------------
-- transactions: income events (earnings, tips, bonuses, adjustments)
-- -----------------------------------------------------------------------------
CREATE TABLE transactions (
    id                    TEXT PRIMARY KEY NOT NULL DEFAULT (
                              lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
                              substr(lower(hex(randomblob(2))), 2) || '-' ||
                              substr('89ab', 1 + (abs(random()) % 4), 1) ||
                              substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))),
    platform_id           TEXT NOT NULL REFERENCES platforms (id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    kind                  TEXT NOT NULL DEFAULT 'earning' CHECK (kind IN (
                              'earning', 'tip', 'bonus', 'incentive',
                              'reimbursement', 'adjustment', 'refund')),
    -- Gross may be negative for adjustments/refunds; fees are never negative.
    gross_amount_minor    INTEGER NOT NULL CHECK (typeof(gross_amount_minor) = 'integer'),
    platform_fee_minor    INTEGER NOT NULL DEFAULT 0
                              CHECK (typeof(platform_fee_minor) = 'integer' AND platform_fee_minor >= 0),
    net_amount_minor      INTEGER GENERATED ALWAYS AS (gross_amount_minor - platform_fee_minor) VIRTUAL,
    currency              TEXT NOT NULL CHECK (length(currency) = 3 AND currency = upper(currency)),
    base_amount_minor     INTEGER CHECK (base_amount_minor IS NULL OR typeof(base_amount_minor) = 'integer'),
    occurred_at           TEXT NOT NULL CHECK (datetime(occurred_at) IS NOT NULL),
    paid_out_at           TEXT CHECK (paid_out_at IS NULL OR datetime(paid_out_at) IS NOT NULL),
    external_ref          TEXT,   -- platform's trip/order/invoice id; dedupes CSV imports
    description           TEXT,

    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at            TEXT,
    version               INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    sync_status           TEXT NOT NULL DEFAULT 'pending' CHECK (sync_status IN ('pending', 'synced', 'conflict')),
    last_synced_at        TEXT,
    last_modified_device  TEXT,

    CHECK (length(id) = 36 AND id = lower(id))
);

CREATE INDEX ix_transactions_platform ON transactions (platform_id);
CREATE INDEX ix_transactions_occurred ON transactions (occurred_at) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX ux_transactions_external_ref
    ON transactions (platform_id, external_ref)
    WHERE external_ref IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX ix_transactions_sync_pending ON transactions (updated_at) WHERE sync_status <> 'synced';


-- -----------------------------------------------------------------------------
-- expenses: business costs (optionally attributed to a platform)
-- -----------------------------------------------------------------------------
CREATE TABLE expenses (
    id                    TEXT PRIMARY KEY NOT NULL DEFAULT (
                              lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
                              substr(lower(hex(randomblob(2))), 2) || '-' ||
                              substr('89ab', 1 + (abs(random()) % 4), 1) ||
                              substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))),
    platform_id           TEXT REFERENCES platforms (id) ON UPDATE RESTRICT ON DELETE SET NULL,
    category              TEXT NOT NULL DEFAULT 'other' CHECK (category IN (
                              'vehicle', 'fuel', 'maintenance', 'insurance', 'parking_tolls',
                              'phone', 'internet', 'equipment', 'supplies', 'software',
                              'platform_fees', 'meals', 'travel', 'home_office',
                              'professional_services', 'education', 'other')),
    amount_minor          INTEGER NOT NULL CHECK (typeof(amount_minor) = 'integer' AND amount_minor >= 0),
    currency              TEXT NOT NULL CHECK (length(currency) = 3 AND currency = upper(currency)),
    base_amount_minor     INTEGER CHECK (base_amount_minor IS NULL OR typeof(base_amount_minor) = 'integer'),
    -- Share of the expense used for business (e.g. phone at 60% = 6000).
    business_use_bp       INTEGER NOT NULL DEFAULT 10000 CHECK (business_use_bp BETWEEN 0 AND 10000),
    is_tax_deductible     INTEGER NOT NULL DEFAULT 1 CHECK (is_tax_deductible IN (0, 1)),
    occurred_at           TEXT NOT NULL CHECK (datetime(occurred_at) IS NOT NULL),
    vendor                TEXT,
    description           TEXT,
    payment_method        TEXT,
    receipt_uri           TEXT,   -- local encrypted-blob reference; binary never stored inline

    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at            TEXT,
    version               INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    sync_status           TEXT NOT NULL DEFAULT 'pending' CHECK (sync_status IN ('pending', 'synced', 'conflict')),
    last_synced_at        TEXT,
    last_modified_device  TEXT,

    CHECK (length(id) = 36 AND id = lower(id))
);

CREATE INDEX ix_expenses_platform ON expenses (platform_id);
CREATE INDEX ix_expenses_occurred ON expenses (occurred_at) WHERE deleted_at IS NULL;
CREATE INDEX ix_expenses_category ON expenses (category, occurred_at) WHERE deleted_at IS NULL;
CREATE INDEX ix_expenses_sync_pending ON expenses (updated_at) WHERE sync_status <> 'synced';


-- -----------------------------------------------------------------------------
-- mileage_logs: trips (IRS-style contemporaneous log: date, distance, purpose)
-- -----------------------------------------------------------------------------
CREATE TABLE mileage_logs (
    id                    TEXT PRIMARY KEY NOT NULL DEFAULT (
                              lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
                              substr(lower(hex(randomblob(2))), 2) || '-' ||
                              substr('89ab', 1 + (abs(random()) % 4), 1) ||
                              substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))),
    platform_id           TEXT REFERENCES platforms (id) ON UPDATE RESTRICT ON DELETE SET NULL,
    started_at            TEXT NOT NULL CHECK (datetime(started_at) IS NOT NULL),
    ended_at              TEXT CHECK (ended_at IS NULL OR datetime(ended_at) IS NOT NULL),
    distance_m            INTEGER NOT NULL CHECK (typeof(distance_m) = 'integer' AND distance_m >= 0),
    odometer_start_m      INTEGER CHECK (odometer_start_m IS NULL OR odometer_start_m >= 0),
    odometer_end_m        INTEGER CHECK (odometer_end_m IS NULL OR odometer_end_m >= 0),
    purpose               TEXT NOT NULL DEFAULT 'business' CHECK (purpose IN ('business', 'commute', 'personal')),
    description           TEXT,   -- business reason, required for audit defensibility
    start_location        TEXT,
    end_location          TEXT,
    vehicle_label         TEXT,
    source                TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'gps', 'import')),

    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at            TEXT,
    version               INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    sync_status           TEXT NOT NULL DEFAULT 'pending' CHECK (sync_status IN ('pending', 'synced', 'conflict')),
    last_synced_at        TEXT,
    last_modified_device  TEXT,

    CHECK (length(id) = 36 AND id = lower(id)),
    CHECK (ended_at IS NULL OR julianday(ended_at) >= julianday(started_at)),
    CHECK (odometer_start_m IS NULL OR odometer_end_m IS NULL OR odometer_end_m >= odometer_start_m)
);

CREATE INDEX ix_mileage_platform ON mileage_logs (platform_id);
CREATE INDEX ix_mileage_started ON mileage_logs (started_at) WHERE deleted_at IS NULL;
CREATE INDEX ix_mileage_sync_pending ON mileage_logs (updated_at) WHERE sync_status <> 'synced';


-- -----------------------------------------------------------------------------
-- sync_meta: device-local sync bookkeeping (NOT synced itself)
--   keys: 'device_id', 'last_pull_cursor', 'last_push_at', ...
-- -----------------------------------------------------------------------------
CREATE TABLE sync_meta (
    key         TEXT PRIMARY KEY NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);


-- =============================================================================
-- Triggers
-- =============================================================================

-- ---- platforms --------------------------------------------------------------
CREATE TRIGGER trg_platforms_immutable BEFORE UPDATE OF id, created_at ON platforms
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'platforms: id and created_at are immutable'); END;

CREATE TRIGGER trg_platforms_touch AFTER UPDATE ON platforms
WHEN NEW.updated_at IS OLD.updated_at AND NEW.sync_status IS OLD.sync_status
BEGIN
    UPDATE platforms
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
           version = OLD.version + 1,
           sync_status = 'pending'
     WHERE id = NEW.id;
END;

-- ---- tax_profiles -----------------------------------------------------------
CREATE TRIGGER trg_tax_profiles_immutable BEFORE UPDATE OF id, created_at ON tax_profiles
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'tax_profiles: id and created_at are immutable'); END;

CREATE TRIGGER trg_tax_profiles_touch AFTER UPDATE ON tax_profiles
WHEN NEW.updated_at IS OLD.updated_at AND NEW.sync_status IS OLD.sync_status
BEGIN
    UPDATE tax_profiles
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
           version = OLD.version + 1,
           sync_status = 'pending'
     WHERE id = NEW.id;
END;

-- ---- transactions -----------------------------------------------------------
CREATE TRIGGER trg_transactions_immutable BEFORE UPDATE OF id, created_at ON transactions
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'transactions: id and created_at are immutable'); END;

CREATE TRIGGER trg_transactions_touch AFTER UPDATE ON transactions
WHEN NEW.updated_at IS OLD.updated_at AND NEW.sync_status IS OLD.sync_status
BEGIN
    UPDATE transactions
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
           version = OLD.version + 1,
           sync_status = 'pending'
     WHERE id = NEW.id;
END;

-- ---- expenses ---------------------------------------------------------------
CREATE TRIGGER trg_expenses_immutable BEFORE UPDATE OF id, created_at ON expenses
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'expenses: id and created_at are immutable'); END;

CREATE TRIGGER trg_expenses_touch AFTER UPDATE ON expenses
WHEN NEW.updated_at IS OLD.updated_at AND NEW.sync_status IS OLD.sync_status
BEGIN
    UPDATE expenses
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
           version = OLD.version + 1,
           sync_status = 'pending'
     WHERE id = NEW.id;
END;

-- ---- mileage_logs -----------------------------------------------------------
CREATE TRIGGER trg_mileage_logs_immutable BEFORE UPDATE OF id, created_at ON mileage_logs
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'mileage_logs: id and created_at are immutable'); END;

CREATE TRIGGER trg_mileage_logs_touch AFTER UPDATE ON mileage_logs
WHEN NEW.updated_at IS OLD.updated_at AND NEW.sync_status IS OLD.sync_status
BEGIN
    UPDATE mileage_logs
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
           version = OLD.version + 1,
           sync_status = 'pending'
     WHERE id = NEW.id;
END;
