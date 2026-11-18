"""
Statement-format distribution, app side: fetch the vendor-signed bundle from a static CDN file
(default https://formats.iflowcore.com/v1/bundle.ifpb) or read a bundle file, verify it and install
it into the encrypted database.

Trust lives in the app, not the host: only the vendor public keys pinned in this build (or added
with IFLOW_LICENSE_PUBKEYS) are accepted, so a compromised bucket or CDN can at most withhold an
update; a tampered or replayed-older bundle is rejected. The request is a plain GET with no cookies,
tokens, account or device ids (the ETag from last time is the only state). Installed formats stay on
this device (vendor_parsers is a local-only table, never synced).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from core.financial.parsers.dynamic import MAX_BUNDLE_CHARS, InstallReport, SchemaError, install_bundle
from core.licensing.tokens import trusted_keys

MAX_FILE_BYTES = MAX_BUNDLE_CHARS
CHECK_EVERY = timedelta(hours=24)


class FormatUpdateError(Exception):
    """Couldn't reach or read the format file (not a trust failure: those are SchemaError)."""


@dataclass(frozen=True)
class FetchResult:
    report: InstallReport | None      # None: nothing new (HTTP 304)
    etag: str | None


def vendor_keys(env: Mapping[str, str] | None = None) -> list[str]:
    return trusted_keys("vendor", allow_simulation=False, env=env)


def fetch_and_install(db: Any, url: str, *, transport: Any, etag: str | None = None,
                      keys: list[str] | None = None, env: Mapping[str, str] | None = None) -> FetchResult:
    """Conditional GET of the bundle, then verify + install. Raises FormatUpdateError / SchemaError."""
    keys = keys if keys is not None else vendor_keys(env)
    if not keys:
        raise SchemaError("This build doesn't trust a statement-format publisher yet")
    headers = {"Accept": "text/plain"}
    if etag:
        headers["If-None-Match"] = etag
    from core.fleet.http import ProviderError

    try:
        resp = transport.request("GET", url, headers=headers, timeout=20)
    except ProviderError as exc:
        raise FormatUpdateError("Couldn't reach the statement-format server") from exc
    if resp.status == 304:
        return FetchResult(None, etag)
    if resp.status == 404:
        raise FormatUpdateError("No statement formats have been published yet")
    if resp.status != 200:
        raise FormatUpdateError(f"The statement-format server answered {resp.status}")
    body = resp.body or b""
    if len(body) > MAX_FILE_BYTES:
        raise SchemaError("The statement-format file is too large")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise SchemaError("That isn't an iFlow Core statement-format file") from None
    report = install_bundle(db, text, keys)
    lowered = {k.lower(): v for k, v in (resp.headers or {}).items()}
    return FetchResult(report, lowered.get("etag"))


def import_file(db: Any, path: str, *, env: Mapping[str, str] | None = None,
                keys: list[str] | None = None) -> InstallReport:
    with open(path, "rb") as fh:
        raw = fh.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise SchemaError("That file is too large to be a statement-format file")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SchemaError("That isn't an iFlow Core statement-format file") from None
    return install_bundle(db, text, keys if keys is not None else vendor_keys(env))


def due(last_checked: str | None, now: datetime | None = None) -> bool:
    """Whether an automatic check is due (at most once a day)."""
    if not last_checked:
        return True
    try:
        then = datetime.fromisoformat(last_checked.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (now or datetime.now(timezone.utc)) - then >= CHECK_EVERY


def summary(report: InstallReport | None) -> str:
    if report is None:
        return "Statement formats are up to date."
    parts = []
    if report.added:
        parts.append(f"{len(report.added)} new")
    if report.updated:
        parts.append(f"{len(report.updated)} updated")
    if report.unchanged:
        parts.append(f"{len(report.unchanged)} already current")
    if report.refused_downgrades:
        parts.append(f"{len(report.refused_downgrades)} older version(s) ignored")
    new_integrations = len(report.integrations_added) + len(report.integrations_updated)
    if new_integrations:
        parts.append(f"{new_integrations} platform connection(s) new or updated")
    return "Statement formats: " + (", ".join(parts) if parts else "nothing to install") + "."
