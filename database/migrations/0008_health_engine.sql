-- =============================================================================
-- Migration 0008 — Phase 8: iFlow Health engine.
--   * health_plans become coverage enrollments: one active plan per line
--     (health / dental / life), with carrier, brokerage, coverage tier, status.
--   * premium reserve entries can be generated from an earnings row
--     (automatic fractional withholding), at most once per (transaction, plan).
-- No medical information is stored anywhere in this schema.
-- =============================================================================

ALTER TABLE health_plans ADD COLUMN line TEXT NOT NULL DEFAULT 'health'
    CHECK (line IN ('health', 'dental', 'life'));
ALTER TABLE health_plans ADD COLUMN carrier TEXT CHECK (carrier IS NULL OR length(trim(carrier)) BETWEEN 1 AND 120);
ALTER TABLE health_plans ADD COLUMN broker_id TEXT CHECK (broker_id IS NULL OR length(broker_id) BETWEEN 1 AND 60);
ALTER TABLE health_plans ADD COLUMN offer_id TEXT CHECK (offer_id IS NULL OR length(offer_id) BETWEEN 1 AND 120);
ALTER TABLE health_plans ADD COLUMN coverage_tier TEXT NOT NULL DEFAULT 'self'
    CHECK (coverage_tier IN ('self', 'self_spouse', 'self_children', 'family'));
ALTER TABLE health_plans ADD COLUMN status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('pending', 'active', 'ended'));
ALTER TABLE health_plans ADD COLUMN enrollment_ref TEXT
    CHECK (enrollment_ref IS NULL OR length(enrollment_ref) BETWEEN 1 AND 120);

CREATE INDEX ix_health_plans_line ON health_plans (line, is_active) WHERE deleted_at IS NULL;

ALTER TABLE health_premium_entries ADD COLUMN transaction_id TEXT
    REFERENCES transactions (id) ON UPDATE RESTRICT ON DELETE SET NULL;
ALTER TABLE health_premium_entries ADD COLUMN rate_bp INTEGER
    CHECK (rate_bp IS NULL OR rate_bp BETWEEN 0 AND 10000);
ALTER TABLE health_premium_entries ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'
    CHECK (source IN ('manual', 'auto'));

-- one automatic withholding per earning per plan (idempotent sync)
CREATE UNIQUE INDEX ux_health_entries_txn_plan ON health_premium_entries (transaction_id, plan_id)
    WHERE transaction_id IS NOT NULL AND deleted_at IS NULL;
