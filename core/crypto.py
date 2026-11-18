"""
Field-level sealing with the device key (AES-256-GCM).

Used where the database itself isn't encrypted (mobile "platform" mode) to
protect the most sensitive values — home location, work zones, feed URLs,
API credentials. Each value is bound to its field name through the GCM
associated data, so a sealed value can't be copied into another field.

Token format: ``rs1:<base64url(nonce ‖ ciphertext+tag)>``

The sealing key is derived from the device key with HKDF-SHA256 and a
purpose label, so it is never the same key that (in future) opens SQLCipher.
Requires the `cryptography` package (Flet ships prebuilt Android/iOS wheels).
"""

from __future__ import annotations

import base64
import os

TOKEN_PREFIX = "rs1:"
NONCE_BYTES = 12
_HKDF_INFO = b"rubysoho/field-sealing/v1"   # frozen pre-rebrand context: changing it breaks sealed fields


class SealingError(ValueError):
    """A sealed value could not be created or opened (wrong key, tampering, bad token)."""


def _aesgcm(device_key_hex: str):
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as exc:  # pragma: no cover - dependency present on all targets
        raise SealingError("Field sealing needs the 'cryptography' package") from exc
    try:
        ikm = bytes.fromhex(device_key_hex)
    except ValueError as exc:
        raise SealingError("Device key must be hex") from exc
    if len(ikm) != 32:
        raise SealingError("Device key must be 256 bits")
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(ikm)
    return AESGCM(key)


class FieldSealer:
    def __init__(self, device_key_hex: str) -> None:
        self._aead = _aesgcm(device_key_hex)

    def seal(self, plaintext: str, *, context: str) -> str:
        nonce = os.urandom(NONCE_BYTES)
        ct = self._aead.encrypt(nonce, plaintext.encode("utf-8"), context.encode("utf-8"))
        return TOKEN_PREFIX + base64.urlsafe_b64encode(nonce + ct).decode("ascii")

    def open(self, token: str, *, context: str) -> str:
        from cryptography.exceptions import InvalidTag

        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
            raise SealingError("Not a sealed value")
        try:
            raw = base64.urlsafe_b64decode(token[len(TOKEN_PREFIX):].encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise SealingError("Corrupt sealed value") from exc
        if len(raw) <= NONCE_BYTES + 16:
            raise SealingError("Corrupt sealed value")
        try:
            return self._aead.decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], context.encode("utf-8")).decode("utf-8")
        except InvalidTag as exc:
            raise SealingError("Sealed value failed authentication (wrong key or tampered)") from exc

    def __repr__(self) -> str:
        return "FieldSealer(<device key>)"


def is_sealed(value: object) -> bool:
    return isinstance(value, str) and value.startswith(TOKEN_PREFIX)
