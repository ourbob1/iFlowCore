"""Service configuration from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class SettingsError(ValueError):
    pass


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise SettingsError(f"{name}={raw!r} is not a boolean")


def _int(env: Mapping[str, str], name: str, default: int, minimum: int = 1) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SettingsError(f"{name}={raw!r} is not an integer") from exc
    if value < minimum:
        raise SettingsError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class Settings:
    tokens: tuple[str, ...]              # client bearer tokens (one per device/install is best)
    admin_token: str | None              # legacy single admin token (RS_AI_ADMIN_TOKEN); see admin_tokens
    data_dir: Path                       # telemetry database lives here
    receipt_backend: str                 # ocr_llm | vlm | off | fake
    tax_backend: str                     # llm | off | fake
    llm_model: str
    llm_revision: str | None
    vlm_model: str
    vlm_revision: str | None
    ocr_languages: str
    preload: bool
    max_concurrency: int
    queue_timeout_s: int
    max_upload_bytes: int
    max_new_tokens: int
    rate_receipts_per_min: int
    rate_tax_per_min: int
    rate_events_per_min: int
    telemetry_retention_days: int
    docs_enabled: bool
    # Phase 11 admin portal. Admin tokens are for operators only (never shipped in the app).
    admin_tokens: tuple[str, ...] = ()   # RS_AI_ADMIN_TOKENS (comma-separated) + RS_AI_ADMIN_TOKEN
    license_signing_key: str | None = None  # RS_AI_LICENSE_SIGNING_KEY: 64 hex chars; enables license issuing
    admin_failures_per_10min: int = 10   # failed admin logins allowed per client IP

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        tokens = tuple(t.strip() for t in env.get("RS_AI_TOKENS", "").split(",") if t.strip())
        if not tokens:
            raise SettingsError("RS_AI_TOKENS is empty: generate one with `openssl rand -hex 32`")
        weak = [t for t in tokens if len(t) < 32]
        if weak:
            raise SettingsError("Every RS_AI_TOKENS entry must be at least 32 characters")
        admin = env.get("RS_AI_ADMIN_TOKEN", "").strip() or None
        if admin is not None and (len(admin) < 32 or admin in tokens):
            raise SettingsError("RS_AI_ADMIN_TOKEN must be 32+ characters and differ from client tokens")
        admins = tuple(dict.fromkeys(
            [*(t.strip() for t in env.get("RS_AI_ADMIN_TOKENS", "").split(",") if t.strip()), *([admin] if admin else [])]))
        if any(len(t) < 32 or t in tokens for t in admins):
            raise SettingsError("RS_AI_ADMIN_TOKENS entries must be 32+ characters and differ from client tokens")
        signing = env.get("RS_AI_LICENSE_SIGNING_KEY", "").strip() or None
        if signing is not None:
            from .licensing import LicenseIssueError, load_signing_key

            try:
                load_signing_key(signing)
            except LicenseIssueError as exc:
                raise SettingsError(f"RS_AI_LICENSE_SIGNING_KEY: {exc}") from None
        receipt_backend = env.get("RS_AI_RECEIPT_BACKEND", "ocr_llm").strip().lower()
        if receipt_backend not in ("ocr_llm", "vlm", "off", "fake"):
            raise SettingsError("RS_AI_RECEIPT_BACKEND must be ocr_llm, vlm, off or fake")
        tax_backend = env.get("RS_AI_TAX_BACKEND", "llm").strip().lower()
        if tax_backend not in ("llm", "off", "fake"):
            raise SettingsError("RS_AI_TAX_BACKEND must be llm, off or fake")
        return cls(
            tokens=tokens,
            admin_token=admin,
            data_dir=Path(env.get("RS_AI_DATA_DIR", "/data")),
            receipt_backend=receipt_backend,
            tax_backend=tax_backend,
            llm_model=env.get("RS_AI_LLM_MODEL", "Qwen/Qwen2.5-1.5B-Instruct").strip(),
            llm_revision=env.get("RS_AI_LLM_REVISION", "").strip() or None,
            vlm_model=env.get("RS_AI_VLM_MODEL", "Qwen/Qwen2.5-VL-7B-Instruct").strip(),
            vlm_revision=env.get("RS_AI_VLM_REVISION", "").strip() or None,
            ocr_languages=env.get("RS_AI_OCR_LANGS", "eng").strip() or "eng",
            preload=_bool(env, "RS_AI_PRELOAD", True),
            max_concurrency=_int(env, "RS_AI_MAX_CONCURRENCY", 1),
            queue_timeout_s=_int(env, "RS_AI_QUEUE_TIMEOUT_S", 90),
            max_upload_bytes=_int(env, "RS_AI_MAX_UPLOAD_MB", 25) * 1024 * 1024,
            max_new_tokens=_int(env, "RS_AI_MAX_NEW_TOKENS", 384, minimum=16),
            rate_receipts_per_min=_int(env, "RS_AI_RATE_RECEIPTS", 20),
            rate_tax_per_min=_int(env, "RS_AI_RATE_TAX", 20),
            rate_events_per_min=_int(env, "RS_AI_RATE_EVENTS", 30),
            telemetry_retention_days=_int(env, "RS_AI_TELEMETRY_RETENTION_DAYS", 90),
            docs_enabled=_bool(env, "RS_AI_DOCS", False),
            admin_tokens=admins,
            license_signing_key=signing,
            admin_failures_per_10min=_int(env, "RS_AI_ADMIN_FAILURES_PER_10MIN", 10),
        )
