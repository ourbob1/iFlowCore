"""
Per-device settings stored inside the encrypted database (`device_settings`).

Anything sensitive or device-specific lives here rather than in
preferences.json: home location, work zones, feed URLs, API credentials the
user types in, motion-sentry switches. Values are JSON; `DeviceSettings`
gives them types and defaults.

Sensitive fields (`SENSITIVE_FIELDS`) are additionally sealed with the device
key (AES-256-GCM, see core.crypto) when a sealer is supplied. On mobile
"platform" storage, where the database file itself isn't encrypted, the
store is created with `require_sealing=True` and refuses to write those
fields in the clear.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any


def _android_timezone() -> str | None:
    """Android keeps the zone in a system property (there's no /etc/localtime)."""
    import subprocess

    for cmd in (["getprop", "persist.sys.timezone"], ["/system/bin/getprop", "persist.sys.timezone"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if "/" in out:
            return out
    return None


def _browser_timezone() -> str | None:
    try:
        import js  # type: ignore[import-not-found]  # Pyodide (web build)

        tz = str(js.Intl.DateTimeFormat.new().resolvedOptions().timeZone or "")
        return tz if "/" in tz else None
    except Exception:
        return None


def guess_timezone() -> str:
    """Best-effort IANA zone for first run: $TZ, Android's system property, the browser's zone,
    then /etc/localtime; else UTC. Only returns zones zoneinfo can load."""
    from zoneinfo import ZoneInfo

    def usable(name: str | None) -> str | None:
        try:
            return name if name and ZoneInfo(name) else None
        except (ValueError, KeyError, OSError):
            return None

    import sys

    tz = os.environ.get("TZ", "").strip().lstrip(":")
    if "/" in tz and usable(tz):
        return tz
    if hasattr(sys, "getandroidapilevel") or os.environ.get("FLET_PLATFORM") == "android":
        found = usable(_android_timezone())
        if found:
            return found
    if sys.platform == "emscripten":
        found = usable(_browser_timezone())
        if found:
            return found
    try:
        target = Path("/etc/localtime").resolve()
        parts = target.parts
        if "zoneinfo" in parts:
            found = usable("/".join(parts[parts.index("zoneinfo") + 1:]))
            if found:
                return found
    except OSError:
        pass
    return "UTC"


@dataclass(frozen=True)
class DeviceSettings:
    timezone: str = "UTC"
    # Motion sentry
    sentry_enabled: bool = False
    sentry_background: bool = False
    disclosure_accepted: bool = False
    start_speed_mph: int = 10
    shift_active: bool = False
    shift_platform_id: str | None = None
    home: dict[str, float] | None = None                  # {"lat", "lng", "radius_m"}
    work_zones: list[dict[str, Any]] = field(default_factory=list)   # [{"name","lat","lng","radius_m"}]
    work_hours: list[dict[str, Any]] = field(default_factory=list)   # [{"weekdays":[0..6],"start":"HH:MM","end":"HH:MM"}]
    # Demand radar
    radar_airport_icao: str = "KTPA"
    radar_center: dict[str, float] | None = None           # defaults to the airport
    radar_radius_miles: int = 25
    ics_feeds: list[dict[str, str]] = field(default_factory=list)     # [{"name","url"}]
    ticketmaster_api_key: str | None = None
    opensky_client_id: str | None = None
    opensky_client_secret: str | None = None
    # Dashboard
    monthly_profit_goal_minor: int | None = None
    # Self-hosted AI service & diagnostics (Phase 5). Every feature that sends
    # data off the device is off until the user turns it on.
    ai_service_url: str | None = None
    ai_service_token: str | None = None
    ai_receipts_enabled: bool = False
    ai_tax_enabled: bool = False
    telemetry_enabled: bool = False
    # Personal Intelligence (Phase 6): remember coarse trip-end areas (~1 km cells, never coordinates, never
    # home) for the area heatmap. Opt-in: the trip-detection disclosure promises only distance and times.
    insights_zones_enabled: bool = False
    # Licensing (Phase 7): signed offline license token, verified locally; never sent anywhere.
    license_token: str | None = None
    license_last_seen: str | None = None     # clock-rollback guard (ISO UTC)
    # iFlow Health (Phase 8). Profile and brokerage tokens are sealed on mobile; no medical data exists here.
    health_profile: dict[str, Any] | None = None           # MemberProfile.to_dict()
    health_broker_tokens: dict[str, str] = field(default_factory=dict)
    health_withholding: dict[str, Any] | None = None       # WithholdingPolicy.to_dict()
    # Account (Phase 10): api_url, user_id, email, device_id, refresh_token. Sealed on mobile; never synced.
    account_session: dict[str, Any] | None = None
    # Sync (Phase 10.3): the account data key, so the passphrase is needed once per device. Sealed on mobile.
    sync_keys: dict[str, Any] | None = None
    sync_enabled: bool = False
    # Statement formats (Phase 11): signed bundles from the iFlow CDN. Only an anonymous GET; on by default,
    # can be turned off. The ETag avoids re-downloading an unchanged file.
    # Platform API connections (Phase 11): {schema_id: {"values", "secrets", "credentials", "platform_id",
    # "last_pull_at", "last_result"}}. Holds OAuth tokens and pasted API tokens: sealed on mobile, inside
    # SQLCipher on desktop, never synced.
    integration_credentials: dict[str, Any] = field(default_factory=dict)
    formats_auto_update: bool = True
    formats_checked_at: str | None = None
    formats_etag: str | None = None


_FIELDS = {f.name for f in fields(DeviceSettings)}
SENSITIVE_FIELDS = frozenset({
    "home", "work_zones", "ics_feeds", "radar_center",
    "ticketmaster_api_key", "opensky_client_id", "opensky_client_secret",
    "ai_service_url", "ai_service_token", "license_token", "health_profile", "health_broker_tokens",
    "account_session", "sync_keys", "integration_credentials",
})
_DEFAULTS = asdict(DeviceSettings())


class SettingsError(RuntimeError):
    pass


class SettingsStore:
    def __init__(self, db: Any, *, sealer: Any | None = None, require_sealing: bool = False) -> None:
        self.db = db
        self.sealer = sealer
        self.require_sealing = require_sealing
        self.unreadable: set[str] = set()      # sealed fields we couldn't open (no/wrong key)

    def _context(self, key: str) -> str:
        return f"device_settings.{key}"

    def _decode(self, key: str, raw: str) -> Any:
        value = json.loads(raw)
        if isinstance(value, dict) and set(value) == {"sealed"}:
            from core.crypto import SealingError

            if self.sealer is None:
                self.unreadable.add(key)
                return _DEFAULTS[key]
            try:
                return json.loads(self.sealer.open(value["sealed"], context=self._context(key)))
            except SealingError:
                self.unreadable.add(key)
                return _DEFAULTS[key]
        return value

    def _encode(self, key: str, value: Any) -> str:
        plain = json.dumps(value)
        if key in SENSITIVE_FIELDS and value != _DEFAULTS[key]:
            if self.sealer is not None:
                return json.dumps({"sealed": self.sealer.seal(plain, context=self._context(key))})
            if self.require_sealing:
                raise SettingsError(f"Refusing to store '{key}' unsealed: device key unavailable")
        return plain

    def load(self) -> DeviceSettings:
        self.unreadable.clear()
        with self.db.connection() as conn:
            rows = conn.execute("SELECT key, value_json FROM device_settings").fetchall()
        values = {r["key"]: self._decode(r["key"], r["value_json"]) for r in rows if r["key"] in _FIELDS}
        if "timezone" not in values:
            values["timezone"] = guess_timezone()
        return DeviceSettings(**values)

    def save(self, settings: DeviceSettings) -> None:
        with self.db.transaction() as conn:
            for key, value in asdict(settings).items():
                if key in self.unreadable and value == _DEFAULTS[key]:
                    continue          # don't overwrite a sealed value we merely couldn't read
                conn.execute(
                    "INSERT INTO device_settings (key, value_json) VALUES (?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                    (key, self._encode(key, value)),
                )

    def update(self, **changes: Any) -> DeviceSettings:
        unknown = set(changes) - _FIELDS
        if unknown:
            raise KeyError(f"Unknown settings: {sorted(unknown)}")
        updated = replace(self.load(), **changes)
        self.save(updated)
        return updated

    def secret(self, name: str, env_var: str) -> str | None:
        """Environment wins over the stored value (for CI / managed devices)."""
        return os.environ.get(env_var) or getattr(self.load(), name)
