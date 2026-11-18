"""
One-time migration of a plaintext SQLite database into SQLCipher.

Used when a device that ran in "platform" mode (mobile without SQLCipher)
gets a build that bundles a SQLCipher driver: the next unlock encrypts the
existing database in place with the device key. Also usable on desktop to
encrypt a development database.

Steps: checkpoint the WAL → `sqlcipher_export` into a new encrypted file →
copy `user_version` → verify (integrity check, same tables, same row counts)
→ atomically swap files → delete the plaintext copy and its WAL/SHM.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from database.connection import EncryptionUnavailableError, _key_pragma, load_sqlcipher_driver

log = logging.getLogger(__name__)
SQLITE_HEADER = b"SQLite format 3\x00"


class MigrationFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class EncryptionMigration:
    tables: int
    rows: int
    user_version: int


def is_plaintext_sqlite(path: str | Path) -> bool:
    """True for an existing, non-empty, unencrypted SQLite file."""
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        return False
    with p.open("rb") as fh:
        return fh.read(16) == SQLITE_HEADER


def _counts(conn, schema: str = "main") -> dict[str, int]:
    names = [r[0] for r in conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {n: conn.execute(f'SELECT count(*) FROM {schema}."{n}"').fetchone()[0] for n in names}


def migrate_to_sqlcipher(path: str | Path, key: str) -> EncryptionMigration:
    cipher = load_sqlcipher_driver()
    if cipher is None:
        raise EncryptionUnavailableError("SQLCipher driver not installed")
    src = Path(path)
    if not is_plaintext_sqlite(src):
        raise MigrationFailed(f"{src} is not a plaintext SQLite database")
    tmp = src.with_name(src.name + ".encrypting")
    tmp.unlink(missing_ok=True)

    conn = cipher.connect(str(src), isolation_level=None)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = _counts(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        key_sql = _key_pragma(key).split("=", 1)[1].strip()      # 'passphrase' or "x'hex'"
        conn.execute(f"ATTACH DATABASE ? AS encrypted KEY {key_sql}", (str(tmp),))
        conn.execute("SELECT sqlcipher_export('encrypted')")
        conn.execute(f"PRAGMA encrypted.user_version = {int(version)}")
        conn.execute("DETACH DATABASE encrypted")
    finally:
        conn.close()

    check = cipher.connect(str(tmp), isolation_level=None)
    try:
        check.execute(_key_pragma(key))
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrationFailed("Encrypted copy failed integrity check")
        after = _counts(check)
        if after != before or check.execute("PRAGMA user_version").fetchone()[0] != version:
            raise MigrationFailed("Encrypted copy does not match the original")
    except Exception:
        check.close()
        tmp.unlink(missing_ok=True)
        raise
    check.close()
    if is_plaintext_sqlite(tmp):
        tmp.unlink(missing_ok=True)
        raise MigrationFailed("Export produced a plaintext file")

    os.replace(tmp, src)                           # atomic on the same filesystem
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{src}{suffix}").unlink(missing_ok=True)
    log.info("Encrypted %s with SQLCipher (%d tables, %d rows)", src, len(before), sum(before.values()))
    return EncryptionMigration(len(before), sum(before.values()), version)
