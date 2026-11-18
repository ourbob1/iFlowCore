"""
Application state and secure database initialization.

`Unlocker` decides how the database is opened at startup:

  0. Android / iOS                                     → device key from the
     Keychain / Android Keystore (created on first run); SQLCipher if bundled,
     otherwise platform-protected SQLite with sealed sensitive fields
  1. `IFLOW_DB_KEY` set (CI, managed devices)      → open silently
  2. encryption required (production default)          → ask for the passphrase
                                                          (first run: create one)
  3. development without a key                         → open unencrypted, and the
                                                          shell shows a warning banner

The passphrase is held only as long as it takes to open the pool — it is
never written to disk, logged, or kept on the state object. Repeated wrong
passphrases trigger an escalating lockout.

`AppState` is the single object views read from: the open database, device
settings, preferences, and a clock (injectable for tests).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from core.config import AppConfig, UnitSystem
from core.device_settings import DeviceSettings, SettingsStore
from core.security import InvalidKeyMaterialError, resolve_db_key, validate_key
from core.timeutil import local_date, utc_now
from database.connection import Database, EncryptionUnavailableError, InvalidKeyError

log = logging.getLogger(__name__)

SYNCED_TABLES = (
    "platforms", "transactions", "expenses", "mileage_logs", "tax_profiles", "import_mappings", "receipts",
    "vehicles", "vehicle_assignments", "fuel_logs", "maintenance_schedules", "maintenance_records",
    "tax_set_asides", "health_plans", "health_premium_entries",
)
LOCKOUT_AFTER = 5            # failed attempts before delays start
LOCKOUT_BASE_S = 30          # doubles each further failure


class UnlockError(ValueError):
    pass


@dataclass
class UnlockPlan:
    mode: str                    # "auto_key" | "prompt" | "create" | "plaintext_dev"
    message: str


class Unlocker:
    def __init__(self, config: AppConfig, *, env: dict[str, str] | None = None,
                 clock: Callable[[], float] = time.monotonic, vault: Any = None) -> None:
        self.config = config
        self.vault = vault                   # web build: core.web.session.WebVaultSession
        self._env = env
        self._clock = clock
        self.failures = 0
        self._locked_until = 0.0

    @property
    def first_run(self) -> bool:
        return not Path(self.config.db_path).exists()

    @property
    def mobile(self) -> bool:
        return self.config.is_mobile

    def plan(self) -> UnlockPlan:
        if self.vault is not None:
            if self.vault.exists:
                return UnlockPlan("prompt", "Enter your passphrase to unlock")
            return UnlockPlan("create", "Create a passphrase to encrypt your data in this browser")
        if self.mobile:
            return UnlockPlan("device_key", "Opening with this device's secure storage…")
        if resolve_db_key(self._env) is not None:
            return UnlockPlan("auto_key", "Opening with the configured key")
        if self.config.require_encryption:
            if self.first_run:
                return UnlockPlan("create", "Create a passphrase to encrypt your data on this device")
            from database.encryption import is_plaintext_sqlite

            if is_plaintext_sqlite(self.config.db_path):
                # e.g. created by a development run: no passphrase can open it as SQLCipher
                return UnlockPlan("encrypt_existing", "Your existing data isn't encrypted yet. Create a "
                                  "passphrase to encrypt it on this device")
            return UnlockPlan("prompt", "Enter your passphrase to unlock")
        return UnlockPlan("plaintext_dev", "Development mode: database is NOT encrypted")

    def open_with_device_key(self, device_key: str) -> Database:
        """Mobile: open with the key from platform secure storage.

        If a SQLCipher driver is bundled and the existing file is still
        plaintext (created before SQLCipher shipped), it is encrypted in place
        first. Without SQLCipher the database opens as platform-protected
        SQLite and the key is used for field sealing by the caller.
        """
        from database.connection import load_sqlcipher_driver
        from database.encryption import is_plaintext_sqlite, migrate_to_sqlcipher

        try:
            validate_key(device_key)
        except InvalidKeyMaterialError as exc:
            raise UnlockError(f"Device key unusable: {exc}") from None
        if load_sqlcipher_driver() is not None and is_plaintext_sqlite(self.config.db_path):
            from database.encryption import MigrationFailed

            try:
                migrate_to_sqlcipher(self.config.db_path, device_key)
            except MigrationFailed as exc:        # original file is left untouched
                raise UnlockError(f"Couldn't encrypt the existing database: {exc}") from None
        db = Database(self.config.db_path, key=device_key, require_encryption=self.config.require_encryption,
                      pool_size=self.config.db_pool_size, platform=self.config.platform)
        try:
            db.initialize()
        except InvalidKeyError:
            db.close()
            raise UnlockError("This device's key doesn't match the database. Restore from a backup "
                              "or reinstall to start fresh.") from None
        return db

    def _open_vault(self, plan: UnlockPlan, passphrase: str | None, confirm: str | None) -> Database:
        """Web build: the passphrase opens (or creates) the encrypted vault in this browser."""
        from core.web.vault import VaultError, WrongPassphrase

        if not passphrase:
            raise UnlockError("Enter your passphrase")
        if plan.mode == "create" and passphrase != confirm:
            raise UnlockError("Passphrases don't match")
        try:
            validate_key(passphrase)
        except InvalidKeyMaterialError as exc:
            raise UnlockError(str(exc)) from None
        try:
            if plan.mode == "create":
                self.vault.create(passphrase)
            else:
                self.vault.unlock(passphrase)
        except WrongPassphrase:
            self._fail()
            raise UnlockError("That passphrase didn't unlock the data saved in this browser") from None
        except VaultError as exc:
            raise UnlockError(str(exc)) from None
        db = Database(self.config.db_path, pool_size=1, platform="web")
        db.initialize()
        self.vault.attach(db)
        self.failures = 0
        return db

    def _encrypt_existing(self, key: str) -> None:
        """Encrypt a plaintext database in place (verified copy, atomic swap, plaintext deleted)."""
        from database.encryption import MigrationFailed, migrate_to_sqlcipher

        try:
            result = migrate_to_sqlcipher(self.config.db_path, key)
        except EncryptionUnavailableError as exc:
            raise UnlockError(str(exc)) from None
        except MigrationFailed as exc:            # the original file is left untouched
            raise UnlockError(f"Couldn't encrypt the existing database: {exc}") from None
        log.info("Encrypted the existing database (%d tables, %d rows)", result.tables, result.rows)

    def lockout_remaining(self) -> int:
        return max(0, int(self._locked_until - self._clock() + 0.999))

    def open(self, passphrase: str | None = None, *, confirm: str | None = None) -> Database:
        """Open (and migrate) the database. Raises UnlockError with a user-safe message."""
        plan = self.plan()
        if plan.mode == "device_key":
            raise UnlockError("This device unlocks with its secure-storage key")
        if self.lockout_remaining():
            raise UnlockError(f"Too many attempts. Try again in {self.lockout_remaining()} s.")
        if self.vault is not None:
            return self._open_vault(plan, passphrase, confirm)
        key: str | None
        if plan.mode == "auto_key":
            key = resolve_db_key(self._env)
        elif plan.mode == "plaintext_dev" and not passphrase:
            key = None
        else:
            if not passphrase:
                raise UnlockError("Enter your passphrase")
            if plan.mode in ("create", "encrypt_existing") and passphrase != confirm:
                raise UnlockError("Passphrases don't match")
            try:
                key = validate_key(passphrase)
            except InvalidKeyMaterialError as exc:
                raise UnlockError(str(exc)) from None
            if plan.mode == "encrypt_existing":
                self._encrypt_existing(key)

        try:
            db = Database(self.config.db_path, key=key, require_encryption=self.config.require_encryption,
                          pool_size=self.config.db_pool_size, platform=self.config.platform)
        except EncryptionUnavailableError as exc:
            raise UnlockError(str(exc)) from None
        try:
            db.initialize()
        except InvalidKeyError:
            db.close()
            self._fail()
            raise UnlockError("That passphrase didn't unlock the database") from None
        except EncryptionUnavailableError as exc:
            db.close()
            raise UnlockError(str(exc)) from None
        self.failures = 0
        return db

    def _fail(self) -> None:
        self.failures += 1
        if self.failures >= LOCKOUT_AFTER:
            self._locked_until = self._clock() + LOCKOUT_BASE_S * 2 ** (self.failures - LOCKOUT_AFTER)


@dataclass
class AppState:
    config: AppConfig
    clock: Callable[[], datetime] = utc_now
    db: Database | None = None
    store: SettingsStore | None = None
    settings: DeviceSettings = field(default_factory=DeviceSettings)
    offline: bool = False
    banner: str | None = None
    sealer: Any = None                # device-key field sealer (mobile platform storage only)

    @property
    def unlocked(self) -> bool:
        return self.db is not None

    def attach(self, db: Database, *, device_key: str | None = None) -> None:
        """Bind an open database. On mobile platform storage the device key
        seals sensitive settings; without it those fields refuse to save."""
        sealer = None
        if device_key and db.protection == "platform":
            from core.crypto import FieldSealer

            sealer = FieldSealer(device_key)
        self.db = db
        self.sealer = sealer
        self.store = SettingsStore(db, sealer=sealer, require_sealing=db.protection == "platform")
        self.settings = self.store.load()
        self.banner = "Development database is not encrypted" if db.protection == "none" else None

    def lock(self) -> None:
        if self.db is not None:
            self.db.close()
        self.db, self.store, self.sealer = None, None, None
        self.settings = DeviceSettings()

    # -- licensing ---------------------------------------------------------

    def license_manager(self) -> Any:
        """Offline license checks. Simulated purchases only outside production builds."""
        from core.licensing import LicenseManager

        if self.store is None:
            raise RuntimeError("Database is locked")
        return LicenseManager(self.store, clock=self.clock, allow_simulation=not self.config.is_production)

    @property
    def license(self) -> Any:
        """Current LicenseState (Solo Core while locked)."""
        from core.licensing import FREE_STATE

        if self.store is None:
            return FREE_STATE
        return self.license_manager().state()

    def allows(self, feature: Any) -> bool:
        return self.license.allows(feature)

    def require(self, feature: Any) -> None:
        self.license.require(feature)

    def zone_store(self) -> Any:
        """Coarse trip zones for Insights: sealed with the device key on mobile platform storage."""
        from core.intelligence.zones import ZoneStore

        if self.db is None:
            raise RuntimeError("Database is locked")
        return ZoneStore(self.db, sealer=self.sealer, require_sealing=self.db.protection == "platform")

    def update_settings(self, **changes: Any) -> DeviceSettings:
        if self.store is None:
            raise RuntimeError("Database is locked")
        self.settings = self.store.update(**changes)
        return self.settings

    # -- convenience -------------------------------------------------------

    @property
    def currency(self) -> str:
        return self.config.preferences.base_currency

    @property
    def units(self) -> UnitSystem:
        return self.config.preferences.unit_system

    @property
    def tz(self) -> str:
        return self.settings.timezone

    def now(self) -> datetime:
        return self.clock()

    def today(self) -> date:
        return local_date(self.clock(), self.tz)

    def pending_sync_count(self) -> int:
        if self.db is None:
            return 0
        total = 0
        with self.db.connection() as conn:
            for table in SYNCED_TABLES:
                total += conn.execute(f"SELECT count(*) FROM {table} WHERE sync_status <> 'synced'").fetchone()[0]
        return total
