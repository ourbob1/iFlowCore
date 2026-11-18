"""
Platform connections in the user app (Finances → "Connect a platform").

Each connection is driven entirely by a signed integration schema (installed with the statement
formats): its form, its sign-in and its API calls. Per connection, device settings keep
`integration_credentials[<id>]` (a sealed field, never synced):

    values       the non-secret form values (date range, options)
    secrets      pasted API tokens (secret form fields)
    credentials  OAuth tokens {access_token, refresh_token, expires_at}
    platform_id  which of the user's platforms the transactions belong to
    last_pull_at / last_result

Pulled transactions go through import_records, the same path as statement imports, so a second
pull of the same period only adds what's new.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from core.financial.ingestion import import_records
from core.financial.parsers.dynamic import installed_integrations
from core.fleet.http import UrllibTransport, in_browser
from core.integration import engine, oauth
from core.integration.schema import IntegrationSchema
from core.timeutil import utc_now
from ui.format import money

log = logging.getLogger(__name__)
UI_KEY = "integrations_ui"


@dataclass
class ConnUi:
    error: str | None = None
    notice: str | None = None
    open: bool = False
    field_errors: dict[str, str] = field(default_factory=dict)


def ui(services: dict[str, Any], schema_id: str) -> ConnUi:
    return services.setdefault(UI_KEY, {}).setdefault(schema_id, ConnUi())


def installed(state: Any) -> list[IntegrationSchema]:
    try:
        return installed_integrations(state.db)
    except Exception:                                     # a database from before migration 0011
        return []


def saved(state: Any, schema_id: str) -> dict[str, Any]:
    return dict((state.settings.integration_credentials or {}).get(schema_id) or {})


def _store(state: Any, schema_id: str, entry: dict[str, Any] | None) -> None:
    all_conns = dict(state.settings.integration_credentials or {})
    if entry is None:
        all_conns.pop(schema_id, None)
    else:
        all_conns[schema_id] = entry
    state.update_settings(integration_credentials=all_conns)


def transport(services: dict[str, Any]) -> Any:
    """Never follow redirects with a token attached (urllib would forward the Authorization header)."""
    injected = services.get("integration_transport")
    if injected is not None:
        return injected
    from core.fleet.http import XhrTransport

    return XhrTransport() if in_browser() else UrllibTransport(follow_redirects=False)


def broker_url(state: Any, services: dict[str, Any]) -> str:
    from ui.viewmodels.account import server_url

    return server_url(state, services)


def values_for(state: Any, schema: IntegrationSchema) -> dict[str, str]:
    """Defaults, then what the user saved (dates are re-derived from 'today-30d' style defaults
    unless the user typed their own)."""
    return engine.defaults(schema, state.today()) | dict(saved(state, schema.id).get("values") or {})


def secrets_saved(state: Any, schema: IntegrationSchema) -> frozenset[str]:
    return frozenset(k for k, v in (saved(state, schema.id).get("secrets") or {}).items() if v)


def status(state: Any, schema: IntegrationSchema) -> tuple[bool, str]:
    """(connected, one-line status)."""
    entry = saved(state, schema.id)
    if schema.auth.type == "oauth2_pkce":
        connected = bool((entry.get("credentials") or {}).get("access_token"))
    elif schema.auth.type == "bearer_token":
        connected = bool((entry.get("secrets") or {}).get(schema.auth.token_field))
    else:
        connected = bool(entry)
    if not connected:
        return False, "Not connected"
    last = entry.get("last_pull_at")
    return True, f"Connected · last pulled {last[:16].replace('T', ' ')} UTC · {entry.get('last_result', '')}" \
        if last else "Connected · not pulled yet"


def can_sign_in_here(state: Any) -> bool:
    """The OAuth browser flow needs a local listener: desktop only."""
    return not in_browser() and not state.config.is_mobile


# --- actions ------------------------------------------------------------------------------------------


def save_form(state: Any, services: dict[str, Any], schema: IntegrationSchema, typed: dict[str, str],
              platform_id: str | None) -> bool:
    u = ui(services, schema.id)
    u.error = u.notice = None
    secret_keys = {f.key for f in schema.fields if f.type == "secret"}
    entry = saved(state, schema.id)
    secrets = dict(entry.get("secrets") or {})
    for k in secret_keys:
        if typed.get(k):
            secrets[k] = typed[k]                         # empty input keeps the saved one
    values = {k: v for k, v in typed.items() if k not in secret_keys}
    problems = engine.check_values(schema, values | secrets)
    u.field_errors = problems
    if problems:
        u.error = "; ".join(problems.values()) + "."
        return False
    entry.update(values=values, secrets=secrets, platform_id=platform_id or entry.get("platform_id"))
    _store(state, schema.id, entry)
    u.notice = "Saved on this device."
    return True


def connect(state: Any, services: dict[str, Any], schema: IntegrationSchema) -> bool:
    u = ui(services, schema.id)
    u.error = u.notice = None
    try:
        creds = oauth.connect(schema, transport=transport(services), broker_url=broker_url(state, services),
                              open_url=services.get("open_url") or __import__("webbrowser").open)
    except engine.IntegrationError as exc:
        u.error = exc.message
        return False
    entry = saved(state, schema.id)
    entry["credentials"] = creds.to_dict()
    _store(state, schema.id, entry)
    u.notice = f"Connected to {schema.platform}. Pull your earnings whenever you like."
    return True


def pull_now(state: Any, services: dict[str, Any], schema: IntegrationSchema) -> bool:
    u = ui(services, schema.id)
    u.error = u.notice = None
    entry = saved(state, schema.id)
    platform_id = entry.get("platform_id")
    if not platform_id:
        u.error = f"Pick which of your platforms these {schema.platform} earnings belong to, then save."
        return False
    values = values_for(state, schema) | dict(entry.get("secrets") or {})
    creds = engine.Credentials.from_dict(entry.get("credentials"))
    t = transport(services)

    def refresh(c: engine.Credentials) -> engine.Credentials:
        return oauth.refresh(schema, c, transport=t, broker_url=broker_url(state, services))

    try:
        result = engine.pull(schema, values, creds, transport=t, tz=ZoneInfo(state.tz), refresh=refresh)
    except engine.AuthExpired as exc:
        if schema.auth.type == "oauth2_pkce":
            entry.pop("credentials", None)               # expired for good: sign in again
            _store(state, schema.id, entry)
        u.error = exc.message
        return False
    except engine.IntegrationError as exc:
        u.error = exc.message
        return False
    if result.credentials_changed:
        entry["credentials"] = creds.to_dict()
    imported = import_records(state.db, platform_id, result.records, base_currency=state.currency)
    earned = sum(r.gross_amount_minor for r in result.records if r.kind != "reimbursement")
    summary = (f"{imported.inserted} new, {imported.duplicates} already here · "
               f"{money(earned, state.currency)} gross in the pulled period")
    entry.update(last_pull_at=utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"), last_result=summary)
    _store(state, schema.id, entry)
    extra = []
    if result.unmapped:
        extra.append(f"{sum(result.unmapped.values())} item(s) of an unknown type were left out")
    if result.errors:
        extra.append(f"{len(result.errors)} item(s) couldn't be read")
    u.notice = f"Pulled from {schema.platform}: {summary}." + (" " + "; ".join(extra) + "." if extra else "")
    try:
        from ui.viewmodels.health import sync_now

        sync_now(state)                                  # new payouts fund health premiums (Guild only)
    except Exception:
        log.exception("Premium sync after a platform pull failed")
    return True


def disconnect(state: Any, services: dict[str, Any], schema: IntegrationSchema) -> None:
    _store(state, schema.id, None)
    u = ui(services, schema.id)
    u.error, u.notice, u.field_errors = None, "Disconnected. Tokens were removed from this device; imported " \
                                               "transactions stay.", {}


def default_platform(platforms: tuple[tuple[str, str], ...], schema: IntegrationSchema) -> str | None:
    wanted = schema.platform.strip().casefold()
    return next((pid for pid, name in platforms if name.strip().casefold() == wanted), None)
