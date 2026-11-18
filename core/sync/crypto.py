"""
End-to-end encryption for sync. Everything here runs on the device; the server only ever sees the
wrapped key envelope and record ciphertext.

Key hierarchy
-------------
ADK   Account Data Key: 32 random bytes, created once by the first device.
KEK   Argon2id(sync passphrase, 16-byte salt; t=3, m=64 MiB, p=1). Wraps the ADK (AES-256-GCM).
RK    Recovery key: 32 random bytes shown once as 11 groups of 5 base32 characters (with a checksum). Wraps the ADK
      too, via HKDF (no stretching needed: it's full-entropy).
DEK   HKDF(ADK, "data/v1"): encrypts records.
CK    HKDF(ADK, "collection/v1"): HMACs table names into opaque collection ids.

Envelope (stored on the server, opaque to it):
  {"v":1, "kdf":"argon2id", "salt", "t", "m", "p", "pass": wrapped ADK, "recovery": wrapped ADK,
   "check": HMAC(ADK, "check") first 8 bytes (lets a device confirm it unwrapped the right key)}
Wraps are bound to the account id (AAD), so an envelope can't be moved to another account.

Records
-------
plaintext  JSON of the row → zlib → 4-byte length + data + zero padding to a multiple of 256 bytes
           (the server learns only a coarse size).
ciphertext 0x01 ‖ 12-byte random nonce ‖ AES-256-GCM(DEK, padded, AAD)
AAD        "iflow-sync/rec/v1|account|collection|record_id|version": the server can't move a record
           to another id or table, or replay an old version as a newer one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import zlib
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

FORMAT = 1
PAD = 256
ARGON2 = {"t": 3, "m": 64 * 1024, "p": 1}
_B32 = "ABCDEFGHJKMNPQRSTVWXYZ0123456789"          # Crockford-style: no I, L, O, U


class SyncCryptoError(Exception):
    pass


class WrongPassphrase(SyncCryptoError):
    pass


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    try:
        return base64.b64decode(s, validate=True)
    except (ValueError, TypeError) as exc:
        raise SyncCryptoError("corrupt key envelope") from exc


def _hkdf(key: bytes, info: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=f"iflow-sync/{info}".encode()).derive(key)


def _kek(passphrase: str, salt: bytes, t: int, m: int, p: int) -> bytes:
    if not passphrase or len(passphrase) < 10:
        raise SyncCryptoError("Use a sync passphrase of at least 10 characters")
    return Argon2id(salt=salt, length=32, iterations=t, lanes=p, memory_cost=m).derive(passphrase.encode("utf-8"))


def _wrap(kek: bytes, adk: bytes, account_id: str, purpose: str) -> str:
    nonce = os.urandom(12)
    return _b64(nonce + AESGCM(kek).encrypt(nonce, adk, f"iflow-sync/adk/v1|{account_id}|{purpose}".encode()))


def _unwrap(kek: bytes, blob: str, account_id: str, purpose: str) -> bytes:
    raw = _unb64(blob)
    try:
        return AESGCM(kek).decrypt(raw[:12], raw[12:], f"iflow-sync/adk/v1|{account_id}|{purpose}".encode())
    except InvalidTag as exc:
        raise WrongPassphrase("That passphrase or recovery key doesn't unlock this account's data") from exc


def _check(adk: bytes) -> str:
    return hmac.new(adk, b"iflow-sync/check/v1", hashlib.sha256).hexdigest()[:16]


# --- recovery key ------------------------------------------------------------------------------------


def format_recovery_key(rk: bytes) -> str:
    data = rk + hashlib.sha256(rk).digest()[:2]                   # 34 bytes = 272 bits
    value = int.from_bytes(data, "big") << 3                        # pad to 275 bits = 55 × 5
    text = "".join(_B32[(value >> (5 * (54 - i))) & 31] for i in range(55))
    return "-".join(text[i:i + 5] for i in range(0, len(text), 5))


def parse_recovery_key(text: str) -> bytes:
    cleaned = "".join(ch for ch in text.upper() if ch.isalnum())
    cleaned = cleaned.replace("O", "0").replace("I", "1").replace("L", "1")
    if len(cleaned) != 55 or any(ch not in _B32 for ch in cleaned):
        raise SyncCryptoError("That doesn't look like a recovery key (55 letters and digits)")
    bits = 0
    for ch in cleaned:
        bits = (bits << 5) | _B32.index(ch)
    data = (bits >> 3).to_bytes(34, "big")                          # 275 bits carried, 272 used
    rk, check = data[:32], data[32:]
    if hashlib.sha256(rk).digest()[:2] != check:
        raise SyncCryptoError("That recovery key has a typo (checksum doesn't match)")
    return rk


# --- keys --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncKeys:
    account_id: str
    adk: bytes

    @property
    def dek(self) -> bytes:
        return _hkdf(self.adk, "data/v1")

    def collection_id(self, table: str) -> str:
        return hmac.new(_hkdf(self.adk, "collection/v1"), table.encode(), hashlib.sha256).hexdigest()[:32]

    # --- envelope ---

    @classmethod
    def create(cls, account_id: str, passphrase: str) -> tuple[SyncKeys, dict[str, Any], str]:
        """New account key: returns (keys, envelope for the server, recovery key to show once)."""
        adk, rk = os.urandom(32), os.urandom(32)
        keys = cls(account_id, adk)
        return keys, keys.envelope(passphrase, rk), format_recovery_key(rk)

    def envelope(self, passphrase: str, rk: bytes) -> dict[str, Any]:
        salt = os.urandom(16)
        kek = _kek(passphrase, salt, ARGON2["t"], ARGON2["m"], ARGON2["p"])
        return {"v": FORMAT, "kdf": "argon2id", "salt": _b64(salt), **ARGON2,
                "pass": _wrap(kek, self.adk, self.account_id, "passphrase"),
                "recovery": _wrap(_hkdf(rk, "recovery-wrap/v1"), self.adk, self.account_id, "recovery"),
                "check": _check(self.adk)}

    def rewrap_passphrase(self, envelope: dict[str, Any], new_passphrase: str) -> dict[str, Any]:
        """Change the passphrase; the recovery wrap (and so the recovery key) stays valid."""
        salt = os.urandom(16)
        kek = _kek(new_passphrase, salt, ARGON2["t"], ARGON2["m"], ARGON2["p"])
        return {**envelope, "salt": _b64(salt), **ARGON2, "pass": _wrap(kek, self.adk, self.account_id, "passphrase")}

    def new_recovery_key(self, envelope: dict[str, Any]) -> tuple[dict[str, Any], str]:
        rk = os.urandom(32)
        return ({**envelope, "recovery": _wrap(_hkdf(rk, "recovery-wrap/v1"), self.adk, self.account_id, "recovery")},
                format_recovery_key(rk))

    @classmethod
    def unlock(cls, account_id: str, envelope: dict[str, Any], passphrase: str) -> SyncKeys:
        _validate(envelope)
        kek = _kek(passphrase, _unb64(envelope["salt"]), int(envelope["t"]), int(envelope["m"]), int(envelope["p"]))
        return cls._checked(account_id, _unwrap(kek, envelope["pass"], account_id, "passphrase"), envelope)

    @classmethod
    def recover(cls, account_id: str, envelope: dict[str, Any], recovery_key: str) -> SyncKeys:
        _validate(envelope)
        rk = parse_recovery_key(recovery_key)
        return cls._checked(account_id, _unwrap(_hkdf(rk, "recovery-wrap/v1"), envelope["recovery"], account_id,
                                                "recovery"), envelope)

    @classmethod
    def _checked(cls, account_id: str, adk: bytes, envelope: dict[str, Any]) -> SyncKeys:
        if not hmac.compare_digest(_check(adk), str(envelope.get("check", ""))):
            raise SyncCryptoError("Key check failed")
        return cls(account_id, adk)

    # --- local storage (device settings, sealed on mobile) ---

    def to_local(self) -> dict[str, str]:
        return {"account_id": self.account_id, "adk": _b64(self.adk)}

    @classmethod
    def from_local(cls, raw: dict[str, Any] | None) -> SyncKeys | None:
        try:
            adk = base64.b64decode(raw["adk"])
            return cls(str(raw["account_id"]), adk) if len(adk) == 32 else None
        except (KeyError, TypeError, ValueError):
            return None

    # --- records ---

    def _aad(self, collection: str, record_id: str, version: int) -> bytes:
        return f"iflow-sync/rec/v1|{self.account_id}|{collection}|{record_id}|{version}".encode()

    def seal(self, collection: str, record_id: str, version: int, payload: dict[str, Any]) -> bytes:
        body = zlib.compress(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(), 6)
        framed = struct.pack(">I", len(body)) + body
        framed += b"\0" * (-len(framed) % PAD)
        nonce = os.urandom(12)
        return bytes([FORMAT]) + nonce + AESGCM(self.dek).encrypt(nonce, framed, self._aad(collection, record_id,
                                                                                            version))

    def open(self, collection: str, record_id: str, version: int, blob: bytes) -> dict[str, Any]:
        if len(blob) < 1 + 12 + 16 or blob[0] != FORMAT:
            raise SyncCryptoError("Unknown record format")
        try:
            framed = AESGCM(self.dek).decrypt(blob[1:13], blob[13:], self._aad(collection, record_id, version))
        except InvalidTag as exc:
            raise SyncCryptoError("Record failed authentication") from exc
        (n,) = struct.unpack(">I", framed[:4])
        try:
            return json.loads(zlib.decompress(framed[4:4 + n]))
        except (zlib.error, ValueError) as exc:
            raise SyncCryptoError("Record is corrupt") from exc


def _validate(envelope: dict[str, Any]) -> None:
    if not isinstance(envelope, dict) or envelope.get("v") != FORMAT or envelope.get("kdf") != "argon2id":
        raise SyncCryptoError("This account's sync keys were made by a newer app version; update iFlow Core")
    t, m, p = (int(envelope.get(k, 0)) for k in ("t", "m", "p"))
    if not (1 <= t <= 10 and 8 * 1024 <= m <= 1024 * 1024 and 1 <= p <= 8):
        raise SyncCryptoError("Key envelope has unsafe parameters")          # a hostile server can't DoS/weaken us
