"""
Credentials and tokens.

Passwords   Argon2id (argon2-cffi); a dummy hash is verified for unknown emails so response time
            doesn't reveal whether an account exists.
Codes       6 random digits, stored as HMAC-SHA256(pepper, user|purpose|code). The pepper lives only
            in the service's secrets, so a database dump can't brute-force the small code space.
Access      EdDSA (Ed25519) JWT, 15 minutes: sub=user, did=device, auth_time=last interactive sign-in,
            kid header for key rotation.
Refresh     opaque "<token id>.<secret>" (256-bit secret); the server keeps SHA-256(secret) only.
            Rotated on every use; presenting an already-used token revokes the whole device (theft).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .settings import Settings

MIN_PASSWORD = 10
MAX_PASSWORD = 256


class TokenError(Exception):
    pass


class Passwords:
    def __init__(self, settings: Settings) -> None:
        p = settings.argon2
        self._ph = PasswordHasher(time_cost=p.time_cost, memory_cost=p.memory_cost, parallelism=p.parallelism)
        self._dummy = self._ph.hash(secrets.token_urlsafe(16))

    def hash(self, password: str) -> str:
        return self._ph.hash(password)

    def verify(self, stored: str | None, password: str) -> bool:
        try:
            return self._ph.verify(stored or self._dummy, password) and stored is not None
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, stored: str) -> bool:
        return self._ph.check_needs_rehash(stored)


def password_problem(password: str, email: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters"
    if len(password) > MAX_PASSWORD:
        return f"Use at most {MAX_PASSWORD} characters"
    if password.strip().lower() == email.strip().lower() or password.lower() in email.lower():
        return "Don't use your email address as your password"
    if len(set(password)) < 4:
        return "That password is too easy to guess"
    return None


def new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def code_hash(pepper: bytes, user_id: str, purpose: str, code: str) -> bytes:
    return hmac.new(pepper, f"{user_id}|{purpose}|{code}".encode(), hashlib.sha256).digest()


def codes_match(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


# --- access tokens ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class AccessClaims:
    user_id: str
    device_id: str
    auth_time: datetime
    issued_at: datetime


def issue_access_token(settings: Settings, *, user_id: str, device_id: str, auth_time: datetime,
                       now: datetime) -> str:
    claims = {"iss": settings.issuer, "aud": settings.audience, "sub": user_id, "did": device_id,
              "typ": "access", "iat": int(now.timestamp()), "nbf": int(now.timestamp()) - 5,
              "exp": int((now + timedelta(seconds=settings.access_ttl_s)).timestamp()),
              "auth_time": int(auth_time.timestamp()), "jti": secrets.token_hex(8)}
    return jwt.encode(claims, settings.jwt_private_key, algorithm="EdDSA", headers={"kid": settings.jwt_key_id})


def verify_access_token(settings: Settings, token: str, *, now: datetime) -> AccessClaims:
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError as exc:
        raise TokenError("malformed token") from exc
    key = settings.public_keys.get(kid or "")
    if key is None:
        raise TokenError("unknown signing key")
    try:
        # time claims are checked below against the service clock (injectable), not the wall clock
        claims = jwt.decode(token, key, algorithms=["EdDSA"], audience=settings.audience, issuer=settings.issuer,
                            options={"require": ["exp", "iat", "nbf", "sub", "did", "auth_time"],
                                     "verify_exp": False, "verify_nbf": False, "verify_iat": False})
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if claims.get("typ") != "access":
        raise TokenError("not an access token")
    ts = now.timestamp()
    if not (claims["nbf"] - 5 <= ts < claims["exp"] + 5):
        raise TokenError("token expired or not yet valid")
    return AccessClaims(claims["sub"], claims["did"], datetime.fromtimestamp(claims["auth_time"], timezone.utc),
                        datetime.fromtimestamp(claims["iat"], timezone.utc))


# --- refresh tokens --------------------------------------------------------------------------------


def new_refresh_secret() -> tuple[str, bytes]:
    raw = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode(), hashlib.sha256(raw).digest()


def split_refresh_token(token: str) -> tuple[str, bytes]:
    token_id, _, secret = token.partition(".")
    if not token_id or not secret or len(token) > 200:
        raise TokenError("malformed refresh token")
    try:
        raw = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
    except (ValueError, TypeError) as exc:
        raise TokenError("malformed refresh token") from exc
    return token_id, hashlib.sha256(raw).digest()
