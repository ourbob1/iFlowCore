"""
The integration runtime: form values → API requests → integer-money transactions.

    values = defaults(schema, today) | what the user typed
    errors = check_values(schema, values)
    url    = build_url(schema, request, values, tz=..., offset=0)
    result = pull(schema, values, credentials, transport=..., tz=...)     # PullResult(records, …)
    import_records(db, platform_id, result.records, base_currency=...)   # dedupes on external_ref

Rules the engine enforces (on top of core/integration/schema.py):
  - requests go only to the schema's base_url host, built from a path template whose placeholders
    are URL-encoded; the token is added as a header by the engine, never through a template;
  - redirects are refused (3xx), so a token can't follow one somewhere else;
  - responses are capped (5 MB a page, pages and items per schema), JSON numbers are read as
    Decimal, and money becomes integer minor units through core.financial.money (no floats);
  - error messages carry status codes and paths, never tokens, secrets or response bodies.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from core.financial.ingestion import SKIP, ParsedRecord
from core.financial.money import MoneyError, apply_bp, parse_amount
from core.fleet.http import HttpResponse, ProviderError, send_with_retry
from core.integration.schema import (AMOUNT_ROLES, BUILTINS, DATE_DEFAULTS, IntegrationSchema, RecordMapping,
                                     RequestSpec, _PLACEHOLDER)

MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_ITEMS = 10_000
_ITEM_REF = re.compile(r"\{((?:detail\.)?[A-Za-z_][A-Za-z0-9_.]*)\}")


class IntegrationError(Exception):
    """A pull failed; `message` is safe to show (no tokens, secrets or response bodies)."""

    def __init__(self, message: str, *, code: str = "failed") -> None:
        super().__init__(message)
        self.message, self.code = message, code


class AuthExpired(IntegrationError):
    def __init__(self, message: str = "The connection has expired. Connect again.") -> None:
        super().__init__(message, code="auth")


@dataclass
class Credentials:
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0                       # unix seconds; 0 = unknown

    def to_dict(self) -> dict[str, Any]:
        return {"access_token": self.access_token, "refresh_token": self.refresh_token, "expires_at": self.expires_at}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> Credentials:
        raw = raw or {}
        return cls(str(raw.get("access_token") or ""), str(raw.get("refresh_token") or ""),
                   float(raw.get("expires_at") or 0))

    def __repr__(self) -> str:                     # never print tokens
        return f"Credentials(access={'set' if self.access_token else 'none'}, " \
               f"refresh={'set' if self.refresh_token else 'none'})"


@dataclass
class PullResult:
    records: list[ParsedRecord] = field(default_factory=list)
    items: int = 0
    skipped: int = 0                               # mapped to "skip" (e.g. cash collected)
    unmapped: dict[str, int] = field(default_factory=dict)   # category → count (not in kind_values)
    errors: list[str] = field(default_factory=list)          # per-item problems, first few only
    requests: int = 0
    credentials_changed: bool = False

    def total(self, kind: str | None = None) -> int:
        return sum(r.gross_amount_minor for r in self.records if kind is None or r.kind == kind)


# --- form values -------------------------------------------------------------------------------------


def defaults(schema: IntegrationSchema, today: date) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in schema.fields:
        value = f.default
        if f.type == "date" and value and DATE_DEFAULTS.match(value):
            days = int(value[6:-1]) if "-" in value else 0
            value = (today - timedelta(days=days)).isoformat()
        if f.type == "checkbox":
            value = "true" if str(value).lower() in ("1", "true", "yes") else "false"
        out[f.key] = value
    return out


def check_values(schema: IntegrationSchema, values: Mapping[str, str]) -> dict[str, str]:
    """Field key → problem (empty: all good)."""
    problems: dict[str, str] = {}
    for f in schema.fields:
        v = str(values.get(f.key, "") or "").strip()
        if not v:
            if f.required:
                problems[f.key] = f"{f.label} is required"
            continue
        if f.type == "date":
            try:
                date.fromisoformat(v)
            except ValueError:
                problems[f.key] = f"{f.label}: use YYYY-MM-DD"
        elif f.type == "number":
            try:
                Decimal(v)
            except InvalidOperation:
                problems[f.key] = f"{f.label} must be a number"
        elif f.type == "select" and v not in {o for o, _ in f.options}:
            problems[f.key] = f"{f.label}: pick one of the options"
    dates = [f.key for f in schema.fields if f.type == "date"]
    if {"start_date", "end_date"} <= set(dates) and not problems.keys() & {"start_date", "end_date"}:
        start, end = values.get("start_date"), values.get("end_date")
        if start and end and start > end:
            problems["end_date"] = "The end date is before the start date"
    return problems


# --- templates ---------------------------------------------------------------------------------------


def _filter(value: str, flt: str, tz: ZoneInfo) -> str:
    if flt == "raw" or not value:
        return value
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise IntegrationError(f"{value!r} isn't a date (YYYY-MM-DD)", code="values") from None
    end = flt.endswith("_end")
    local = datetime.combine(day, time(23, 59, 59) if end else time(0, 0), tzinfo=tz)
    if flt.startswith("epoch_ms"):
        return str(int(local.timestamp()) * 1000 + (999 if end else 0))
    if flt.startswith("epoch"):
        return str(int(local.timestamp()))
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")       # iso / iso_end


def render(template: str, values: Mapping[str, str], tz: ZoneInfo, *, quote: Callable[[str], str] | None = None) -> str:
    def sub(m: re.Match[str]) -> str:
        name, flt = m.group(1), m.group(2) or "raw"
        out = _filter(str(values.get(name, "") or ""), flt, tz)
        return quote(out) if quote else out

    return _PLACEHOLDER.sub(sub, template)


def build_url(schema: IntegrationSchema, request: RequestSpec, values: Mapping[str, str], *, tz: ZoneInfo,
              offset: int = 0, page: int = 1) -> str:
    """The request URL for these form values and this page. Placeholders are URL-encoded; empty query
    values are left out; the host is always the schema's."""
    secret = {f.key for f in schema.fields if f.type == "secret"}
    env = {k: v for k, v in values.items() if k not in secret and k not in BUILTINS}
    p = request.pagination
    env.update(offset=str(offset), limit=str(p.limit), page=str(page))
    path = render(request.path, env, tz, quote=lambda v: urllib.parse.quote(v, safe=""))
    query = {k: render(v, env, tz) for k, v in request.query.items()}
    if p.type == "offset":
        query[p.offset_param] = str(offset)
        query[p.limit_param] = str(p.limit)
    query = {k: v for k, v in query.items() if v != ""}
    url = schema.base_url + path + (("?" + urllib.parse.urlencode(query)) if query else "")
    if urllib.parse.urlsplit(url).hostname != schema.host:           # defence in depth: never another host
        raise IntegrationError("Refusing a request outside the integration's API host", code="schema")
    return url


# --- reading responses ----------------------------------------------------------------------------------


def resolve(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(obj, Mapping):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit():
            idx = int(part)
            obj = obj[idx] if idx < len(obj) else None
        else:
            return None
        if obj is None:
            return None
    return obj


def _lookup(item: Mapping[str, Any], detail: Mapping[str, Any], path: str) -> Any:
    if path.startswith("detail."):
        return resolve(detail, path[len("detail."):])
    return resolve(item, path)


def _first(item: Mapping[str, Any], detail: Mapping[str, Any], paths: tuple[str, ...]) -> Any:
    for path in paths:
        value = _lookup(item, detail, path)
        if value not in (None, ""):
            return value
    return None


def _when(value: Any, fmt: str) -> str:
    try:
        if fmt in ("epoch_s", "epoch_ms"):
            seconds = Decimal(str(value)) / (1000 if fmt == "epoch_ms" else 1)
            moment = datetime.fromtimestamp(float(seconds), tz=timezone.utc)
        else:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
    except (InvalidOperation, ValueError, OverflowError, OSError):
        raise ValueError(f"unreadable time {str(value)[:40]!r}") from None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _cents(value: Any, currency: str) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool) or isinstance(value, (list, dict)):
        raise ValueError(f"not an amount: {type(value).__name__}")
    if isinstance(value, float):                   # only if a caller parsed JSON without Decimal
        value = Decimal(repr(value))
    return parse_amount(value if isinstance(value, (int, Decimal)) else str(value), currency)


def _description(template: str, item: Mapping[str, Any], detail: Mapping[str, Any]) -> str | None:
    """Render ' · '-separated parts; a part whose placeholder is missing is dropped, not left half-empty."""
    if not template:
        return None
    parts = []
    for part in template.split(" · "):
        missing = False

        def sub(m: re.Match[str]) -> str:
            nonlocal missing
            value = _lookup(item, detail, m.group(1))
            if value in (None, "") or isinstance(value, (list, dict)):
                missing = True
                return ""
            return str(value)

        text = _ITEM_REF.sub(sub, part)
        if not missing and text.strip():
            parts.append(text.strip())
    return " · ".join(parts)[:200] or None


def normalize(schema: IntegrationSchema, mapping: RecordMapping, item: Mapping[str, Any],
              detail: Mapping[str, Any], index: int) -> list[ParsedRecord] | None:
    """One API item → transactions (None: skipped on purpose). Raises ValueError / MoneyError."""
    kind_override: str | None = None
    if mapping.kind_path:
        raw = str(_lookup(item, detail, mapping.kind_path) or "").strip().casefold()
        mapped = mapping.kind_values.get(raw)
        if mapped is None:
            raise LookupError(raw or "(blank)")
        if mapped == SKIP:
            return None
        kind_override = mapped
    when = _first(item, detail, mapping.occurred_at)
    if when is None:
        raise ValueError("no time")
    occurred_at = _when(when, mapping.time_format)
    currency = str(_first(item, detail, mapping.currency) or mapping.default_currency).strip().upper()
    if not re.match(r"^[A-Z]{3}$", currency):
        raise ValueError(f"bad currency {currency[:8]!r}")

    def amount(rule: Any) -> int:
        total = 0
        for path in rule.paths:
            sign = -1 if path.startswith("-") else 1
            total += sign * _cents(_lookup(item, detail, path.lstrip("-")), currency)
        return apply_bp(total, rule.multiplier_bp)

    # the category decides what the main amount is; tolls / tips / bonus columns keep their own kinds
    components = [((kind_override or "earning") if role == "gross" else AMOUNT_ROLES[role], amount(rule))
                  for role, rule in mapping.amounts.items()]
    components = [(k, a) for k, a in components if a]
    fee = abs(amount(mapping.fees)) if mapping.fees else 0
    if not components and not fee:
        return []
    if not components:
        components = [("adjustment", 0)]
    fee_at = next((i for i, (k, _) in enumerate(components) if k == "earning"), 0)
    ref = _first(item, detail, mapping.reference)
    if ref is None:
        digest = hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()[:24]
        ref = f"h:{digest}"
    base = f"{schema.platform.casefold()}:{ref}"
    multi = len(mapping.amounts) > 1
    description = _description(mapping.description, item, detail)
    return [ParsedRecord(occurred_at=occurred_at, kind=kind, gross_amount_minor=value,
                         platform_fee_minor=fee if i == fee_at else 0, currency=currency,
                         external_ref=f"{base}#{kind}" if multi else base, description=description,
                         source_line=index)
            for i, (kind, value) in enumerate(components)]


# --- running a pull ----------------------------------------------------------------------------------------


def _read(resp: HttpResponse) -> Any:
    if len(resp.body or b"") > MAX_PAGE_BYTES:
        raise IntegrationError("The platform sent more data than iFlow accepts in one page", code="too_large")
    try:
        return json.loads((resp.body or b"").decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise IntegrationError("The platform's answer wasn't JSON", code="bad_response") from None


def _fetch_all(schema: IntegrationSchema, request: RequestSpec, values: Mapping[str, str], *, tz: ZoneInfo,
               send: Callable[[str], HttpResponse], result: PullResult) -> list[Any]:
    items: list[Any] = []
    p = request.pagination
    offset, page = 0, 1
    while True:
        resp = send(build_url(schema, request, values, tz=tz, offset=offset, page=page))
        result.requests += 1
        data = _read(resp)
        found = resolve(data, request.items_path) if request.items_path else data
        if found is None:
            found = []
        if not isinstance(found, list):
            raise IntegrationError(f"{request.name}: expected a list at {request.items_path or 'the top level'}",
                                   code="bad_response")
        items += found
        if len(items) > MAX_ITEMS:
            raise IntegrationError(f"More than {MAX_ITEMS} items; pick a shorter date range", code="too_large")
        if p.type != "offset" or not found or page >= p.max_pages:
            break
        offset += len(found)
        page += 1
        total = resolve(data, p.total_path) if p.total_path else None
        if isinstance(total, (int, Decimal)) and not isinstance(total, bool):
            if offset >= int(total):                   # the API says how many there are: trust that
                break
        elif len(found) < p.limit:                     # no total: a short page is the last page
            break
    return items


def pull(schema: IntegrationSchema, values: Mapping[str, str], credentials: Credentials | None, *,
         transport: Any, tz: ZoneInfo, refresh: Callable[[Credentials], Credentials] | None = None,
         now: Callable[[], float] | None = None, sleep: Any = None) -> PullResult:
    """Run every request and normalise the records. Raises IntegrationError (AuthExpired on 401/403)."""
    import time as _time

    problems = check_values(schema, values)
    if problems:
        raise IntegrationError(next(iter(problems.values())), code="values")
    result = PullResult()
    clock = now or _time.time
    creds = credentials or Credentials()
    if schema.auth.type == "bearer_token":
        creds = Credentials(access_token=str(values.get(schema.auth.token_field, "") or "").strip())
        if not creds.access_token:
            raise AuthExpired("Enter your access token")
    if schema.auth.type == "oauth2_pkce":
        if not creds.access_token:
            raise AuthExpired("Connect your account first")
        if creds.expires_at and creds.expires_at - 60 < clock() and creds.refresh_token and refresh:
            creds = refresh(creds)
            result.credentials_changed = True

    def headers() -> dict[str, str]:
        if schema.auth.type == "none":
            return {}
        if schema.auth.header.lower() == "authorization":
            return {"Authorization": f"Bearer {creds.access_token}"}
        return {schema.auth.header: creds.access_token}

    def send(url: str) -> HttpResponse:
        nonlocal creds
        kwargs: dict[str, Any] = {"headers": headers(), "timeout": 30, "retries": 2}
        if sleep is not None:
            kwargs["sleep"] = sleep
        try:
            resp = send_with_retry(transport, "GET", url, **kwargs)
        except ProviderError:
            raise IntegrationError(f"Can't reach {schema.host}. Check your connection.", code="offline") from None
        if resp.status in (401, 403) and schema.auth.type == "oauth2_pkce" and creds.refresh_token and refresh \
                and not result.credentials_changed:
            creds = refresh(creds)
            result.credentials_changed = True
            kwargs["headers"] = headers()
            resp = send_with_retry(transport, "GET", url, **kwargs)
        if 300 <= resp.status < 400:
            raise IntegrationError(f"{schema.host} tried to redirect the request; refused for safety", code="redirect")
        if resp.status in (401, 403):
            raise AuthExpired()
        if resp.status == 429:
            raise IntegrationError(f"{schema.platform} is rate-limiting requests; try again later", code="rate_limited")
        if resp.status >= 400:
            raise IntegrationError(f"{schema.platform} answered {resp.status}", code="http")
        return resp

    details: dict[str, Any] = {}
    for request in (r for r in schema.requests if r.role == "details"):
        for item in _fetch_all(schema, request, values, tz=tz, send=send, result=result):
            key = resolve(item, request.key_path) if isinstance(item, Mapping) else None
            if key not in (None, ""):
                details[str(key)] = item
    for request in (r for r in schema.requests if r.role == "records"):
        assert request.mapping is not None
        for index, item in enumerate(_fetch_all(schema, request, values, tz=tz, send=send, result=result), start=1):
            result.items += 1
            if not isinstance(item, Mapping):
                result.errors.append(f"{request.name} #{index}: not an object")
                continue
            detail = _join(item, details, schema)
            try:
                recs = normalize(schema, request.mapping, item, detail, index)
            except LookupError as exc:
                category = str(exc.args[0])[:40]
                result.unmapped[category] = result.unmapped.get(category, 0) + 1
                continue
            except (ValueError, MoneyError) as exc:
                if len(result.errors) < 20:
                    result.errors.append(f"{request.name} #{index}: {exc}")
                continue
            if recs is None:
                result.skipped += 1
            else:
                result.records += recs
    if result.credentials_changed and credentials is not None:
        credentials.access_token, credentials.refresh_token, credentials.expires_at = (
            creds.access_token, creds.refresh_token, creds.expires_at)
    return result


def _join(item: Mapping[str, Any], details: Mapping[str, Any], schema: IntegrationSchema) -> Mapping[str, Any]:
    """Find the details item for a record by any details request's key path present on the record."""
    for request in schema.requests:
        if request.role == "details":
            key = resolve(item, request.key_path)
            if key not in (None, "") and str(key) in details:
                return details[str(key)]
    return {}


def preview_urls(schema: IntegrationSchema, values: Mapping[str, str], *, tz: ZoneInfo) -> list[tuple[str, str]]:
    """(request name, first-page URL) for the Studio and the wizard's "what will be fetched" line."""
    return [(r.name, build_url(schema, r, values, tz=tz)) for r in schema.requests]


def test_response(schema: IntegrationSchema, responses: Mapping[str, Any], values: Mapping[str, str] | None = None,
                  ) -> PullResult:
    """The Studio's test bench: run the mapping over pasted sample responses (one page per request).
    Local only; nothing is sent anywhere."""
    pages = {name: json.dumps(body) if not isinstance(body, str) else body for name, body in responses.items()}

    class _Bench:
        def request(self, method, url, **kw):
            path = urllib.parse.urlsplit(url).path
            for r in schema.requests:
                if path.startswith(r.path.split("{")[0]) and r.name in pages:
                    return HttpResponse(200, pages[r.name].encode("utf-8"), {})
            return HttpResponse(200, b"[]", {})

    filled = defaults(schema, date.today()) | dict(values or {})
    for f in schema.fields:
        if f.type == "secret" and not filled.get(f.key):
            filled[f.key] = "test-bench"
    creds = Credentials(access_token="test-bench") if schema.auth.type == "oauth2_pkce" else None
    single = IntegrationSchema(**{**{k: getattr(schema, k) for k in schema.__dataclass_fields__},
                                  "requests": tuple(_one_page(r) for r in schema.requests)})
    return pull(single, filled, creds, transport=_Bench(), tz=ZoneInfo("UTC"))


def _one_page(r: RequestSpec) -> RequestSpec:
    from dataclasses import replace

    from core.integration.schema import Pagination

    return replace(r, pagination=Pagination())
