"""Local-first persistence layer (SQLite / SQLCipher)."""

from database.connection import Database, DatabaseError, EncryptionUnavailableError, InvalidKeyError

__all__ = ["Database", "DatabaseError", "EncryptionUnavailableError", "InvalidKeyError"]
