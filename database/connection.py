"""
SQLite / SQLCipher connection manager.

Responsibilities
----------------
* **Driver selection** — prefer SQLCipher (`sqlcipher3`, then legacy
  `pysqlcipher3`) for AES-256 encryption at rest; fall back to the stdlib
  `sqlite3` only when encryption is not required *and* no key was supplied.
  A supplied key is never silently ignored.
* **Per-connection hardening** — key first, then foreign keys ON (verified),
  WAL journaling, FULL sync, recursive triggers OFF (required by the schema's
  auto-touch triggers), in-memory temp store, secure delete.
* **Pooling** — a small bounded pool. SQLite connections are cheap, but key
  derivation under SQLCipher is deliberately slow (PBKDF2), so reusing
  connections matters. Each connection is used by one thread at a time; a
  thread that re-enters `connection()` gets the same connection back, so
  nested helpers never deadlock the pool.
* **Transactions** — explicit `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`, with
  SAVEPOINTs for nesting.
* **Automatic initialization** — `initialize()` creates the data directory,
  opens the DB, and applies pending migrations.

Usage
-----
    from core.config import load_config
    from core.security import resolve_db_key
    from database.connection import Database

    cfg = load_config()
    db = Database.from_config(cfg, key=resolve_db_key())
    db.initialize()

    with db.transaction() as conn:
        conn.execute("INSERT INTO platforms (id, name) VALUES (?, ?)", (uuid, "Uber"))

    with db.connection() as conn:
        rows = conn.execute("SELECT * FROM platforms").fetchall()
"""

from __future__ import annotations

import importlib
import logging
import queue
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Iterator, Literal

from database import migrations as _migrations

if TYPE_CHECKING:  # pragma: no cover
    from core.config import AppConfig

log = logging.getLogger(__name__)

DriverChoice = Literal["auto", "sqlcipher", "sqlite"]
MEMORY = ":memory:"

# Import names tried, in order, for an SQLCipher-enabled DB-API module.
_SQLCIPHER_MODULES = ("sqlcipher3", "pysqlcipher3.dbapi2")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DatabaseError(RuntimeError):
    """Base class for connection-manager errors."""


class EncryptionUnavailableError(DatabaseError):
    """Encryption was required (or a key supplied) but can't be provided."""


class InvalidKeyError(DatabaseError):
    """The key does not decrypt the database (or the DB is not encrypted)."""


class PoolTimeoutError(DatabaseError):
    """No connection became available within the timeout."""


class DatabaseClosedError(DatabaseError):
    """The Database was closed."""


# ---------------------------------------------------------------------------
# Driver loading
# ---------------------------------------------------------------------------


def load_sqlcipher_driver() -> ModuleType | None:
    """Return the first importable SQLCipher DB-API module, or None."""
    for name in _SQLCIPHER_MODULES:
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


def _key_pragma(key: str) -> str:
    """Build the PRAGMA key statement.

    PRAGMA arguments can't be bound as parameters, so the key is embedded as a
    SQL string literal with single quotes doubled. A 64-char hex key is passed
    in SQLCipher's raw-key form (x'...'), which bypasses PBKDF2.
    """
    from core.security import is_raw_key

    if is_raw_key(key):
        return f"PRAGMA key = \"x'{key.lower()}'\""
    escaped = key.replace("'", "''")
    return f"PRAGMA key = '{escaped}'"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


class Database:
    """Pooled, hardened access to the local (optionally encrypted) database."""

    def __init__(
        self,
        path: str | Path,
        *,
        key: str | None = None,
        require_encryption: bool = False,
        pool_size: int = 4,
        timeout: float = 5.0,
        driver: DriverChoice = "auto",
        platform: str | None = None,
    ) -> None:
        if pool_size < 1:
            raise ValueError("pool_size must be >= 1")

        self._path = str(path) if str(path) == MEMORY else str(Path(path).expanduser())
        self._timeout = timeout
        self._key = key

        if key is not None:
            from core.security import validate_key

            validate_key(key)

        from core.platform import detect_platform, is_mobile

        self._platform = platform or detect_platform()
        self._mobile = is_mobile(self._platform)
        self._web = self._platform == "web"
        self.after_commit: list = []         # callbacks after an outermost COMMIT (the web vault saves here)
        if self._web and driver == "auto":
            # Browser build: SQLite lives in Pyodide's in-memory filesystem; encryption at rest is the
            # vault's job (core/web/vault.py: AES-256-GCM snapshots under the user's passphrase).
            self._key = None
            self._driver, self._encrypted = sqlite3, False
        elif self._mobile and driver == "auto":
            self._driver, self._encrypted = self._select_mobile_driver(key)
        else:
            # Desktop / server: Phase 1 rules, unchanged — SQLCipher is mandatory
            # when required, and a supplied key is never ignored.
            self._driver, self._encrypted = self._select_driver(driver, key, require_encryption)

        # An in-memory DB is private to one connection, so it can't be pooled.
        self._is_memory = self._path == MEMORY
        self._pool_size = 1 if self._is_memory else pool_size

        self._idle: queue.LifoQueue[Any] = queue.LifoQueue()
        self._slots = threading.BoundedSemaphore(self._pool_size)
        self._all: set[Any] = set()          # every open connection (for close())
        self._lock = threading.Lock()
        self._local = threading.local()      # per-thread re-entrancy tracking
        self._closed = False

    # -- construction -----------------------------------------------------

    @classmethod
    def from_config(cls, config: "AppConfig", *, key: str | None = None) -> "Database":
        return cls(
            config.db_path,
            key=key,
            require_encryption=config.require_encryption,
            pool_size=config.db_pool_size,
            platform=config.platform,
        )

    def _select_mobile_driver(self, key: str | None) -> tuple[ModuleType, bool]:
        """Android / iOS: SQLCipher when a driver is bundled, else platform-protected SQLite.

        The device key (from the Keychain / Android Keystore) is applied to the
        database only when SQLCipher is present; otherwise the caller uses it
        for field sealing. Never raises for a missing driver — see
        core.security for the mobile protection model.
        """
        cipher = load_sqlcipher_driver()
        if cipher is not None and key is not None:
            return cipher, True
        if key is not None:
            log.info("SQLCipher not bundled on %s: using platform-protected SQLite "
                     "(device key reserved for field sealing)", self._platform)
            self._key = None
        else:
            log.info("Opening platform-protected SQLite on %s", self._platform)
        return sqlite3, False

    @staticmethod
    def _select_driver(
        choice: DriverChoice, key: str | None, require_encryption: bool
    ) -> tuple[ModuleType, bool]:
        if choice not in ("auto", "sqlcipher", "sqlite"):
            raise ValueError(f"Unknown driver '{choice}'")

        if choice == "sqlite":
            if require_encryption:
                raise EncryptionUnavailableError("driver='sqlite' cannot satisfy require_encryption=True")
            if key is not None:
                raise EncryptionUnavailableError("A key was supplied but driver='sqlite' cannot encrypt")
            log.warning("Opening database WITHOUT encryption (driver='sqlite')")
            return sqlite3, False

        cipher = load_sqlcipher_driver()
        if cipher is None and (choice == "sqlcipher" or require_encryption or key is not None):
            raise EncryptionUnavailableError(
                "SQLCipher driver not installed. Install 'sqlcipher3-binary' (Linux) "
                "or 'sqlcipher3' (macOS/Windows with system SQLCipher)."
            )
        if key is None:
            if require_encryption or choice == "sqlcipher":
                raise EncryptionUnavailableError("Encryption required but no database key was provided")
            log.warning("No database key supplied — opening database WITHOUT encryption (dev mode)")
            return sqlite3, False

        assert cipher is not None
        return cipher, True

    # -- properties -------------------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    @property
    def encrypted(self) -> bool:
        return self._encrypted

    @property
    def platform(self) -> str:
        return self._platform

    @property
    def protection(self) -> str:
        """'sqlcipher' | 'platform' (mobile sandbox + OS encryption) | 'none' (desktop dev)."""
        if self._encrypted:
            return "sqlcipher"
        if self._web:
            return "vault"
        return "platform" if self._mobile else "none"

    @property
    def driver_name(self) -> str:
        return self._driver.__name__

    # -- raw connection lifecycle -----------------------------------------

    def _open(self) -> Any:
        """Open and configure a brand-new connection."""
        if not self._is_memory:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            if self.protection != "sqlcipher":
                self._restrict_permissions()

        conn = self._driver.connect(
            self._path,
            timeout=self._timeout,          # also sets SQLite's busy timeout
            isolation_level=None,           # autocommit; we manage transactions
            check_same_thread=False,        # pool hands connections across threads
        )
        try:
            self._configure(conn)
        except BaseException:
            conn.close()
            raise
        conn.row_factory = self._driver.Row
        return conn

    def _configure(self, conn: Any) -> None:
        driver_error = self._driver.DatabaseError

        # 1. Key MUST be the first statement on an SQLCipher connection.
        if self._encrypted:
            assert self._key is not None
            conn.execute(_key_pragma(self._key))
            row = conn.execute("PRAGMA cipher_version").fetchone()
            if not row or not row[0]:
                raise EncryptionUnavailableError(
                    f"Driver '{self.driver_name}' loaded but SQLCipher is not active"
                )

        # 2. Touch the schema: this is where a wrong key / wrong mode surfaces.
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except driver_error as exc:
            if "not a database" in str(exc).lower():
                if self._encrypted:
                    raise InvalidKeyError(
                        "Database key is incorrect, or the file is not an SQLCipher database"
                    ) from exc
                raise InvalidKeyError(
                    "File is not a plain SQLite database (is it encrypted? supply the key)"
                ) from exc
            raise

        # 3. Integrity & safety pragmas.
        conn.execute("PRAGMA foreign_keys = ON")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise DatabaseError("This SQLite build does not support foreign key enforcement")

        conn.execute("PRAGMA recursive_triggers = OFF")   # schema's touch triggers depend on this
        conn.execute("PRAGMA temp_store = MEMORY")         # no plaintext temp files on disk
        conn.execute("PRAGMA secure_delete = ON")          # zero freed pages
        if self._web:
            # No WAL in the browser: after each commit the file itself is complete, so the vault can
            # snapshot it (WAL needs shared memory the in-memory filesystem doesn't provide).
            conn.execute("PRAGMA journal_mode = DELETE")
        elif not self._is_memory:
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                log.warning("WAL journal mode unavailable (got %s); continuing", mode)
        # FULL: every commit is durable across power loss — worth it for money data.
        conn.execute("PRAGMA synchronous = FULL")

    def _restrict_permissions(self) -> None:
        """Owner-only file mode for unencrypted database files (defence in depth;
        mobile sandboxes already isolate apps, and Windows ignores POSIX modes)."""
        import os

        path = Path(self._path)
        if not path.exists():
            path.touch(mode=0o600)
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            try:
                if candidate.exists():
                    os.chmod(candidate, 0o600)
            except OSError:
                log.debug("Could not restrict permissions on %s", candidate)

    @staticmethod
    def _discard(conn: Any) -> None:
        try:
            conn.close()
        except Exception:  # pragma: no cover - best effort
            log.debug("Error closing discarded connection", exc_info=True)

    # -- pool -------------------------------------------------------------

    def _checkout(self) -> Any:
        if self._closed:
            raise DatabaseClosedError("Database is closed")
        if not self._slots.acquire(timeout=self._timeout):
            raise PoolTimeoutError(f"No database connection available within {self._timeout}s")
        try:
            try:
                return self._idle.get_nowait()
            except queue.Empty:
                conn = self._open()
                with self._lock:
                    self._all.add(conn)
                return conn
        except BaseException:
            self._slots.release()
            raise

    def _checkin(self, conn: Any) -> None:
        try:
            healthy = True
            if conn.in_transaction:
                # A caller leaked an open transaction; never hand it to someone else.
                log.warning("Connection returned with open transaction; rolling back")
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    healthy = False
            if self._closed or not healthy:
                with self._lock:
                    self._all.discard(conn)
                self._discard(conn)
            else:
                self._idle.put(conn)
        finally:
            self._slots.release()

    @contextmanager
    def connection(self) -> Iterator[Any]:
        """Borrow a connection. Re-entrant within the same thread."""
        held = getattr(self._local, "conn", None)
        if held is not None:
            self._local.depth += 1
            try:
                yield held
            finally:
                self._local.depth -= 1
            return

        conn = self._checkout()
        self._local.conn, self._local.depth = conn, 1
        try:
            yield conn
        finally:
            self._local.conn, self._local.depth = None, 0
            self._checkin(conn)

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[Any]:
        """Run a block atomically.

        Outermost call: BEGIN IMMEDIATE (takes the write lock up front, which
        avoids SQLITE_BUSY deadlocks when a read later upgrades to a write).
        Nested call: a SAVEPOINT, so an inner failure rolls back only the
        inner block if the caller catches it.
        """
        with self.connection() as conn:
            if conn.in_transaction:
                name = f"sp_{uuid.uuid4().hex}"
                conn.execute(f"SAVEPOINT {name}")
                try:
                    yield conn
                except BaseException:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                    conn.execute(f"RELEASE SAVEPOINT {name}")
                    raise
                else:
                    conn.execute(f"RELEASE SAVEPOINT {name}")
                return

            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN DEFERRED")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
                for callback in list(self.after_commit):
                    try:
                        callback()
                    except Exception:                        # a failed save must not undo the commit
                        log.exception("after-commit callback failed")

    # -- lifecycle --------------------------------------------------------

    def initialize(self) -> int:
        """Create the database if needed and apply pending migrations.

        Safe to call on every app start. Returns the schema version.
        """
        with self.connection() as conn:
            version = _migrations.migrate(conn, self._driver.complete_statement)
        log.info(
            "Database ready: %s (schema v%d, %s)",
            self._path, version, "encrypted" if self._encrypted else "UNENCRYPTED",
        )
        return version

    def schema_version(self) -> int:
        with self.connection() as conn:
            return _migrations.current_version(conn)

    def close(self) -> None:
        """Close idle connections now; in-use ones close when returned."""
        self._closed = True
        while True:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                self._all.discard(conn)
            self._discard(conn)

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"Database(path={self._path!r}, driver={self.driver_name}, "
            f"encrypted={self._encrypted}, protection={self.protection}, pool_size={self._pool_size})"
        )
