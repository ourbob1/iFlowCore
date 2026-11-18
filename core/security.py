"""
Database key handling and the per-platform storage-protection policy.

Key formats accepted by the connection manager:
  * a passphrase — SQLCipher derives the AES-256 key with PBKDF2-HMAC-SHA512
    (256,000 iterations in SQLCipher 4) — used on desktop, or
  * a raw 256-bit key as 64 hex chars — skips the KDF; what device keystores
    generate (`generate_raw_key`).

Storage protection by platform (`resolve_storage_policy`)
---------------------------------------------------------
Desktop (Windows / macOS / Linux)
    SQLCipher, full-database AES-256. Mandatory in production: a missing
    driver or key fails loudly. Development may run unencrypted with a warning.

Mobile (Android / iOS)
    No SQLCipher wheel exists for Flet's mobile Python runtime, so the database
    is standard SQLite protected by the platform:
      - the app sandbox (no other app can read the file),
      - OS file encryption (Android file-based encryption; iOS Data
        Protection, class "until first unlock" so background trip logging
        still works while the phone is locked),
      - a 256-bit device key kept in the Keychain / Android Keystore
        (hardware-backed where available) that seals the most sensitive
        fields with AES-256-GCM (`core.crypto.FieldSealer`).
    This is weaker than SQLCipher against a rooted/jailbroken or forensically
    unlocked device: ledger rows are protected by the OS, not by the app.
    When a mobile SQLCipher driver becomes importable, the same device key
    opens (and one-time migrates) the database into SQLCipher automatically.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Literal, Protocol
import secrets
from typing import Any, Mapping

DB_KEY_ENV = "IFLOW_DB_KEY"
MIN_PASSPHRASE_LENGTH = 12
_RAW_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class InvalidKeyMaterialError(ValueError):
    """Invalid database key material."""


def is_raw_key(key: str) -> bool:
    """True if `key` is a 256-bit key written as 64 hex characters."""
    return bool(_RAW_KEY_RE.match(key))


def validate_key(key: str) -> str:
    """Reject obviously weak or malformed keys before they reach SQLCipher."""
    if not isinstance(key, str) or not key:
        raise InvalidKeyMaterialError("Database key must be a non-empty string")
    if "\x00" in key:
        raise InvalidKeyMaterialError("Database key must not contain NUL bytes")
    if not is_raw_key(key) and len(key) < MIN_PASSPHRASE_LENGTH:
        raise InvalidKeyMaterialError(
            f"Passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters "
            "(or supply a 64-char hex raw key)"
        )
    return key


def generate_raw_key() -> str:
    """Generate a new random 256-bit key as hex (for first-run provisioning)."""
    return secrets.token_hex(32)


def resolve_db_key(env: Mapping[str, str] | None = None) -> str | None:
    """Return the configured DB key, or None if not set."""
    env = os.environ if env is None else env
    key = env.get(DB_KEY_ENV)
    return validate_key(key) if key else None


# ---------------------------------------------------------------------------
# Storage policy
# ---------------------------------------------------------------------------

ProtectionMode = Literal["sqlcipher", "platform", "vault", "none"]


class StoragePolicyError(RuntimeError):
    """The platform's required protection cannot be provided."""


@dataclass(frozen=True)
class StoragePolicy:
    platform: str
    mode: ProtectionMode
    field_sealing: bool           # sensitive fields sealed with the device key
    description: str

    @property
    def encrypted_database(self) -> bool:
        return self.mode == "sqlcipher"


def resolve_storage_policy(
    platform: str,
    *,
    require_encryption: bool,
    sqlcipher_available: bool,
    have_key: bool,
) -> StoragePolicy:
    """Decide how the local database is protected on this platform.

    Raises StoragePolicyError on desktop when encryption is required but
    SQLCipher or the key is missing — desktop never silently degrades.
    """
    from core.platform import is_mobile

    if platform == "web":
        return StoragePolicy(platform, "vault", False,
                             "Encrypted in this browser with your passphrase (AES-256-GCM snapshots)")
    if is_mobile(platform):
        if sqlcipher_available and have_key:
            return StoragePolicy(platform, "sqlcipher", False,
                                 "Encrypted with SQLCipher using a key from this device's secure storage")
        return StoragePolicy(platform, "platform", True,
                             "Protected by the app sandbox and device encryption; sensitive fields "
                             "sealed with a key from this device's secure storage")
    if require_encryption:
        if not sqlcipher_available:
            raise StoragePolicyError(
                "SQLCipher is required on desktop but no driver is installed "
                "(install 'sqlcipher3-binary' on Linux or 'sqlcipher3' on macOS/Windows)")
        if not have_key:
            raise StoragePolicyError("SQLCipher is required on desktop but no database key was provided")
        return StoragePolicy(platform, "sqlcipher", False, "Encrypted with SQLCipher (AES-256)")
    if sqlcipher_available and have_key:
        return StoragePolicy(platform, "sqlcipher", False, "Encrypted with SQLCipher (AES-256)")
    return StoragePolicy(platform, "none", False, "NOT encrypted (development mode)")


# ---------------------------------------------------------------------------
# Platform-native key storage
# ---------------------------------------------------------------------------

DEVICE_KEY_NAME = "rubysoho.device_key.v1"    # frozen pre-rebrand name: renaming loses the device key


class KeyStoreError(RuntimeError):
    """Secure storage is unavailable or returned unusable key material."""


class KeyStore(Protocol):
    """Async key/value secret store (Keychain, Android Keystore, …)."""

    async def get(self, name: str) -> str | None: ...

    async def set(self, name: str, value: str) -> None: ...

    async def delete(self, name: str) -> None: ...


class MemoryKeyStore:
    """In-process store for tests and desktop development."""

    def __init__(self, initial: Mapping[str, str] | None = None) -> None:
        self._data = dict(initial or {})

    async def get(self, name: str) -> str | None:
        return self._data.get(name)

    async def set(self, name: str, value: str) -> None:
        self._data[name] = value

    async def delete(self, name: str) -> None:
        self._data.pop(name, None)


class FletSecureKeyStore:
    """Keychain (iOS/macOS) / Android Keystore-backed storage via flet-secure-storage.

    * iOS: `first_unlock_this_device` — readable after the first unlock since
      boot (so background trip logging works on a locked phone), never synced
      to iCloud and never restored onto another device.
    * Android: EncryptedSharedPreferences with an AES-GCM key held in the
      Android Keystore. `reset_on_error` is off: a keystore failure surfaces
      as an error instead of silently replacing the key.
    """

    def __init__(self, page: Any) -> None:
        try:
            import flet_secure_storage as fss
        except ImportError as exc:  # pragma: no cover - mobile builds include it
            raise KeyStoreError("flet-secure-storage is not installed") from exc
        self._storage = fss.SecureStorage(
            ios_options=fss.IOSOptions(accessibility=fss.KeychainAccessibility.FIRST_UNLOCK_THIS_DEVICE,
                                       # account_name is frozen (pre-rebrand): changing it would orphan existing keychain entries
                                       synchronizable=False, account_name="rubysoho"),
            android_options=fss.AndroidOptions(reset_on_error=False),
        )
        page.services.append(self._storage)

    async def get(self, name: str) -> str | None:
        return await self._storage.get(name)

    async def set(self, name: str, value: str) -> None:
        await self._storage.set(name, value)

    async def delete(self, name: str) -> None:
        await self._storage.remove(name)


async def ensure_device_key(store: KeyStore, name: str = DEVICE_KEY_NAME) -> str:
    """Return this device's 256-bit key, creating it on first run.

    The value is read back after writing so a store that silently drops
    writes is caught now rather than when the key is next needed.
    """
    try:
        existing = await store.get(name)
    except Exception as exc:
        raise KeyStoreError(f"Secure storage unavailable: {exc}") from exc
    if existing:
        if not is_raw_key(existing):
            raise KeyStoreError("Stored device key is malformed; refusing to replace it automatically")
        return existing.lower()
    key = generate_raw_key()
    try:
        await store.set(name, key)
        confirmed = await store.get(name)
    except Exception as exc:
        raise KeyStoreError(f"Secure storage unavailable: {exc}") from exc
    if confirmed != key:
        raise KeyStoreError("Secure storage did not persist the device key")
    return key
