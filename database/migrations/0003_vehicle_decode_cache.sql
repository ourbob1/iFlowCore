-- =============================================================================
-- Migration 0003 — device-local cache of raw VIN decoder responses.
--
-- DataOne lookups are billed per decode, so repeated lookups for the same VIN
-- must be served locally. Raw payloads are stored (not parsed models) so an
-- improved parser can re-read old responses without paying again.
-- Not synced: like sync_meta, it has no sync columns.
-- =============================================================================

CREATE TABLE vehicle_decode_cache (
    provider      TEXT NOT NULL CHECK (provider IN ('nhtsa', 'dataone')),
    vin           TEXT NOT NULL CHECK (length(vin) = 17 AND vin = upper(vin)),
    request_key   TEXT NOT NULL DEFAULT '',     -- variant of the query (e.g. data packs, model year)
    payload_json  TEXT NOT NULL CHECK (json_valid(payload_json)),
    fetched_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at    TEXT NOT NULL CHECK (datetime(expires_at) IS NOT NULL),
    PRIMARY KEY (provider, vin, request_key)
);

CREATE INDEX ix_vehicle_decode_cache_expires ON vehicle_decode_cache (expires_at);
