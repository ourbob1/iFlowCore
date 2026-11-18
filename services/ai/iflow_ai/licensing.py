"""
License signing for the admin portal (Phase 11).

The app verifies licenses offline (core/licensing/tokens.py). This module produces the exact same
format, so a token issued here verifies there with nothing but the vendor *public* key:

    RSL1.<base64url(compact JSON payload)>.<base64url(Ed25519 signature over "RSL1.<payload>")>

The service is deployed on its own (it doesn't ship the app's code), so the format and price book
are restated here; tests/test_admin.py checks both against core/licensing byte for byte.
Money is integer minor units (cents) throughout.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

PREFIX = "RSL1"
ISSUER = "vendor"
GRACE_DAYS = 7
TIERS = {"pro": "iFlow Pro", "guild": "iFlow Health & Guild"}
PERIOD_MONTHS = {"monthly": 1, "annual": 12}
# (tier, period, currency) → price in cents. Must equal core.licensing.pricing.PRICE_BOOK.
PRICE_BOOK: dict[tuple[str, str, str], int] = {
    ("pro", "monthly", "USD"): 699,
    ("pro", "annual", "USD"): 6000,
    ("guild", "monthly", "USD"): 1499,
}
# The app's public "simulation" key seed: anyone can derive it, so it must never sign real licenses.
_SIMULATION_SEED = hashlib.sha256(b"ruby-soho/license-simulation/v1 - not secret, never trusted in production").digest()


class LicenseIssueError(ValueError):
    pass


def load_signing_key(raw: str) -> Ed25519PrivateKey:
    """RS_AI_LICENSE_SIGNING_KEY: 64 hex characters (the raw 32-byte Ed25519 seed)."""
    try:
        seed = bytes.fromhex(raw.strip())
    except ValueError as exc:
        raise LicenseIssueError("License signing key must be 64 hex characters") from exc
    if len(seed) != 32:
        raise LicenseIssueError("License signing key must be 64 hex characters (32 bytes)")
    if seed == _SIMULATION_SEED:
        raise LicenseIssueError("That is the public simulation key; generate a real one")
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_key_hex(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def add_months(dt: datetime, months: int) -> datetime:
    """Same day next month(s), clamped to the month's last day (Jan 31 + 1 month → Feb 28/29)."""
    total = dt.month - 1 + months
    year, month = dt.year + total // 12, total % 12 + 1
    for day in (dt.day, 30, 29, 28):
        try:
            return dt.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    raise AssertionError("unreachable")


def price(tier: str, period: str, currency: str = "USD") -> int:
    try:
        return PRICE_BOOK[(tier, period, currency.upper())]
    except KeyError:
        raise LicenseIssueError(f"{TIERS.get(tier, tier)} isn't offered {period} in {currency.upper()}") from None


@dataclass(frozen=True)
class Issued:
    token: str
    license_id: str
    tier: str
    period: str
    terms: int
    currency: str
    amount_minor: int
    issued_at: datetime
    expires_at: datetime


def issue(key: Ed25519PrivateKey, *, tier: str, period: str, terms: int = 1, currency: str = "USD",
          now: datetime | None = None, complimentary: bool = False) -> Issued:
    if tier not in TIERS:
        raise LicenseIssueError("Tier must be 'pro' or 'guild' (Solo Core is free and needs no license)")
    if period not in PERIOD_MONTHS:
        raise LicenseIssueError("Period must be 'monthly' or 'annual'")
    if isinstance(terms, bool) or not isinstance(terms, int) or not 1 <= terms <= 36:
        raise LicenseIssueError("Terms must be a whole number from 1 to 36")
    unit = price(tier, period, currency)
    amount = 0 if complimentary else unit * terms                  # integer cents, never floats
    issued = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    expires = add_months(issued, PERIOD_MONTHS[period] * terms)
    lid = str(uuid.uuid4())
    payload = {"v": 1, "lid": lid, "iss": ISSUER, "tier": tier, "per": period, "iat": int(issued.timestamp()),
               "exp": int(expires.timestamp()), "grace": GRACE_DAYS, "ccy": currency.upper(), "amt": amount}
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = key.sign(f"{PREFIX}.{body}".encode("ascii"))
    return Issued(f"{PREFIX}.{body}.{_b64e(signature)}", lid, tier, period, terms, currency.upper(), amount,
                  issued, expires)


def inspect(token: str, public_hex: str) -> dict:
    """Decode and check a token against this server's key (for the portal's verify tool)."""
    token = "".join((token or "").split())
    parts = token.split(".")
    if len(token) > 2048 or len(parts) != 3 or parts[0] != PREFIX:
        raise LicenseIssueError("That isn't an iFlow Core license key")
    try:
        payload = json.loads(_b64d(parts[1]))
        signature = _b64d(parts[2])
    except (ValueError, UnicodeDecodeError) as exc:
        raise LicenseIssueError("License key is damaged") from exc
    if not isinstance(payload, dict):
        raise LicenseIssueError("License key is damaged")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(signature, f"{PREFIX}.{parts[1]}".encode())
        valid = True
    except (InvalidSignature, ValueError):
        valid = False
    return {"signature_valid": valid, "license_id": payload.get("lid"), "issuer": payload.get("iss"),
            "tier": payload.get("tier"), "period": payload.get("per"),
            "issued_at": _iso(payload.get("iat")), "expires_at": _iso(payload.get("exp")),
            "currency": payload.get("ccy"), "amount_minor": payload.get("amt")}


def _iso(ts: object) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
