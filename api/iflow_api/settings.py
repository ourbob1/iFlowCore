"""
Service configuration from environment variables (IFLOW_API_*).

    IFLOW_API_ENV                 dev | test | production (default dev)
    IFLOW_API_DATABASE_URL        postgresql+psycopg://user:pass@host/db (dev default: local SQLite file)
    IFLOW_API_DATABASE_SECRET     alternative on AWS: the RDS-generated secret JSON (username, password,
                                  host, port, dbname), injected by ECS from Secrets Manager. The URL is
                                  built here, with TLS required, so the password is never copied anywhere.
    IFLOW_API_JWT_PRIVATE_KEY     Ed25519 private key, PEM (signs access tokens)
    IFLOW_API_JWT_KEY_ID          key id for rotation (default "k1")
    IFLOW_API_JWT_PREVIOUS_KEYS   earlier public keys still accepted: "kid=PEM;kid=PEM" (rotation window)
    IFLOW_API_CODE_PEPPER         32+ random bytes, hex; HMAC key for email codes (never stored with them)
    IFLOW_API_GOOGLE_CLIENT_IDS   comma-separated OAuth client IDs accepted as `aud` (Google sign-in off if empty)
    IFLOW_API_MAIL_BACKEND        console (dev only) | ses
    IFLOW_API_MAIL_FROM           sender address, e.g. "iFlow Core <no-reply@example.com>"
    IFLOW_API_SES_REGION          AWS region for SES
    IFLOW_API_TRUST_PROXY         true when behind a load balancer that sets X-Forwarded-For (AWS ALB)
    IFLOW_API_INTEGRATION_CLIENTS JSON {integration id: {token_url, client_id, client_secret}} for the OAuth
                                  token broker (platforms that require a client secret, e.g. Uber)
    IFLOW_API_CORS_ORIGINS        browser origins allowed to call the API, e.g. "https://iflowcore.com"
                                  (the web app); comma-separated, https only (http://localhost in dev)

In production every secret must be supplied (AWS Secrets Manager → environment); the service
refuses to start with development defaults, SQLite, or the console mailer.
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PREFIX = "IFLOW_API_"
ENVIRONMENTS = ("dev", "test", "production")


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Argon2Params:
    time_cost: int = 3
    memory_cost: int = 64 * 1024          # KiB
    parallelism: int = 2


@dataclass(frozen=True)
class Settings:
    environment: str
    database_url: str
    jwt_private_key: Ed25519PrivateKey
    jwt_key_id: str
    jwt_previous_keys: Mapping[str, Ed25519PublicKey] = field(default_factory=dict)
    code_pepper: bytes = b""
    google_client_ids: tuple[str, ...] = ()
    mail_backend: str = "console"
    mail_from: str = "iFlow Core <no-reply@localhost>"
    ses_region: str | None = None
    trust_proxy: bool = False
    cors_origins: tuple[str, ...] = ()
    integration_clients: Mapping[str, Any] = field(default_factory=dict)    # broker.BrokerClient by id
    argon2: Argon2Params = Argon2Params()
    issuer: str = "iflow-core-api"
    audience: str = "iflow-core"
    access_ttl_s: int = 15 * 60
    refresh_ttl_s: int = 60 * 24 * 3600
    code_ttl_s: int = 15 * 60
    fresh_auth_s: int = 10 * 60           # account deletion needs a sign-in this recent

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def public_keys(self) -> dict[str, Ed25519PublicKey]:
        return {self.jwt_key_id: self.jwt_private_key.public_key(), **self.jwt_previous_keys}

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        def get(name: str, default: str | None = None) -> str | None:
            value = (env.get(PREFIX + name) or "").strip()
            return value or default

        environment = get("ENV", "dev")
        if environment not in ENVIRONMENTS:
            raise SettingsError(f"{PREFIX}ENV must be one of {ENVIRONMENTS}")
        prod = environment == "production"

        database_url = get("DATABASE_URL")
        secret = get("DATABASE_SECRET")
        if secret:
            if database_url:
                raise SettingsError(f"Set {PREFIX}DATABASE_URL or {PREFIX}DATABASE_SECRET, not both")
            database_url = database_url_from_secret(secret)
        if not database_url:
            if prod:
                raise SettingsError(f"{PREFIX}DATABASE_URL is required in production")
            database_url = "sqlite:///./iflow_api_dev.db"
        if prod and database_url.startswith("sqlite"):
            raise SettingsError("SQLite is for development only; production needs PostgreSQL")

        pem = get("JWT_PRIVATE_KEY")
        if pem:
            key = _load_private_key(pem)
        elif prod:
            raise SettingsError(f"{PREFIX}JWT_PRIVATE_KEY is required in production")
        else:
            key = Ed25519PrivateKey.generate()          # dev: tokens die with the process
        previous = {}
        for item in filter(None, (get("JWT_PREVIOUS_KEYS") or "").split(";")):
            kid, _, pub_pem = item.partition("=")
            previous[kid.strip()] = _load_public_key(pub_pem.strip())

        pepper_hex = get("CODE_PEPPER")
        if pepper_hex:
            try:
                pepper = bytes.fromhex(pepper_hex)
            except ValueError as exc:
                raise SettingsError(f"{PREFIX}CODE_PEPPER must be hex") from exc
            if len(pepper) < 32:
                raise SettingsError(f"{PREFIX}CODE_PEPPER must be at least 32 bytes")
        elif prod:
            raise SettingsError(f"{PREFIX}CODE_PEPPER is required in production")
        else:
            pepper = secrets.token_bytes(32)

        mail_backend = get("MAIL_BACKEND", "ses" if prod else "console")
        if mail_backend not in ("console", "ses", "memory"):
            raise SettingsError(f"{PREFIX}MAIL_BACKEND must be console or ses")
        if prod and mail_backend != "ses":
            raise SettingsError("production must send email through SES (the console mailer logs codes)")
        mail_from = get("MAIL_FROM", "iFlow Core <no-reply@localhost>")
        if prod and mail_from.endswith("@localhost>"):
            raise SettingsError(f"{PREFIX}MAIL_FROM is required in production")

        origins = tuple(o.strip().rstrip("/") for o in (get("CORS_ORIGINS") or "").split(",") if o.strip())
        for origin in origins:
            local = origin.startswith(("http://localhost", "http://127.0.0.1"))
            if origin == "*" or not (origin.startswith("https://") or (local and not prod)):
                raise SettingsError(f"{PREFIX}CORS_ORIGINS must list exact https origins (got {origin!r})")

        return cls(
            environment=environment, cors_origins=origins, database_url=database_url, jwt_private_key=key,
            jwt_key_id=get("JWT_KEY_ID", "k1"), jwt_previous_keys=previous, code_pepper=pepper,
            google_client_ids=tuple(c.strip() for c in (get("GOOGLE_CLIENT_IDS") or "").split(",") if c.strip()),
            mail_backend=mail_backend, mail_from=mail_from, ses_region=get("SES_REGION"),
            trust_proxy=(get("TRUST_PROXY", "false") or "").lower() in ("1", "true", "yes"),
            integration_clients=_integration_clients(get("INTEGRATION_CLIENTS")),
        )


def _integration_clients(raw: str | None) -> dict:
    from .broker import BrokerConfigError, parse_clients

    try:
        return parse_clients(raw)
    except BrokerConfigError as exc:
        raise SettingsError(str(exc)) from None


def database_url_from_secret(raw: str) -> str:
    """postgresql+psycopg URL from an RDS secret ({"username", "password", "host", "port", "dbname"})."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise SettingsError(f"{PREFIX}DATABASE_SECRET must be the RDS secret JSON") from None
    if not isinstance(data, dict):
        raise SettingsError(f"{PREFIX}DATABASE_SECRET must be the RDS secret JSON")
    missing = [k for k in ("username", "password", "host") if not data.get(k)]
    if missing:
        raise SettingsError(f"{PREFIX}DATABASE_SECRET is missing {', '.join(missing)}")
    if data.get("engine", "postgres") not in ("postgres", "postgresql"):
        raise SettingsError(f"{PREFIX}DATABASE_SECRET is not a PostgreSQL secret")
    quote = lambda v: urllib.parse.quote(str(v), safe="")    # noqa: E731 - passwords hold @ : / % …
    port = int(data.get("port") or 5432)
    dbname = quote(data.get("dbname") or "postgres")
    return (f"postgresql+psycopg://{quote(data['username'])}:{quote(data['password'])}@{data['host']}:{port}/"
            f"{dbname}?sslmode=require")


def _load_private_key(pem: str) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(pem.replace("\\n", "\n").encode(), password=None)
    except ValueError as exc:
        raise SettingsError(f"{PREFIX}JWT_PRIVATE_KEY is not a valid PEM private key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SettingsError(f"{PREFIX}JWT_PRIVATE_KEY must be an Ed25519 key")
    return key


def _load_public_key(pem: str) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(pem.replace("\\n", "\n").encode())
    except ValueError as exc:
        raise SettingsError(f"{PREFIX}JWT_PREVIOUS_KEYS contains an invalid public key") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise SettingsError(f"{PREFIX}JWT_PREVIOUS_KEYS must contain Ed25519 keys")
    return key


def generate_keypair_pem() -> tuple[str, str]:
    """(private PEM, public PEM) for a new signing key: `python -m iflow_api.keys`."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public
