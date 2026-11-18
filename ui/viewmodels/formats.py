"""
Statement-format updates (Parser Studio → signed bundle on the iFlow CDN → this device).

`check(state, services)` is used by Settings ("Check now") and by the app after unlock (at most once a
day, only when automatic updates are on). Errors never interrupt the user: they become a one-line
status in Settings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from core.financial.parsers.dynamic import SchemaError, installed_schemas
from core.financial.parsers.sync import FormatUpdateError, due, fetch_and_install, summary
from core.timeutil import utc_now

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InstalledFormat:
    name: str
    platform: str
    version: int


def installed(db: Any) -> list[InstalledFormat]:
    try:
        return [InstalledFormat(s.name, s.platform, s.version) for s in installed_schemas(db)]
    except Exception:                                  # a database from before migration 0010
        return []


def check(state: Any, services: dict[str, Any], *, force: bool = False) -> str | None:
    """Fetch + install new formats. Returns a status line, or None when nothing was due."""
    settings = state.settings
    url = state.config.formats_url
    if not url:
        return "Statement-format updates are turned off for this build."
    if not force and (not settings.formats_auto_update or not due(settings.formats_checked_at)):
        return None
    transport = services.get("formats_transport")
    if transport is None:
        from core.fleet.http import default_transport

        transport = default_transport()
    now = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        result = fetch_and_install(state.db, url, transport=transport, etag=settings.formats_etag)
    except (FormatUpdateError, SchemaError) as exc:
        state.update_settings(formats_checked_at=now)   # try again tomorrow, not on every unlock
        log.warning("Statement-format update: %s", exc)
        return str(exc)
    state.update_settings(formats_checked_at=now, formats_etag=result.etag)
    return summary(result.report)
