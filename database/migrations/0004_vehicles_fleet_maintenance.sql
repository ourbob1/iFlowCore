-- =============================================================================
-- Migration 0004 — Phase 3: vehicles, fleet assignments, fuel, maintenance,
-- and trip lifecycle columns on mileage_logs.
-- Units: distances in meters, fuel in milliliters, energy in watt-hours,
-- money in minor units, shares in basis points.
-- =============================================================================

-- vehicles: one row per car/bike/scooter the user works with
CREATE TABLE vehicles (
    id                    TEXT PRIMARY KEY NOT NULL DEFAULT (
                              lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
                              substr(lower(hex(randomblob(2))), 2) || '-' ||
                              substr('89ab', 1 + (abs(random()) % 4), 1) ||
                              substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))),

    nickname              TEXT NOT NULL CHECK (length(trim(nickname)) BETWEEN 1 AND 60),
    vin                   TEXT CHECK (vin IS NULL OR (length(vin) = 17 AND vin = upper(vin))),
    vehicle_type          TEXT NOT NULL DEFAULT 'car' CHECK (vehicle_type IN ('car', 'truck', 'van', 'motorcycle', 'scooter', 'bicycle', 'ebike', 'other')),
    year                  INTEGER CHECK (year IS NULL OR year BETWEEN 1900 AND 2100),
    make                  TEXT,
    model                 TEXT,
    trim                  TEXT,
    style_id              TEXT,
    fuel_type             TEXT CHECK (fuel_type IS NULL OR fuel_type IN ('gasoline', 'diesel', 'hybrid', 'plug_in_hybrid', 'electric', 'flex_fuel', 'hydrogen', 'other', 'none')),
    rated_mpg_combined    INTEGER CHECK (rated_mpg_combined IS NULL OR rated_mpg_combined > 0),
    equipment_json        TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(equipment_json)),
    odometer_m            INTEGER NOT NULL DEFAULT 0 CHECK (odometer_m >= 0),
    odometer_updated_at   TEXT,
    baseline_odometer_m   INTEGER NOT NULL DEFAULT 0 CHECK (baseline_odometer_m >= 0),   -- odometer when tracking began
    acquired_on           TEXT CHECK (acquired_on IS NULL OR date(acquired_on) IS NOT NULL),
    placed_in_service_on  TEXT CHECK (placed_in_service_on IS NULL OR date(placed_in_service_on) IS NOT NULL),
    purchase_price_minor  INTEGER CHECK (purchase_price_minor IS NULL OR purchase_price_minor >= 0),
    currency              TEXT NOT NULL DEFAULT 'USD' CHECK (length(currency) = 3 AND currency = upper(currency)),
    business_use_bp       INTEGER NOT NULL DEFAULT 10000 CHECK (business_use_bp BETWEEN 0 AND 10000),
    deduction_method      TEXT NOT NULL DEFAULT 'standard_mileage' CHECK (deduction_method IN ('standard_mileage', 'actual')),
    severe_service        INTEGER NOT NULL DEFAULT 1 CHECK (severe_service IN (0, 1)),
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

CREATE UNIQUE INDEX ux_vehicles_vin_live ON vehicles (vin) WHERE vin IS NOT NULL AND deleted_at IS NULL;
CREATE UNIQUE INDEX ux_vehicles_nickname_live ON vehicles (nickname COLLATE NOCASE) WHERE deleted_at IS NULL;
CREATE INDEX ix_vehicles_sync_pending ON vehicles (updated_at) WHERE sync_status <> 'synced';


-- vehicle_assignments: which vehicle works which platform, over which dates
-- (platform_id NULL = the default vehicle for any platform)
CREATE TABLE vehicle_assignments (
    id                    TEXT PRIMARY KEY NOT NULL DEFAULT (
                              lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
                              substr(lower(hex(randomblob(2))), 2) || '-' ||
                              substr('89ab', 1 + (abs(random()) % 4), 1) ||
                              substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))),

    vehicle_id            TEXT NOT NULL REFERENCES vehicles (id) ON UPDATE RESTRICT ON DELETE CASCADE,
    platform_id           TEXT REFERENCES platforms (id) ON UPDATE RESTRICT ON DELETE CASCADE,
    starts_on             TEXT NOT NULL CHECK (date(starts_on) IS NOT NULL),
    ends_on               TEXT CHECK (ends_on IS NULL OR date(ends_on) IS NOT NULL),

    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at            TEXT,
    version               INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    sync_status           TEXT NOT NULL DEFAULT 'pending' CHECK (sync_status IN ('pending', 'synced', 'conflict')),
    last_synced_at        TEXT,
    last_modified_device  TEXT,

    CHECK (length(id) = 36 AND id = lower(id)),
    CHECK (ends_on IS NULL OR ends_on >= starts_on)
);

CREATE INDEX ix_vehicle_assignments_lookup ON vehicle_assignments (platform_id, starts_on) WHERE deleted_at IS NULL;
CREATE INDEX ix_vehicle_assignments_vehicle ON vehicle_assignments (vehicle_id);
CREATE INDEX ix_vehicle_assignments_sync_pending ON vehicle_assignments (updated_at) WHERE sync_status <> 'synced';


-- fuel_logs: fill-ups (volume_ml) or charging sessions (energy_wh)
CREATE TABLE fuel_logs (
    id                    TEXT PRIMARY KEY NOT NULL DEFAULT (
                              lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
                              substr(lower(hex(randomblob(2))), 2) || '-' ||
                              substr('89ab', 1 + (abs(random()) % 4), 1) ||
                              substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))),

    vehicle_id            TEXT NOT NULL REFERENCES vehicles (id) ON UPDATE RESTRICT ON DELETE CASCADE,
    filled_at             TEXT NOT NULL CHECK (datetime(filled_at) IS NOT NULL),
    odometer_m            INTEGER NOT NULL CHECK (odometer_m >= 0),
    volume_ml             INTEGER CHECK (volume_ml IS NULL OR volume_ml > 0),
    energy_wh             INTEGER CHECK (energy_wh IS NULL OR energy_wh > 0),
    total_cost_minor      INTEGER NOT NULL CHECK (typeof(total_cost_minor) = 'integer' AND total_cost_minor >= 0),
    currency              TEXT NOT NULL DEFAULT 'USD' CHECK (length(currency) = 3 AND currency = upper(currency)),
    is_full               INTEGER NOT NULL DEFAULT 1 CHECK (is_full IN (0, 1)),
    station               TEXT,
    expense_id            TEXT REFERENCES expenses (id) ON UPDATE RESTRICT ON DELETE SET NULL,

    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at            TEXT,
    version               INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    sync_status           TEXT NOT NULL DEFAULT 'pending' CHECK (sync_status IN ('pending', 'synced', 'conflict')),
    last_synced_at        TEXT,
    last_modified_device  TEXT,

    CHECK (length(id) = 36 AND id = lower(id)),
    CHECK ((volume_ml IS NULL) <> (energy_wh IS NULL))
);

CREATE INDEX ix_fuel_logs_vehicle ON fuel_logs (vehicle_id, odometer_m) WHERE deleted_at IS NULL;
CREATE INDEX ix_fuel_logs_sync_pending ON fuel_logs (updated_at) WHERE sync_status <> 'synced';


-- maintenance_schedules: per-vehicle service intervals (from DataOne, user, or generic defaults)
CREATE TABLE maintenance_schedules (
    id                    TEXT PRIMARY KEY NOT NULL DEFAULT (
                              lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
                              substr(lower(hex(randomblob(2))), 2) || '-' ||
                              substr('89ab', 1 + (abs(random()) % 4), 1) ||
                              substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))),

    vehicle_id            TEXT NOT NULL REFERENCES vehicles (id) ON UPDATE RESTRICT ON DELETE CASCADE,
    service               TEXT NOT NULL CHECK (length(trim(service)) > 0),
    category              TEXT NOT NULL DEFAULT 'general',
    interval_meters       INTEGER CHECK (interval_meters IS NULL OR interval_meters > 0),
    interval_months       INTEGER CHECK (interval_months IS NULL OR interval_months > 0),
    severe_interval_meters INTEGER CHECK (severe_interval_meters IS NULL OR severe_interval_meters > 0),
    severe_interval_months INTEGER CHECK (severe_interval_months IS NULL OR severe_interval_months > 0),
    source                TEXT NOT NULL DEFAULT 'user' CHECK (source IN ('dataone', 'user', 'generic')),
    is_active             INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),

    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    deleted_at            TEXT,
    version               INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    sync_status           TEXT NOT NULL DEFAULT 'pending' CHECK (sync_status IN ('pending', 'synced', 'conflict')),
    last_synced_at        TEXT,
    last_modified_device  TEXT,

    CHECK (length(id) = 36 AND id = lower(id)),
    CHECK (interval_meters IS NOT NULL OR interval_months IS NOT NULL)
);

CREATE UNIQUE INDEX ux_maintenance_schedules_service ON maintenance_schedules (vehicle_id, service COLLATE NOCASE) WHERE deleted_at IS NULL;
CREATE INDEX ix_maintenance_schedules_sync_pending ON maintenance_schedules (updated_at) WHERE sync_status <> 'synced';


-- maintenance_records: services actually performed
CREATE TABLE maintenance_records (
    id                    TEXT PRIMARY KEY NOT NULL DEFAULT (
                              lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
                              substr(lower(hex(randomblob(2))), 2) || '-' ||
                              substr('89ab', 1 + (abs(random()) % 4), 1) ||
                              substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))),

    vehicle_id            TEXT NOT NULL REFERENCES vehicles (id) ON UPDATE RESTRICT ON DELETE CASCADE,
    service               TEXT NOT NULL CHECK (length(trim(service)) > 0),
    category              TEXT NOT NULL DEFAULT 'general',
    performed_on          TEXT NOT NULL CHECK (date(performed_on) IS NOT NULL),
    odometer_m            INTEGER NOT NULL CHECK (odometer_m >= 0),
    cost_minor            INTEGER CHECK (cost_minor IS NULL OR (typeof(cost_minor) = 'integer' AND cost_minor >= 0)),
    currency              TEXT NOT NULL DEFAULT 'USD' CHECK (length(currency) = 3 AND currency = upper(currency)),
    vendor                TEXT,
    expense_id            TEXT REFERENCES expenses (id) ON UPDATE RESTRICT ON DELETE SET NULL,
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

CREATE INDEX ix_maintenance_records_vehicle ON maintenance_records (vehicle_id, service, performed_on) WHERE deleted_at IS NULL;
CREATE INDEX ix_maintenance_records_sync_pending ON maintenance_records (updated_at) WHERE sync_status <> 'synced';


-- mileage_logs: link trips to vehicles and support live start/stop trips.
-- trip_state 'active' rows hold distance_m = 0 until stopped.
ALTER TABLE mileage_logs ADD COLUMN vehicle_id TEXT REFERENCES vehicles (id) ON UPDATE RESTRICT ON DELETE SET NULL;
ALTER TABLE mileage_logs ADD COLUMN trip_state TEXT NOT NULL DEFAULT 'completed' CHECK (trip_state IN ('active', 'completed'));
ALTER TABLE mileage_logs ADD COLUMN distance_source TEXT CHECK (distance_source IS NULL OR distance_source IN ('manual', 'odometer', 'gps'));

CREATE INDEX ix_mileage_vehicle ON mileage_logs (vehicle_id, started_at) WHERE deleted_at IS NULL;
-- At most one active trip per vehicle (and one without a vehicle).
CREATE UNIQUE INDEX ux_mileage_one_active_trip ON mileage_logs (ifnull(vehicle_id, ''))
    WHERE trip_state = 'active' AND deleted_at IS NULL;


-- ---- triggers (same pattern as schema.sql) ----------------------------------

CREATE TRIGGER trg_vehicles_immutable BEFORE UPDATE OF id, created_at ON vehicles
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'vehicles: id and created_at are immutable'); END;

CREATE TRIGGER trg_vehicles_touch AFTER UPDATE ON vehicles
WHEN NEW.updated_at IS OLD.updated_at AND NEW.sync_status IS OLD.sync_status
BEGIN
    UPDATE vehicles
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
           version = OLD.version + 1,
           sync_status = 'pending'
     WHERE id = NEW.id;
END;


CREATE TRIGGER trg_vehicle_assignments_immutable BEFORE UPDATE OF id, created_at ON vehicle_assignments
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'vehicle_assignments: id and created_at are immutable'); END;

CREATE TRIGGER trg_vehicle_assignments_touch AFTER UPDATE ON vehicle_assignments
WHEN NEW.updated_at IS OLD.updated_at AND NEW.sync_status IS OLD.sync_status
BEGIN
    UPDATE vehicle_assignments
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
           version = OLD.version + 1,
           sync_status = 'pending'
     WHERE id = NEW.id;
END;


CREATE TRIGGER trg_fuel_logs_immutable BEFORE UPDATE OF id, created_at ON fuel_logs
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'fuel_logs: id and created_at are immutable'); END;

CREATE TRIGGER trg_fuel_logs_touch AFTER UPDATE ON fuel_logs
WHEN NEW.updated_at IS OLD.updated_at AND NEW.sync_status IS OLD.sync_status
BEGIN
    UPDATE fuel_logs
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
           version = OLD.version + 1,
           sync_status = 'pending'
     WHERE id = NEW.id;
END;


CREATE TRIGGER trg_maintenance_schedules_immutable BEFORE UPDATE OF id, created_at ON maintenance_schedules
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'maintenance_schedules: id and created_at are immutable'); END;

CREATE TRIGGER trg_maintenance_schedules_touch AFTER UPDATE ON maintenance_schedules
WHEN NEW.updated_at IS OLD.updated_at AND NEW.sync_status IS OLD.sync_status
BEGIN
    UPDATE maintenance_schedules
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
           version = OLD.version + 1,
           sync_status = 'pending'
     WHERE id = NEW.id;
END;


CREATE TRIGGER trg_maintenance_records_immutable BEFORE UPDATE OF id, created_at ON maintenance_records
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'maintenance_records: id and created_at are immutable'); END;

CREATE TRIGGER trg_maintenance_records_touch AFTER UPDATE ON maintenance_records
WHEN NEW.updated_at IS OLD.updated_at AND NEW.sync_status IS OLD.sync_status
BEGIN
    UPDATE maintenance_records
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
           version = OLD.version + 1,
           sync_status = 'pending'
     WHERE id = NEW.id;
END;
