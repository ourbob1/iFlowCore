"""
Configuration loader for iFlow Core.

Two layers of configuration live here:

1. **App config** (`AppConfig`) — deployment/runtime settings controlled by
   environment variables: environment name, data directory, database path,
   encryption policy, pool size, sync endpoint.

2. **User preferences** (`Preferences`) — toggles the user changes from the UI
   (unit system, base currency, display currencies, locale). Persisted as a
   small JSON file in the app data directory so they survive restarts and are
   editable on every platform, including Android/iOS sandboxes.

Precedence (lowest → highest):

    built-in defaults  <  preferences.json  <  environment variables

Env overrides for preferences exist so tests and CI can pin behavior without
touching disk; normal users never set them.

All config objects are immutable. To change a preference, build a new one with
`Preferences.with_updates(...)` and persist it with `save_preferences(...)`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, MutableMapping

log = logging.getLogger(__name__)

APP_NAME = "iFlowCore"
ENV_PREFIX = "IFLOW_"
PREFERENCES_FILENAME = "preferences.json"
DEFAULT_DB_FILENAME = "iflowcore.db"
# iFlow-hosted services (Phase 10/11). Both are opt-in from Settings and overridable for self-hosting.
DEFAULT_ACCOUNT_API_URL = "https://api.iflowcore.com"               # E2E-encrypted sync (ciphertext only)
DEFAULT_FORMATS_URL = "https://formats.iflowcore.com/v1/bundle.ifpb"  # signed statement formats (static CDN)
# Before the iFlow Core rebrand (Phase 10) the app was "Ruby Soho". Existing installs keep working:
# RUBYSOHO_* variables are still read, and the old data directory / database file are picked up and
# moved on first launch (migrate_legacy_data).
LEGACY_APP_NAME = "RubySoho"
LEGACY_ENV_PREFIX = "RUBYSOHO_"
LEGACY_DB_FILENAME = "rubysoho.db"
_DB_SIDECARS = ("-wal", "-shm", "-journal")


class ConfigError(ValueError):
    """Raised when a configuration value is missing or invalid."""


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Environment(str, Enum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class UnitSystem(str, Enum):
    """Display unit system. Storage is always metric (meters) — see schema.sql."""

    IMPERIAL = "imperial"
    METRIC = "metric"

    @property
    def distance_unit(self) -> str:
        return "mi" if self is UnitSystem.IMPERIAL else "km"

    @property
    def meters_per_distance_unit(self) -> float:
        return 1609.344 if self is UnitSystem.IMPERIAL else 1000.0

    def to_meters(self, distance: float) -> int:
        """Convert a user-entered distance to canonical integer meters."""
        return round(distance * self.meters_per_distance_unit)

    def from_meters(self, meters: int) -> float:
        """Convert canonical meters to this system's display unit."""
        return meters / self.meters_per_distance_unit


# ---------------------------------------------------------------------------
# Currency support
# ---------------------------------------------------------------------------

# ISO 4217 code -> number of minor-unit digits. Money is stored as integer
# minor units (cents, pence, fils...), so the exponent MUST be known for every
# currency we accept. Extend this table deliberately; unknown codes are
# rejected rather than silently assumed to have 2 decimals.
CURRENCY_MINOR_UNITS: dict[str, int] = {
    "USD": 2, "CAD": 2, "MXN": 2, "BRL": 2, "ARS": 2, "CLP": 0, "COP": 2,
    "EUR": 2, "GBP": 2, "CHF": 2, "SEK": 2, "NOK": 2, "DKK": 2, "PLN": 2,
    "CZK": 2, "HUF": 2, "RON": 2, "TRY": 2,
    "JPY": 0, "KRW": 0, "CNY": 2, "HKD": 2, "TWD": 2, "SGD": 2, "INR": 2,
    "IDR": 2, "PHP": 2, "THB": 2, "VND": 0, "MYR": 2,
    "AUD": 2, "NZD": 2,
    "ZAR": 2, "NGN": 2, "KES": 2, "EGP": 2,
    "AED": 2, "SAR": 2, "ILS": 2, "KWD": 3, "BHD": 3, "JOD": 3, "OMR": 3,
}

_ISO_CODE_RE = re.compile(r"^[A-Z]{3}$")


def normalize_currency(code: str) -> str:
    """Upper-case and validate an ISO 4217 currency code."""
    if not isinstance(code, str):
        raise ConfigError(f"Currency code must be a string, got {type(code).__name__}")
    normalized = code.strip().upper()
    if not _ISO_CODE_RE.match(normalized):
        raise ConfigError(f"'{code}' is not a valid ISO 4217 code (expected 3 letters)")
    if normalized not in CURRENCY_MINOR_UNITS:
        raise ConfigError(
            f"Currency '{normalized}' is not supported yet. "
            "Add it to CURRENCY_MINOR_UNITS with its minor-unit exponent."
        )
    return normalized


def minor_units(code: str) -> int:
    """Number of decimal places for a currency (e.g. USD → 2, JPY → 0)."""
    return CURRENCY_MINOR_UNITS[normalize_currency(code)]


# ---------------------------------------------------------------------------
# Env parsing helpers
# ---------------------------------------------------------------------------

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env(env: Mapping[str, str], name: str) -> str | None:
    """Fetch IFLOW_<name> (or legacy RUBYSOHO_<name>), treating empty strings as unset."""
    for prefix in (ENV_PREFIX, LEGACY_ENV_PREFIX):
        value = env.get(prefix + name)
        if value and value.strip():
            return value.strip()
    return None


def promote_legacy_env(environ: MutableMapping[str, str]) -> list[str]:
    """Copy each RUBYSOHO_<X> to IFLOW_<X> when the new name is unset (modules read IFLOW_* directly).
    Returns the promoted names (never their values)."""
    promoted = []
    for name in [k for k in environ if k.startswith(LEGACY_ENV_PREFIX)]:
        new = ENV_PREFIX + name[len(LEGACY_ENV_PREFIX):]
        if not (environ.get(new) or "").strip() and (environ.get(name) or "").strip():
            environ[new] = environ[name]
            promoted.append(new)
    return promoted


def _parse_bool(raw: str, name: str) -> bool:
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{ENV_PREFIX}{name}='{raw}' is not a boolean (use true/false)")


def _parse_int(raw: str, name: str, *, minimum: int = 1) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{ENV_PREFIX}{name}='{raw}' is not an integer") from exc
    if value < minimum:
        raise ConfigError(f"{ENV_PREFIX}{name} must be >= {minimum}, got {value}")
    return value


def _parse_enum(enum_cls: type[Enum], raw: str, name: str) -> Any:
    try:
        return enum_cls(raw.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(m.value for m in enum_cls)  # type: ignore[attr-defined]
        raise ConfigError(f"{name}='{raw}' is invalid (allowed: {allowed})") from exc


# ---------------------------------------------------------------------------
# User preferences
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Preferences:
    """User-facing toggles. Validated on construction."""

    unit_system: UnitSystem = UnitSystem.IMPERIAL
    base_currency: str = "USD"
    # Currencies the user earns/spends in; base_currency is always included.
    display_currencies: tuple[str, ...] = ("USD",)
    locale: str = "en_US"

    def __post_init__(self) -> None:
        # Coerce + validate. object.__setattr__ is the sanctioned way to
        # normalize fields on a frozen dataclass.
        unit = self.unit_system
        if not isinstance(unit, UnitSystem):
            unit = _parse_enum(UnitSystem, str(unit), "unit_system")
        object.__setattr__(self, "unit_system", unit)

        base = normalize_currency(self.base_currency)
        object.__setattr__(self, "base_currency", base)

        currencies = [normalize_currency(c) for c in self.display_currencies]
        # Base first, then the rest in given order, de-duplicated.
        ordered = tuple(dict.fromkeys([base, *currencies]))
        object.__setattr__(self, "display_currencies", ordered)

        if not re.match(r"^[a-z]{2,3}(_[A-Z]{2})?$", self.locale):
            raise ConfigError(f"locale '{self.locale}' must look like 'en' or 'en_US'")

    def with_updates(self, **changes: Any) -> "Preferences":
        """Return a validated copy with the given fields changed."""
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["unit_system"] = self.unit_system.value
        data["display_currencies"] = list(self.display_currencies)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Preferences":
        """Build from persisted JSON, ignoring (and logging) unknown keys so
        older app versions can read newer preference files."""
        known = set(cls.__dataclass_fields__)
        unknown = set(data) - known
        if unknown:
            log.warning("Ignoring unknown preference keys: %s", sorted(unknown))
        kwargs = {k: v for k, v in data.items() if k in known}
        if "display_currencies" in kwargs:
            kwargs["display_currencies"] = tuple(kwargs["display_currencies"])
        return cls(**kwargs)


def load_preferences(path: Path) -> Preferences:
    """Load preferences from disk; fall back to defaults if absent or corrupt.

    A corrupt file is renamed to `*.corrupt` (not deleted) so it can be
    inspected, and the app keeps running on defaults.
    """
    if not path.exists():
        return Preferences()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ConfigError("preferences root must be a JSON object")
        return Preferences.from_dict(data)
    except (json.JSONDecodeError, ConfigError, TypeError) as exc:
        backup = path.with_suffix(path.suffix + ".corrupt")
        log.error("Invalid preferences file %s (%s); moved to %s", path, exc, backup)
        path.replace(backup)
        return Preferences()


def save_preferences(prefs: Preferences, path: Path) -> None:
    """Persist preferences atomically (write temp file, then rename).

    Atomic replace prevents a half-written file if the app is killed mid-save,
    which is common on mobile.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".prefs-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(prefs.to_dict(), fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppConfig:
    environment: Environment
    debug: bool
    data_dir: Path
    db_path: Path
    require_encryption: bool
    db_pool_size: int
    sync_enabled: bool
    api_base_url: str | None
    preferences_path: Path
    preferences: Preferences = field(default_factory=Preferences)
    platform: str = "linux"          # runtime platform (core.platform.detect_platform)
    ai_service_url: str | None = None  # self-hosted AI / telemetry service (Phase 5); Settings can override
    formats_url: str | None = DEFAULT_FORMATS_URL   # None: statement-format updates off (IFLOW_FORMATS_URL=off)

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def is_mobile(self) -> bool:
        return self.platform in ("android", "ios")

    @property
    def is_web(self) -> bool:
        """Browser build: encrypted at rest by the vault (core/web), not SQLCipher."""
        return self.platform == "web"

    @property
    def requires_sqlcipher(self) -> bool:
        """Desktop/server in a mode that demands encryption. On mobile the
        requirement is met by platform protection + field sealing instead."""
        return self.require_encryption and not self.is_mobile and not self.is_web


def resolve_data_dir(env: Mapping[str, str]) -> Path:
    """Pick the per-user data directory.

    Order: explicit IFLOW_DATA_DIR → Flet's app storage dir (set when
    running as a packaged Flet app, incl. Android/iOS sandboxes) → the OS
    convention via platformdirs (e.g. ~/.local/share/iFlowCore,
    %LOCALAPPDATA%\\iFlowCore, ~/Library/Application Support/iFlowCore).
    """
    explicit = _env(env, "DATA_DIR")
    if explicit:
        return Path(explicit).expanduser()

    flet_dir = env.get("FLET_APP_STORAGE_DATA")
    if flet_dir:
        return Path(flet_dir)

    try:
        from platformdirs import user_data_dir
    except ImportError:  # pragma: no cover - platformdirs is in requirements
        return Path.home() / f".{APP_NAME.lower()}"
    current = Path(user_data_dir(APP_NAME, appauthor=False))
    legacy = Path(user_data_dir(LEGACY_APP_NAME, appauthor=False))
    if not current.exists() and legacy.is_dir():
        return legacy                 # pre-rebrand install not moved yet: keep using it rather than start empty
    return current


def default_db_path(data_dir: Path) -> Path:
    current, legacy = data_dir / DEFAULT_DB_FILENAME, data_dir / LEGACY_DB_FILENAME
    return legacy if legacy.exists() and not current.exists() else current


def migrate_legacy_data(env: Mapping[str, str] | None = None) -> list[str]:
    """One-time move of a pre-rebrand install: the RubySoho data directory → iFlowCore, and
    rubysoho.db (+ WAL/SHM) → iflowcore.db. Runs at startup before the database is opened; it never
    overwrites anything, and on failure the app simply keeps using the legacy location."""
    env = os.environ if env is None else env
    done: list[str] = []
    try:
        if not _env(env, "DATA_DIR") and not env.get("FLET_APP_STORAGE_DATA"):
            from platformdirs import user_data_dir

            current = Path(user_data_dir(APP_NAME, appauthor=False))
            legacy = Path(user_data_dir(LEGACY_APP_NAME, appauthor=False))
            if legacy.is_dir() and not current.exists():
                current.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(legacy), str(current))
                done.append(f"moved data directory {legacy.name} -> {current.name}")
        if not _env(env, "DB_PATH"):
            data_dir = resolve_data_dir(env)
            old, new = data_dir / LEGACY_DB_FILENAME, data_dir / DEFAULT_DB_FILENAME
            sidecars = [Path(f"{p}{s}") for p in (old, new) for s in _DB_SIDECARS]
            # A leftover WAL/journal holds committed pages under the *old* name; renaming the pair
            # non-atomically could separate them. Only rename a cleanly closed database; otherwise
            # default_db_path() keeps using the legacy name.
            if old.exists() and not new.exists() and not any(p.exists() for p in sidecars):
                old.rename(new)
                done.append(f"renamed {LEGACY_DB_FILENAME} -> {DEFAULT_DB_FILENAME}")
    except OSError as exc:
        log.warning("Couldn't migrate pre-rebrand data (%s); using it where it is", type(exc).__name__)
    for step in done:
        log.info("Rebrand migration: %s", step)
    return done


def load_config(env: Mapping[str, str] | None = None) -> AppConfig:
    """Build the immutable AppConfig from the environment + preferences file.

    `env` defaults to `os.environ`; pass a dict in tests for isolation.
    """
    env = os.environ if env is None else env

    raw_env = _env(env, "ENV")
    if not raw_env:
        # Packaged apps default to their build channel (production for shipped builds); source
        # checkouts default to development. See core/buildinfo.py.
        from core.buildinfo import load as load_build_info

        raw_env = load_build_info().default_environment
    environment = (
        _parse_enum(Environment, raw_env, f"{ENV_PREFIX}ENV") if raw_env else Environment.DEVELOPMENT
    )

    raw_debug = _env(env, "DEBUG")
    debug = _parse_bool(raw_debug, "DEBUG") if raw_debug else environment is Environment.DEVELOPMENT

    data_dir = resolve_data_dir(env)
    raw_db = _env(env, "DB_PATH")
    db_path = Path(raw_db).expanduser() if raw_db else default_db_path(data_dir)

    # Encryption is mandatory in production. Dev/test may run plaintext so
    # contributors without a SQLCipher build can still work.
    raw_enc = _env(env, "REQUIRE_ENCRYPTION")
    require_encryption = (
        _parse_bool(raw_enc, "REQUIRE_ENCRYPTION") if raw_enc else environment is Environment.PRODUCTION
    )
    if environment is Environment.PRODUCTION and not require_encryption:
        raise ConfigError("Encryption cannot be disabled in production")

    raw_pool = _env(env, "DB_POOL_SIZE")
    pool_size = _parse_int(raw_pool, "DB_POOL_SIZE") if raw_pool else 4

    api_base_url = _env(env, "API_URL")
    if api_base_url and environment is Environment.PRODUCTION and not api_base_url.startswith("https://"):
        raise ConfigError("IFLOW_API_URL must use https:// in production")

    ai_service_url = _env(env, "AI_URL")
    if ai_service_url:
        from core.ai.client import AIServiceError, validate_service_url

        try:
            ai_service_url = validate_service_url(ai_service_url,
                                                  allow_http=environment is not Environment.PRODUCTION)
        except AIServiceError as exc:
            raise ConfigError(f"{ENV_PREFIX}AI_URL: {exc}") from exc

    formats_url: str | None = DEFAULT_FORMATS_URL
    raw_formats = _env(env, "FORMATS_URL")
    if raw_formats:
        if raw_formats.strip().lower() in ("off", "none", "false", "0"):
            formats_url = None
        else:
            local = raw_formats.startswith(("http://localhost", "http://127.0.0.1"))
            if not (raw_formats.startswith("https://") or (local and environment is not Environment.PRODUCTION)):
                raise ConfigError(f"{ENV_PREFIX}FORMATS_URL must use https:// (http://localhost in development)")
            formats_url = raw_formats.strip()

    raw_sync = _env(env, "SYNC_ENABLED")
    sync_enabled = _parse_bool(raw_sync, "SYNC_ENABLED") if raw_sync else api_base_url is not None
    if sync_enabled and not api_base_url:
        raise ConfigError("IFLOW_SYNC_ENABLED is true but IFLOW_API_URL is not set")

    prefs_path = data_dir / PREFERENCES_FILENAME
    prefs = _apply_preference_env_overrides(load_preferences(prefs_path), env)

    from core.platform import PlatformError, detect_platform

    try:
        runtime_platform = detect_platform(env)
    except PlatformError as exc:
        raise ConfigError(str(exc)) from exc

    return AppConfig(
        environment=environment,
        debug=debug,
        data_dir=data_dir,
        db_path=db_path,
        require_encryption=require_encryption,
        db_pool_size=pool_size,
        sync_enabled=sync_enabled,
        api_base_url=api_base_url,
        preferences_path=prefs_path,
        preferences=prefs,
        platform=runtime_platform,
        ai_service_url=ai_service_url,
        formats_url=formats_url,
    )


def _apply_preference_env_overrides(prefs: Preferences, env: Mapping[str, str]) -> Preferences:
    changes: dict[str, Any] = {}
    if units := _env(env, "UNITS"):
        changes["unit_system"] = _parse_enum(UnitSystem, units, f"{ENV_PREFIX}UNITS")
    if currency := _env(env, "CURRENCY"):
        changes["base_currency"] = currency
    if currencies := _env(env, "CURRENCIES"):
        changes["display_currencies"] = tuple(c for c in currencies.split(",") if c.strip())
    if locale := _env(env, "LOCALE"):
        changes["locale"] = locale
    return prefs.with_updates(**changes) if changes else prefs
