"""
Account-level sync operations the UI calls (10.5): set up, unlock on another device, recover,
change the sync passphrase, turn off, sync now.

The account data key is kept in device settings (`sync_keys`, sealed on mobile, inside SQLCipher on
desktop), so the passphrase is typed once per device. It is bound to the signed-in account: a device
never pushes one account's data with another account's keys.
"""

from __future__ import annotations

from typing import Any

from core.account.client import AccountClient, AccountError
from core.sync.crypto import SyncKeys
from core.sync.engine import SyncEngine, SyncError, SyncReport


class SyncManager:
    def __init__(self, db: Any, settings_store: Any, client: AccountClient) -> None:
        self.db, self.settings, self.client = db, settings_store, client

    # --- state ------------------------------------------------------------------------------------

    def _account_id(self) -> str:
        session = self.client.session
        if session is None:
            raise SyncError("Sign in to your account to sync")
        return session.user_id

    def keys(self) -> SyncKeys | None:
        keys = SyncKeys.from_local(self.settings.load().sync_keys)
        if keys is not None and self.client.session is not None and keys.account_id != self.client.session.user_id:
            raise SyncError("This device's sync keys belong to a different account. Turn sync off first.")
        return keys

    @property
    def enabled(self) -> bool:
        s = self.settings.load()
        return bool(s.sync_enabled and s.sync_keys)

    def _store(self, keys: SyncKeys) -> None:
        self.settings.update(sync_keys=keys.to_local(), sync_enabled=True)

    def _envelope(self) -> tuple[dict[str, Any], int] | None:
        try:
            data = self.client.api("GET", "/v1/sync/keys")
        except AccountError as exc:
            if exc.status == 404:
                return None
            raise
        return data["envelope"], int(data["key_version"])

    # --- setup ------------------------------------------------------------------------------------

    def setup(self, passphrase: str) -> str | None:
        """First device: create the account key and return the recovery key (show it once).
        Other devices: unlock the existing key with the passphrase and return None."""
        account = self._account_id()
        for _ in range(2):
            existing = self._envelope()
            if existing is not None:
                self._store(SyncKeys.unlock(account, existing[0], passphrase))
                return None
            keys, envelope, recovery = SyncKeys.create(account, passphrase)
            try:
                self.client.api("PUT", "/v1/sync/keys", {"envelope": envelope, "expected_version": None})
            except AccountError as exc:
                if exc.status == 409:             # another device set sync up at the same moment: use theirs
                    continue
                raise
            self._store(keys)
            return recovery
        raise SyncError("Couldn't set up sync; try again")

    def recover(self, recovery_key: str, new_passphrase: str) -> None:
        """Forgotten passphrase: unlock with the recovery key and choose a new passphrase."""
        account = self._account_id()
        existing = self._envelope()
        if existing is None:
            raise SyncError("Sync isn't set up for this account")
        envelope, version = existing
        keys = SyncKeys.recover(account, envelope, recovery_key)
        self.client.api("PUT", "/v1/sync/keys", {"envelope": keys.rewrap_passphrase(envelope, new_passphrase),
                                                 "expected_version": version})
        self._store(keys)

    def change_passphrase(self, new_passphrase: str) -> None:
        keys = self.keys()
        existing = self._envelope()
        if keys is None or existing is None:
            raise SyncError("Sync isn't set up on this device")
        envelope, version = existing
        self.client.api("PUT", "/v1/sync/keys", {"envelope": keys.rewrap_passphrase(envelope, new_passphrase),
                                                 "expected_version": version})

    def new_recovery_key(self) -> str:
        keys = self.keys()
        existing = self._envelope()
        if keys is None or existing is None:
            raise SyncError("Sync isn't set up on this device")
        envelope, version = existing
        updated, recovery = keys.new_recovery_key(envelope)
        self.client.api("PUT", "/v1/sync/keys", {"envelope": updated, "expected_version": version})
        return recovery

    def disable(self) -> None:
        """Stop syncing on this device and forget its copy of the key (local data stays)."""
        self.settings.update(sync_keys=None, sync_enabled=False)

    # --- run --------------------------------------------------------------------------------------

    def engine(self) -> SyncEngine:
        keys = self.keys()
        session = self.client.session
        if keys is None or session is None or not self.enabled:
            raise SyncError("Sync is off on this device")
        return SyncEngine(self.db, self.client, keys, device_id=session.device_id)

    def sync_now(self) -> SyncReport:
        return self.engine().sync()
