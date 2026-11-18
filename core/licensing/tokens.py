"""
Offline license tokens (Ed25519).

    RSL1.<base64url payload>.<base64url signature>

The payload is compact JSON; the signature covers the ASCII bytes of
"RSL1.<payload>", so there is no canonicalization to get wrong. Verification
needs only the issuer's *public* key, bundled with the app, so checking a
license never touches the network and never reports anything anywhere.

Issuers
-------
  vendor      real purchases. Trusted public keys come from
              `IFLOW_LICENSE_PUBKEYS` (comma-separated hex) until the
              production key is pinned in `VENDOR_PUBLIC_KEYS`.
  simulation  the in-app "simulated upgrade". Its signing key is derived from
              a public seed (so it is NOT secret), which is why production
              builds refuse simulation tokens outright.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from core.licensing.pricing import Period
from core.licensing.tiers import Tier

PREFIX = "RSL1"
MAX_TOKEN_CHARS = 2048
ENV_PUBKEYS = "IFLOW_LICENSE_PUBKEYS"
DEFAULT_GRACE_DAYS = 7

# Production vendor keys (hex, raw 32-byte Ed25519): licenses (RSL1) and statement formats (IFPB1).
# The private half lives only with the operator (Admin Console: ~/.local/share/iflow-ai/vendor-signing.key,
# created 2026-09-27). To rotate: add the new key here, ship, and remove the old one once nothing uses it.
VENDOR_PUBLIC_KEYS: tuple[str, ...] = (
    "e136d98cc20d781b2c8be45a4d404e0c04ff454899e49e0bbc1700f9bdb2d35c",
)

_SIMULATION_SEED = hashlib.sha256(b"ruby-soho/license-simulation/v1 - not secret, never trusted in production").digest()


class LicenseError(ValueError):
    """A token is malformed, forged, for another issuer, or otherwise unusable."""


@dataclass(frozen=True)
class LicenseClaims:
    license_id: str
    issuer: str                     # vendor | simulation
    tier: Tier
    period: Period
    issued_at: datetime
    expires_at: datetime
    grace_days: int = DEFAULT_GRACE_DAYS
    currency: str = "USD"
    amount_minor: int = 0           # price paid for this term (display / proration only)

    def to_payload(self) -> dict:
        return {
            "v": 1, "lid": self.license_id, "iss": self.issuer, "tier": self.tier.value,
            "per": self.period.value, "iat": int(self.issued_at.timestamp()), "exp": int(self.expires_at.timestamp()),
            "grace": self.grace_days, "ccy": self.currency, "amt": self.amount_minor,
        }

    @classmethod
    def from_payload(cls, p: Mapping) -> "LicenseClaims":
        try:
            if p.get("v") != 1:
                raise LicenseError("Unsupported license version")
            claims = cls(
                license_id=str(uuid.UUID(str(p["lid"]))), issuer=str(p["iss"]), tier=Tier(p["tier"]),
                period=Period(p["per"]),
                issued_at=datetime.fromtimestamp(int(p["iat"]), tz=timezone.utc),
                expires_at=datetime.fromtimestamp(int(p["exp"]), tz=timezone.utc),
                grace_days=int(p.get("grace", DEFAULT_GRACE_DAYS)), currency=str(p.get("ccy", "USD")),
                amount_minor=int(p.get("amt", 0)),
            )
        except LicenseError:
            raise
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            raise LicenseError("License payload is incomplete or invalid") from exc
        if claims.tier is Tier.SOLO:
            raise LicenseError("Solo Core doesn't need a license")
        if claims.expires_at <= claims.issued_at:
            raise LicenseError("License expires before it was issued")
        if not 0 <= claims.grace_days <= 31:
            raise LicenseError("License grace period is out of range")
        if isinstance(p.get("amt"), bool) or claims.amount_minor < 0:
            raise LicenseError("License amount is invalid")
        return claims


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def public_key_hex(key: Ed25519PublicKey) -> str:
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def simulation_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_SIMULATION_SEED)


def simulation_public_key_hex() -> str:
    return public_key_hex(simulation_private_key().public_key())


def sign(claims: LicenseClaims, private_key: Ed25519PrivateKey) -> str:
    payload = _b64e(json.dumps(claims.to_payload(), separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signed = f"{PREFIX}.{payload}".encode("ascii")
    return f"{PREFIX}.{payload}.{_b64e(private_key.sign(signed))}"


def trusted_keys(issuer: str, *, allow_simulation: bool, env: Mapping[str, str] | None = None) -> list[str]:
    if issuer == "simulation":
        return [simulation_public_key_hex()] if allow_simulation else []
    if issuer == "vendor":
        env = os.environ if env is None else env
        extra = [k.strip().lower() for k in env.get(ENV_PUBKEYS, "").split(",") if k.strip()]
        return [*VENDOR_PUBLIC_KEYS, *extra]
    return []


def verify(token: str, *, allow_simulation: bool, env: Mapping[str, str] | None = None,
           keys: Iterable[str] | None = None) -> LicenseClaims:
    """Check the signature and shape of a token. Expiry is judged by the caller (state.py)."""
    token = "".join((token or "").split())                 # tolerate pasted line breaks / spaces
    if not token:
        raise LicenseError("Enter a license key")
    if len(token) > MAX_TOKEN_CHARS:
        raise LicenseError("That license key is too long")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise LicenseError("That doesn't look like a iFlow Core license key")
    try:
        payload = json.loads(_b64d(parts[1]))
        signature = _b64d(parts[2])
    except (ValueError, UnicodeDecodeError) as exc:
        raise LicenseError("License key is damaged") from exc
    if not isinstance(payload, dict):
        raise LicenseError("License key is damaged")
    issuer = str(payload.get("iss", ""))
    candidates = list(keys) if keys is not None else trusted_keys(issuer, allow_simulation=allow_simulation, env=env)
    if not candidates:
        if issuer == "simulation":
            raise LicenseError("Simulated licenses only work in development builds")
        raise LicenseError("This build can't verify licenses from that issuer")
    signed = f"{PREFIX}.{parts[1]}".encode("ascii")
    for hex_key in candidates:
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(hex_key)).verify(signature, signed)
        except (InvalidSignature, ValueError):
            continue
        return LicenseClaims.from_payload(payload)
    raise LicenseError("License key signature is invalid")
