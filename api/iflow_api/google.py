"""
Google sign-in: verify an ID token the app obtained from Google (OAuth 2.0 + PKCE on desktop,
native Google Sign-In on Android/iOS).

Checks: RS256 signature against Google's published keys (cached), issuer, audience is one of our
OAuth client IDs, expiry, and a *verified* email (we link accounts by email, so an unverified
Google email is refused).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol

import jwt

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


class GoogleTokenError(Exception):
    pass


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str


class KeyResolver(Protocol):
    def __call__(self, token: str) -> Any: ...


def jwks_resolver(url: str = GOOGLE_JWKS_URL) -> KeyResolver:
    client = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=10)

    def resolve(token: str) -> Any:
        return client.get_signing_key_from_jwt(token).key

    return resolve


class GoogleVerifier:
    def __init__(self, client_ids: tuple[str, ...], resolve_key: KeyResolver | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.client_ids = client_ids
        self._resolve = resolve_key or jwks_resolver()
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return bool(self.client_ids)

    def verify(self, id_token: str) -> GoogleIdentity:
        if not self.enabled:
            raise GoogleTokenError("Google sign-in isn't configured")
        if not id_token or len(id_token) > 4096:
            raise GoogleTokenError("invalid token")
        try:
            key = self._resolve(id_token)
            claims = jwt.decode(id_token, key, algorithms=["RS256"], audience=list(self.client_ids),
                                options={"require": ["iss", "aud", "exp", "iat", "sub"],
                                         "verify_exp": self._clock is None, "verify_iat": False},
                                leeway=30)
        except (jwt.PyJWTError, jwt.PyJWKClientError, ValueError) as exc:
            raise GoogleTokenError("invalid token") from exc
        if self._clock is not None and claims["exp"] < self._clock().timestamp() - 30:
            raise GoogleTokenError("invalid token")
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise GoogleTokenError("invalid token")
        email = (claims.get("email") or "").strip().lower()
        if not email or claims.get("email_verified") not in (True, "true"):
            raise GoogleTokenError("Google didn't confirm an email address for this account")
        return GoogleIdentity(subject=str(claims["sub"]), email=email)
