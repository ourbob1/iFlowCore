"""
Glue between the vault and the running web app.

While unlocked, the database and preferences live as ordinary files in Pyodide's in-memory
filesystem, so the rest of the app is unchanged. This session restores them from the vault at
unlock and writes an encrypted snapshot back whenever they change: after every committed
transaction (Database.after_commit) and on a short timer for anything written outside one
(preferences, autocommit statements).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from core.web.vault import Vault

log = logging.getLogger(__name__)

DB_MEMBER = "iflowcore.db"
PREFS_MEMBER = "preferences.json"


class WebVaultSession:
    def __init__(self, vault: Vault, db_path: Path, prefs_path: Path) -> None:
        self.vault, self.db_path, self.prefs_path = vault, Path(db_path), Path(prefs_path)
        self._signature: tuple[Any, ...] | None = None
        self.saves = 0

    @property
    def exists(self) -> bool:
        return self.vault.exists()

    def _members(self) -> dict[str, Path]:
        return {DB_MEMBER: self.db_path, PREFS_MEMBER: self.prefs_path}

    def _current_signature(self) -> tuple[Any, ...]:
        sig = []
        for path in self._members().values():
            try:
                st = os.stat(path)
                sig.append((st.st_mtime_ns, st.st_size))
            except FileNotFoundError:
                sig.append(None)
        return tuple(sig)

    def files(self) -> dict[str, bytes]:
        return {name: path.read_bytes() for name, path in self._members().items() if path.exists()}

    def _clear_local(self) -> None:
        for path in [self.db_path, self.prefs_path, *(Path(f"{self.db_path}{s}") for s in ("-journal", "-wal", "-shm"))]:
            path.unlink(missing_ok=True)

    def create(self, passphrase: str) -> None:
        """First run in this browser: start empty (drop anything left in memory) and create the vault."""
        self._clear_local()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.vault.create(passphrase, {})

    def unlock(self, passphrase: str) -> None:
        files = self.vault.unlock(passphrase)          # raises WrongPassphrase
        self._clear_local()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        for name, path in self._members().items():
            if name in files:
                path.write_bytes(files[name])
        self._signature = self._current_signature()

    def attach(self, db: Any) -> None:
        if self.save_if_changed not in db.after_commit:
            db.after_commit.append(self.save_if_changed)
        self.save_if_changed()

    def save_if_changed(self) -> bool:
        if not self.vault.unlocked:
            return False
        sig = self._current_signature()
        if sig == self._signature:
            return False
        self.vault.save(self.files())
        self._signature = sig
        self.saves += 1
        return True

    def change_passphrase(self, new_passphrase: str) -> None:
        self.vault.change_passphrase(new_passphrase, self.files())
        self._signature = self._current_signature()

    def lock(self) -> None:
        """Save, forget the key and wipe the plaintext copies from memory."""
        if self.vault.unlocked:
            self.save_if_changed()
        self.vault.lock()
        self._clear_local()
        self._signature = None
