"""
Schema migration runner.

Versioning uses SQLite's built-in `PRAGMA user_version` (stored in the DB
header, so it is encrypted along with everything else under SQLCipher).

    version 1  = database/schema.sql      (baseline)
    version N  = database/migrations/NNNN_description.sql   (N >= 2)

Each migration runs in its own `BEGIN IMMEDIATE` transaction together with
the `user_version` bump and a `PRAGMA foreign_key_check`, so a migration
either applies completely or not at all.

Table-rebuild migrations (SQLite's 12-step ALTER procedure) need foreign keys
disabled for their duration; put this marker line anywhere in the file:

    -- rubysoho: disable-foreign-keys

The runner then turns FKs off around the transaction and still runs
`foreign_key_check` before committing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).with_name("schema.sql")
MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_DISABLE_FK_MARKER = "-- rubysoho: disable-foreign-keys"   # frozen: used by shipped migrations


class MigrationError(RuntimeError):
    """A migration failed or the migration set is inconsistent."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def disables_foreign_keys(self) -> bool:
        return _DISABLE_FK_MARKER in self.sql


def discover_migrations(
    schema_file: Path = SCHEMA_FILE, migrations_dir: Path = MIGRATIONS_DIR
) -> list[Migration]:
    """Return all migrations in version order, validating contiguity."""
    migrations = [Migration(1, "baseline_schema", schema_file.read_text(encoding="utf-8"))]

    if migrations_dir.is_dir():
        found: list[Migration] = []
        for path in migrations_dir.glob("*.sql"):
            match = _MIGRATION_RE.match(path.name)
            if not match:
                raise MigrationError(f"Bad migration filename '{path.name}' (want NNNN_name.sql)")
            found.append(Migration(int(match.group(1)), match.group(2), path.read_text(encoding="utf-8")))
        found.sort(key=lambda m: m.version)
        migrations.extend(found)

    for expected, migration in enumerate(migrations, start=1):
        if migration.version != expected:
            raise MigrationError(
                f"Migration versions must be contiguous from 1; expected {expected}, "
                f"found {migration.version} ({migration.name})"
            )
    return migrations


def split_statements(sql: str, complete_statement: Any) -> Iterator[str]:
    """Split a SQL script into individual statements.

    Uses the driver's `complete_statement()` (SQLite's own tokenizer) so
    semicolons inside strings, comments, and trigger bodies are handled
    correctly. We avoid `executescript()` because on Python 3.10 it forces a
    COMMIT, which would break our single-transaction guarantee.
    """
    pieces = sql.split(";")
    buffer = ""
    # Every piece but the last was followed by a ';' in the original text.
    for piece in pieces[:-1]:
        buffer += piece + ";"
        if complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if _has_code(statement):
                yield statement
    # Anything left over is whitespace/comments or an unterminated statement.
    leftover = (buffer + pieces[-1]).strip()
    if _has_code(leftover):
        raise MigrationError(f"Unterminated SQL statement near: {leftover[:80]!r}")


def _has_code(statement: str) -> bool:
    """True if the text contains something other than comments / ';'."""
    without_line_comments = re.sub(r"--[^\n]*", "", statement)
    without_block_comments = re.sub(r"/\*.*?\*/", "", without_line_comments, flags=re.S)
    return bool(without_block_comments.replace(";", "").strip())


def current_version(conn: Any) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: Any, complete_statement: Any, migrations: list[Migration] | None = None) -> int:
    """Apply all pending migrations. Returns the resulting schema version.

    `conn` must be in autocommit mode (isolation_level=None).
    """
    migrations = discover_migrations() if migrations is None else migrations
    latest = migrations[-1].version if migrations else 0
    version = current_version(conn)

    if version > latest:
        raise MigrationError(
            f"Database schema v{version} is newer than this app supports (v{latest}). "
            "Update the app before opening this database."
        )

    for migration in migrations[version:]:
        _apply(conn, migration, complete_statement)
        log.info("Applied migration %04d_%s", migration.version, migration.name)

    return current_version(conn)


def _apply(conn: Any, migration: Migration, complete_statement: Any) -> None:
    if migration.disables_foreign_keys:
        conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in split_statements(migration.sql, complete_statement):
                conn.execute(statement)
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise MigrationError(f"Foreign key violations after migration: {violations[:5]}")
            # user_version cannot be parameterized; value is an int we control.
            conn.execute(f"PRAGMA user_version = {int(migration.version)}")
            conn.execute("COMMIT")
        except Exception as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError(
                f"Migration {migration.version:04d}_{migration.name} failed: {exc}"
            ) from exc
    finally:
        if migration.disables_foreign_keys:
            conn.execute("PRAGMA foreign_keys = ON")
