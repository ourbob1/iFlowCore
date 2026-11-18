"""Connection manager: driver selection, encryption, pooling, transactions, migrations."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from core.config import load_config
from database import migrations
from database.connection import (
    Database,
    DatabaseClosedError,
    EncryptionUnavailableError,
    InvalidKeyError,
    PoolTimeoutError,
)
from database.migrations import Migration, MigrationError, discover_migrations

from .conftest import TEST_KEY, requires_sqlcipher

LATEST = len(discover_migrations())

EXPECTED_TABLES = {"platforms", "transactions", "expenses", "mileage_logs", "tax_profiles", "sync_meta",
                   "import_mappings", "receipts", "vehicle_decode_cache", "vehicles",
                   "vehicle_assignments", "fuel_logs", "maintenance_schedules", "maintenance_records"}


# --- initialization -----------------------------------------------------------


def test_initialize_creates_schema(db):
    with db.connection() as conn:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= tables
    assert db.schema_version() == LATEST


def test_initialize_is_idempotent(db):
    assert db.initialize() == LATEST
    assert db.initialize() == LATEST


def test_initialize_creates_missing_directories(tmp_path):
    path = tmp_path / "a" / "b" / "c.db"
    with Database(path, driver="sqlite") as database:
        database.initialize()
    assert path.exists()


def test_pragmas_applied(db):
    with db.connection() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_rows_are_mapping_like(db, platform_id):
    with db.connection() as conn:
        row = conn.execute("SELECT id, name FROM platforms").fetchone()
    assert row["name"] == "Uber" and row["id"] == platform_id


def test_newer_db_than_app_is_refused(tmp_path):
    database = Database(tmp_path / "x.db", driver="sqlite")
    database.initialize()
    with database.connection() as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(MigrationError, match="newer"):
        database.initialize()


def test_from_config(tmp_path):
    cfg = load_config({"IFLOW_DATA_DIR": str(tmp_path)})
    database = Database.from_config(cfg)
    assert Path(database.path) == tmp_path / "iflowcore.db"
    assert database.encrypted is False


# --- driver selection / encryption --------------------------------------------


def test_sqlite_driver_rejects_key(tmp_path):
    with pytest.raises(EncryptionUnavailableError):
        Database(tmp_path / "x.db", key=TEST_KEY, driver="sqlite")


def test_sqlite_driver_rejects_required_encryption(tmp_path):
    with pytest.raises(EncryptionUnavailableError):
        Database(tmp_path / "x.db", require_encryption=True, driver="sqlite")


def test_required_encryption_without_key_fails(tmp_path):
    with pytest.raises(EncryptionUnavailableError):
        Database(tmp_path / "x.db", require_encryption=True)


def test_key_never_silently_ignored_without_driver(tmp_path, monkeypatch):
    monkeypatch.setattr("database.connection.load_sqlcipher_driver", lambda: None)
    with pytest.raises(EncryptionUnavailableError, match="not installed"):
        Database(tmp_path / "x.db", key=TEST_KEY)


def test_auto_without_key_is_plaintext(tmp_path):
    database = Database(tmp_path / "x.db")
    assert database.encrypted is False and database.driver_name == "sqlite3"


@requires_sqlcipher
def test_encrypted_file_is_unreadable_without_key(tmp_path):
    path = tmp_path / "secret.db"
    with Database(path, key=TEST_KEY) as enc:
        assert enc.encrypted
        enc.initialize()
        with enc.transaction() as conn:
            conn.execute("INSERT INTO platforms (name) VALUES ('Secret Gig Co')")

    raw = path.read_bytes()
    assert not raw.startswith(b"SQLite format 3")
    assert b"Secret Gig Co" not in raw

    # Stdlib sqlite3 can't read it...
    plain = sqlite3.connect(path)
    with pytest.raises(sqlite3.DatabaseError):
        plain.execute("SELECT * FROM sqlite_master").fetchall()
    plain.close()

    # ...and our manager gives a clear error for plaintext mode.
    with pytest.raises(InvalidKeyError, match="encrypted"):
        with Database(path, driver="sqlite").connection():
            pass


@requires_sqlcipher
def test_wrong_key_rejected(tmp_path):
    path = tmp_path / "secret.db"
    with Database(path, key=TEST_KEY) as enc:
        enc.initialize()
    with pytest.raises(InvalidKeyError, match="incorrect"):
        with Database(path, key="definitely the wrong key").connection():
            pass


@requires_sqlcipher
def test_raw_hex_key(tmp_path):
    from core.security import generate_raw_key

    key = generate_raw_key()
    path = tmp_path / "raw.db"
    with Database(path, key=key) as enc:
        enc.initialize()
    with Database(path, key=key) as again:
        assert again.schema_version() == LATEST


@requires_sqlcipher
def test_passphrase_with_quotes(tmp_path):
    key = "it's a \"quoted\" passphrase"
    path = tmp_path / "q.db"
    with Database(path, key=key) as enc:
        enc.initialize()
    with Database(path, key=key) as again:
        assert again.schema_version() == LATEST


# --- transactions -------------------------------------------------------------


def test_transaction_commits(db):
    with db.transaction() as conn:
        conn.execute("INSERT INTO platforms (name) VALUES ('Lyft')")
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM platforms").fetchone()[0] == 1


def test_transaction_rolls_back_on_error(db):
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("INSERT INTO platforms (name) VALUES ('Lyft')")
            raise RuntimeError("boom")
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM platforms").fetchone()[0] == 0


def test_nested_transaction_uses_savepoint(db):
    with db.transaction() as conn:
        conn.execute("INSERT INTO platforms (name) VALUES ('Outer')")
        with pytest.raises(ValueError):
            with db.transaction() as inner:
                inner.execute("INSERT INTO platforms (name) VALUES ('Inner')")
                raise ValueError
    with db.connection() as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM platforms")]
    assert names == ["Outer"]


def test_leaked_transaction_is_rolled_back_on_checkin(db):
    with db.connection() as conn:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO platforms (name) VALUES ('Leaked')")
    with db.connection() as conn:
        assert not conn.in_transaction
        assert conn.execute("SELECT count(*) FROM platforms").fetchone()[0] == 0


# --- pooling ------------------------------------------------------------------


def test_connection_is_reentrant_in_same_thread(tmp_path):
    database = Database(tmp_path / "x.db", driver="sqlite", pool_size=1)
    database.initialize()
    with database.connection() as a:
        with database.connection() as b:   # would deadlock with pool_size=1 if not re-entrant
            assert a is b


def test_connections_are_reused(tmp_path):
    database = Database(tmp_path / "x.db", driver="sqlite", pool_size=2)
    with database.connection() as a:
        pass
    with database.connection() as b:
        pass
    assert a is b


def test_pool_timeout(tmp_path):
    database = Database(tmp_path / "x.db", driver="sqlite", pool_size=1, timeout=0.2)
    database.initialize()
    holding, release = threading.Event(), threading.Event()

    def hold():
        with database.connection():
            holding.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    holding.wait(5)
    try:
        with pytest.raises(PoolTimeoutError):
            with database.connection():
                pass
    finally:
        release.set()
        t.join()


def test_concurrent_writers(db):
    errors: list[BaseException] = []

    def writer(n: int):
        try:
            for i in range(20):
                with db.transaction() as conn:
                    conn.execute("INSERT INTO platforms (name) VALUES (?)", (f"p{n}-{i}",))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM platforms").fetchone()[0] == 80


def test_closed_database_refuses_checkout(tmp_path):
    database = Database(tmp_path / "x.db", driver="sqlite")
    database.close()
    with pytest.raises(DatabaseClosedError):
        with database.connection():
            pass


def test_memory_database_uses_single_connection():
    database = Database(":memory:", driver="sqlite", pool_size=8)
    database.initialize()
    assert database.schema_version() == LATEST


# --- migration runner ---------------------------------------------------------


def test_migration_files_are_contiguous():
    found = discover_migrations()
    assert [m.version for m in found] == list(range(1, len(found) + 1))


def test_failed_migration_rolls_back(tmp_path):
    database = Database(tmp_path / "x.db", driver="sqlite")
    base = discover_migrations()
    bad = Migration(len(base) + 1, "broken", "CREATE TABLE ok_table (x INTEGER); THIS IS NOT SQL;")
    with database.connection() as conn:
        migrations.migrate(conn, sqlite3.complete_statement, base)
        with pytest.raises(MigrationError, match="broken"):
            migrations.migrate(conn, sqlite3.complete_statement, [*base, bad])
        assert migrations.current_version(conn) == len(base)
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='ok_table'").fetchone() is None


def test_split_statements_handles_triggers_and_strings():
    sql = """
    -- leading comment
    CREATE TABLE t (x TEXT DEFAULT 'a;b');
    CREATE TRIGGER tr AFTER INSERT ON t BEGIN UPDATE t SET x = 'c;d'; END;
    /* trailing block comment */
    """
    stmts = list(migrations.split_statements(sql, sqlite3.complete_statement))
    assert len(stmts) == 2
    assert stmts[1].startswith("CREATE TRIGGER") and stmts[1].endswith("END;")


def test_split_statements_rejects_unterminated():
    with pytest.raises(MigrationError, match="Unterminated"):
        list(migrations.split_statements("CREATE TABLE t (x)", sqlite3.complete_statement))
