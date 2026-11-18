"""Schema behavior: constraints, foreign keys, UUIDs, auto-touch triggers, sync flags."""

from __future__ import annotations

import re
import time

import pytest

from .conftest import new_id

UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
SYNCED_TABLES = ["platforms", "transactions", "expenses", "mileage_logs", "tax_profiles"]


def integrity_error(db):
    # Works for both stdlib sqlite3 and sqlcipher3 (distinct exception classes).
    return db._driver.IntegrityError


def insert_txn(conn, platform_id, **overrides):
    values = {
        "id": new_id(), "platform_id": platform_id, "gross_amount_minor": 2500,
        "platform_fee_minor": 500, "currency": "USD", "occurred_at": "2026-09-01T14:00:00Z",
    }
    values.update(overrides)
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    conn.execute(f"INSERT INTO transactions ({cols}) VALUES ({marks})", tuple(values.values()))
    return values["id"]


# --- sync columns present everywhere -----------------------------------------------


@pytest.mark.parametrize("table", SYNCED_TABLES)
def test_sync_columns_exist(db, table):
    with db.connection() as conn:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_xinfo({table})")}
    assert {"id", "created_at", "updated_at", "deleted_at", "version",
            "sync_status", "last_synced_at", "last_modified_device"} <= cols


def test_default_uuid_is_v4(db):
    with db.transaction() as conn:
        conn.execute("INSERT INTO platforms (name) VALUES ('DoorDash')")
        pid = conn.execute("SELECT id FROM platforms").fetchone()[0]
    assert UUID4_RE.match(pid)


def test_malformed_id_rejected(db):
    with pytest.raises(integrity_error(db)):
        with db.transaction() as conn:
            conn.execute("INSERT INTO platforms (id, name) VALUES ('not-a-uuid', 'X')")


def test_new_rows_start_pending_v1(db, platform_id):
    with db.connection() as conn:
        row = conn.execute("SELECT version, sync_status, created_at FROM platforms").fetchone()
    assert row["version"] == 1 and row["sync_status"] == "pending"
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$", row["created_at"])


# --- foreign keys ---------------------------------------------------------------


def test_transaction_requires_existing_platform(db):
    with pytest.raises(integrity_error(db)):
        with db.transaction() as conn:
            insert_txn(conn, new_id())


def test_platform_with_transactions_cannot_be_hard_deleted(db, platform_id):
    with db.transaction() as conn:
        insert_txn(conn, platform_id)
    with pytest.raises(integrity_error(db)):
        with db.transaction() as conn:
            conn.execute("DELETE FROM platforms WHERE id = ?", (platform_id,))


def test_expense_platform_set_null_on_delete(db, platform_id):
    eid = new_id()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO expenses (id, platform_id, category, amount_minor, currency, occurred_at) "
            "VALUES (?, ?, 'fuel', 4210, 'USD', '2026-09-02T09:00:00Z')", (eid, platform_id))
        conn.execute("DELETE FROM platforms WHERE id = ?", (platform_id,))
        row = conn.execute("SELECT platform_id, sync_status FROM expenses WHERE id = ?", (eid,)).fetchone()
    assert row["platform_id"] is None
    assert row["sync_status"] == "pending"


# --- money & domain constraints ---------------------------------------------------


def test_net_amount_is_generated(db, platform_id):
    with db.transaction() as conn:
        tid = insert_txn(conn, platform_id, gross_amount_minor=3000, platform_fee_minor=750)
        net = conn.execute("SELECT net_amount_minor FROM transactions WHERE id = ?", (tid,)).fetchone()[0]
    assert net == 2250


@pytest.mark.parametrize("overrides", [
    {"gross_amount_minor": 12.5},             # REAL money is rejected
    {"platform_fee_minor": -1},
    {"currency": "usd"},
    {"currency": "US"},
    {"occurred_at": "yesterday"},
    {"kind": "salary"},
])
def test_transaction_constraints(db, platform_id, overrides):
    with pytest.raises(integrity_error(db)):
        with db.transaction() as conn:
            insert_txn(conn, platform_id, **overrides)


def test_duplicate_external_ref_rejected_but_allowed_after_soft_delete(db, platform_id):
    with db.transaction() as conn:
        first = insert_txn(conn, platform_id, external_ref="TRIP-1")
    with pytest.raises(integrity_error(db)):
        with db.transaction() as conn:
            insert_txn(conn, platform_id, external_ref="TRIP-1")
    with db.transaction() as conn:
        conn.execute("UPDATE transactions SET deleted_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?", (first,))
        insert_txn(conn, platform_id, external_ref="TRIP-1")


def test_platform_names_unique_case_insensitive(db, platform_id):
    with pytest.raises(integrity_error(db)):
        with db.transaction() as conn:
            conn.execute("INSERT INTO platforms (name) VALUES ('uber')")


def test_expense_business_use_bounds(db):
    with pytest.raises(integrity_error(db)):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO expenses (category, amount_minor, currency, occurred_at, business_use_bp) "
                "VALUES ('phone', 8000, 'USD', '2026-09-01', 10001)")


def test_mileage_constraints(db):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO mileage_logs (started_at, ended_at, distance_m, odometer_start_m, odometer_end_m) "
            "VALUES ('2026-09-01T08:00:00Z', '2026-09-01T09:00:00Z', 16093, 1000000, 1016093)")
    bad_rows = [
        ("2026-09-01T09:00:00Z", "2026-09-01T08:00:00Z", 100, None, None),   # ends before start
        ("2026-09-01T08:00:00Z", None, -5, None, None),                      # negative distance
        ("2026-09-01T08:00:00Z", None, 100, 500, 400),                       # odometer backwards
    ]
    for row in bad_rows:
        with pytest.raises(integrity_error(db)):
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO mileage_logs (started_at, ended_at, distance_m, odometer_start_m, odometer_end_m) "
                    "VALUES (?, ?, ?, ?, ?)", row)


def test_tax_profile_constraints(db):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO tax_profiles (tax_year, country_code, region_code, is_default, "
            "income_tax_rate_bp, self_employment_rate_bp, mileage_rate_minor, mileage_rate_unit) "
            "VALUES (2026, 'US', 'US-NY', 1, 2200, 1530, 70, 'mi')")
    cases = [
        # duplicate year + jurisdiction
        "INSERT INTO tax_profiles (tax_year, country_code, region_code) VALUES (2026, 'US', 'US-NY')",
        # second default for the same year
        "INSERT INTO tax_profiles (tax_year, country_code, region_code, is_default) VALUES (2026, 'US', 'US-NJ', 1)",
        # rate without unit
        "INSERT INTO tax_profiles (tax_year, country_code, mileage_rate_minor) VALUES (2027, 'US', 70)",
        # rate > 100%
        "INSERT INTO tax_profiles (tax_year, country_code, income_tax_rate_bp) VALUES (2027, 'CA', 10001)",
    ]
    for sql in cases:
        with pytest.raises(integrity_error(db)):
            with db.transaction() as conn:
                conn.execute(sql)


# --- auto-touch triggers & sync flags ---------------------------------------------


def _state(conn, pid):
    return conn.execute(
        "SELECT updated_at, version, sync_status FROM platforms WHERE id = ?", (pid,)).fetchone()


def test_app_update_bumps_version_timestamp_and_marks_pending(db, platform_id):
    with db.transaction() as conn:
        conn.execute("UPDATE platforms SET sync_status = 'synced' WHERE id = ?", (platform_id,))
        before = _state(conn, platform_id)
    time.sleep(0.01)
    with db.transaction() as conn:
        conn.execute("UPDATE platforms SET notes = 'weekends only' WHERE id = ?", (platform_id,))
        after = _state(conn, platform_id)
    assert after["version"] == before["version"] + 1
    assert after["updated_at"] > before["updated_at"]
    assert after["sync_status"] == "pending"


def test_marking_synced_does_not_bump_version(db, platform_id):
    with db.transaction() as conn:
        conn.execute(
            "UPDATE platforms SET sync_status = 'synced', last_synced_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ?", (platform_id,))
        row = _state(conn, platform_id)
    assert row["version"] == 1 and row["sync_status"] == "synced"


def test_applying_server_row_with_explicit_updated_at_is_untouched(db, platform_id):
    server_ts = "2030-01-01T00:00:00.000Z"
    with db.transaction() as conn:
        conn.execute(
            "UPDATE platforms SET name = 'Uber Eats', updated_at = ?, version = 7, sync_status = 'synced' "
            "WHERE id = ?", (server_ts, platform_id))
        row = _state(conn, platform_id)
    assert (row["updated_at"], row["version"], row["sync_status"]) == (server_ts, 7, "synced")


@pytest.mark.parametrize("table", SYNCED_TABLES)
def test_touch_trigger_on_every_table(db, platform_id, table):
    inserts = {
        "platforms": None,
        "transactions": lambda c: insert_txn(c, platform_id),
        "expenses": lambda c: c.execute(
            "INSERT INTO expenses (id, category, amount_minor, currency, occurred_at) "
            "VALUES (?, 'fuel', 100, 'USD', '2026-09-01')", (rid,)),
        "mileage_logs": lambda c: c.execute(
            "INSERT INTO mileage_logs (id, started_at, distance_m) VALUES (?, '2026-09-01', 100)", (rid,)),
        "tax_profiles": lambda c: c.execute(
            "INSERT INTO tax_profiles (id, tax_year, country_code) VALUES (?, 2026, 'US')", (rid,)),
    }
    rid = new_id()
    with db.transaction() as conn:
        if table == "platforms":
            rid = platform_id
        elif table == "transactions":
            rid = inserts[table](conn)
        else:
            inserts[table](conn)
        conn.execute(f"UPDATE {table} SET sync_status = 'synced' WHERE id = ?", (rid,))
        conn.execute(f"UPDATE {table} SET last_modified_device = 'dev-1' WHERE id = ?", (rid,))
        row = conn.execute(f"SELECT version, sync_status FROM {table} WHERE id = ?", (rid,)).fetchone()
    assert (row["version"], row["sync_status"]) == (2, "pending")


@pytest.mark.parametrize("column,value", [("id", "00000000-0000-4000-8000-000000000000"),
                                          ("created_at", "2000-01-01T00:00:00.000Z")])
def test_id_and_created_at_immutable(db, platform_id, column, value):
    with pytest.raises(integrity_error(db)):
        with db.transaction() as conn:
            conn.execute(f"UPDATE platforms SET {column} = ? WHERE id = ?", (value, platform_id))


def test_pending_sync_queue_uses_partial_index(db, platform_id):
    with db.connection() as conn:
        plan = " ".join(str(tuple(r)) for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM platforms WHERE sync_status <> 'synced' ORDER BY updated_at"))
    assert "ix_platforms_sync_pending" in plan
