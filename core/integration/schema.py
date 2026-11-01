"""
Platform API integrations (Phase 11): a declarative, signed JSON schema that describes

  • the connection form the user fills in (fields, types, defaults, help),
  • how the app authenticates (OAuth 2.0 + PKCE, a pasted token, or none),
  • the API requests to make (path + query *templates* filled from the form, pagination), and
  • how each returned item becomes integer-money transactions (paths into the JSON, signs, kinds).

Schemas are written in the Admin Console (Integration Studio), signed into the same IFPB1 bundle as
statement formats and verified on the device; this module re-validates everything on install.

Plain dataclasses, not Pydantic: Pydantic isn't packaged in the mobile builds, and statement formats
use the same pattern (core/financial/parsers/dynamic.py).

Safety rules enforced here (the engine enforces the rest at run time):
  - every URL is https; requests can only go to `base_url`'s host (paths must start with "/");
  - templates may only reference declared form fields and the pagination built-ins;
  - the token is never a template value, so a schema can't move it into a URL or another host;
  - limits on pages, items and field counts keep a bad schema from running away.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from core.financial.ingestion import SKIP, TRANSACTION_KINDS

SCHEMA_FORMAT = 1
MAX_FIELDS = 20
MAX_REQUESTS = 6
MAX_PAGES_LIMIT = 50
MAX_TEMPLATE = 300

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PATH = re.compile(r"^-?[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*$")       # "breakdown.service_fee", "-fee"
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)(?:\|([a-z_]+))?\}")
_ITEM_PLACEHOLDER = re.compile(r"\{((?:detail\.)?[A-Za-z_][A-Za-z0-9_.]*)\}")

FIELD_TYPES = ("text", "secret", "date", "number", "select", "checkbox")
AUTH_TYPES = ("oauth2_pkce", "bearer_token", "none")
TOKEN_EXCHANGE = ("direct", "broker")       # broker: iFlow's API adds the client secret (never in the app)
FILTERS = ("raw", "epoch", "epoch_end", "epoch_ms", "epoch_ms_end", "iso", "iso_end")
BUILTINS = ("offset", "limit", "page")
TIME_FORMATS = ("epoch_s", "epoch_ms", "iso8601")
AMOUNT_ROLES = {"gross": "earning", "tips": "tip", "bonus": "bonus", "tolls": "reimbursement"}
DATE_DEFAULTS = re.compile(r"^today(-\d{1,3}d)?$")                   # "today", "today-30d"


class IntegrationSchemaError(ValueError):
    pass


def _err(where: str, message: str) -> IntegrationSchemaError:
    return IntegrationSchemaError(f"{where}: {message}")


def https_url(value: Any, where: str) -> str:
    url = str(value or "").strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise _err(where, f"must be an https:// address (got {url!r})")
    if parts.query or parts.fragment:
        raise _err(where, "can't have a query string or fragment")
    return url.rstrip("/")


def placeholders(template: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2) or "raw") for m in _PLACEHOLDER.finditer(template)]


# --- pieces ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FormField:
    key: str
    label: str
    type: str = "text"
    required: bool = False
    default: str = ""
    help: str = ""
    placeholder: str = ""
    options: tuple[tuple[str, str], ...] = ()      # select: (value, label)

    def __post_init__(self) -> None:
        where = f"field {self.key!r}"
        if not _KEY.match(self.key or ""):
            raise _err(where, "keys are 1–32 lowercase letters, digits and _ (starting with a letter)")
        if self.key in BUILTINS:
            raise _err(where, f"{self.key!r} is reserved for pagination")
        if self.type not in FIELD_TYPES:
            raise _err(where, f"type must be one of {', '.join(FIELD_TYPES)}")
        if not (self.label or "").strip() or len(self.label) > 80:
            raise _err(where, "needs a label (up to 80 characters)")
        if self.type == "select" and not self.options:
            raise _err(where, "a select needs options")
        if self.type == "date" and self.default and not DATE_DEFAULTS.match(self.default):
            raise _err(where, "date defaults are 'today' or 'today-<N>d'")
        if self.type == "secret" and self.default:
            raise _err(where, "secret fields can't have a default")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"key": self.key, "label": self.label, "type": self.type}
        for name in ("required", "default", "help", "placeholder"):
            if getattr(self, name):
                d[name] = getattr(self, name)
        if self.options:
            d["options"] = [{"value": v, "label": lbl} for v, lbl in self.options]
        return d

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> FormField:
        opts = tuple((str(o["value"]), str(o.get("label", o["value"]))) for o in raw.get("options") or ())
        return cls(str(raw.get("key", "")), str(raw.get("label", "")), str(raw.get("type", "text")),
                   bool(raw.get("required", False)), str(raw.get("default", "")), str(raw.get("help", ""))[:300],
                   str(raw.get("placeholder", ""))[:80], opts)


@dataclass(frozen=True)
class AuthSpec:
    type: str = "none"
    authorize_url: str = ""
    token_url: str = ""
    client_id: str = ""
    scopes: tuple[str, ...] = ()
    token_exchange: str = "direct"
    header: str = "Authorization"                  # bearer_token: the header the token goes in
    token_field: str = ""                          # bearer_token: which secret form field holds it
    redirect_port: int = 0                         # OAuth loopback port; 0 = any (providers that allow it)

    def __post_init__(self) -> None:
        if self.type not in AUTH_TYPES:
            raise _err("auth", f"type must be one of {', '.join(AUTH_TYPES)}")
        if self.type == "oauth2_pkce":
            object.__setattr__(self, "authorize_url", https_url(self.authorize_url, "auth.authorize_url"))
            object.__setattr__(self, "token_url", https_url(self.token_url, "auth.token_url"))
            if not self.client_id.strip():
                raise _err("auth", "OAuth needs a client_id (a public value)")
            if self.token_exchange not in TOKEN_EXCHANGE:
                raise _err("auth", f"token_exchange must be one of {', '.join(TOKEN_EXCHANGE)}")
            if not (self.redirect_port == 0 or 1024 <= int(self.redirect_port) <= 65535):
                raise _err("auth", "redirect_port is 0 (any) or 1024–65535")
        if not re.match(r"^[A-Za-z][A-Za-z0-9-]{0,40}$", self.header):
            raise _err("auth", "header must be a plain header name")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type}
        if self.type == "oauth2_pkce":
            d.update(authorize_url=self.authorize_url, token_url=self.token_url, client_id=self.client_id,
                     scopes=list(self.scopes), token_exchange=self.token_exchange, redirect_port=self.redirect_port)
        if self.type == "bearer_token":
            d.update(header=self.header, token_field=self.token_field)
        return d

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> AuthSpec:
        raw = raw or {}
        return cls(str(raw.get("type", "none")), str(raw.get("authorize_url", "")), str(raw.get("token_url", "")),
                   str(raw.get("client_id", "")), tuple(str(s) for s in raw.get("scopes") or ()),
                   str(raw.get("token_exchange", "direct")), str(raw.get("header", "Authorization")),
                   str(raw.get("token_field", "")), int(raw.get("redirect_port", 0) or 0))


@dataclass(frozen=True)
class Pagination:
    type: str = "none"                  # none | offset
    offset_param: str = "offset"
    limit_param: str = "limit"
    limit: int = 50
    max_pages: int = 20
    total_path: str = ""                # e.g. "count": stop once offset ≥ total

    def __post_init__(self) -> None:
        if self.type not in ("none", "offset"):
            raise _err("pagination", "type must be 'none' or 'offset'")
        if not 1 <= int(self.limit) <= 500 or not 1 <= int(self.max_pages) <= MAX_PAGES_LIMIT:
            raise _err("pagination", f"limit 1–500, max_pages 1–{MAX_PAGES_LIMIT}")
        if self.total_path and not _PATH.match(self.total_path):
            raise _err("pagination", "total_path must be a dotted path")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> Pagination:
        raw = raw or {}
        return cls(str(raw.get("type", "none")), str(raw.get("offset_param", "offset")),
                   str(raw.get("limit_param", "limit")), int(raw.get("limit", 50)), int(raw.get("max_pages", 20)),
                   str(raw.get("total_path", "")))

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "offset_param": self.offset_param, "limit_param": self.limit_param,
                "limit": self.limit, "max_pages": self.max_pages, "total_path": self.total_path}


@dataclass(frozen=True)
class AmountRule:
    """Sum of item paths ("-" in front negates one), then × multiplier (basis points)."""
    paths: tuple[str, ...]
    multiplier_bp: int = 10_000

    def __post_init__(self) -> None:
        if not self.paths or not all(_PATH.match(p) for p in self.paths):
            raise _err("amount", f"paths must be dotted paths like 'amount' or '-breakdown.fee' (got {self.paths})")
        if isinstance(self.multiplier_bp, bool) or not -100_000 <= int(self.multiplier_bp) <= 100_000:
            raise _err("amount", "multiplier_bp must be whole basis points (-100000…100000)")

    @classmethod
    def coerce(cls, raw: Any) -> AmountRule:
        if isinstance(raw, str):
            return cls((raw,))
        if isinstance(raw, (list, tuple)):
            return cls(tuple(str(p) for p in raw))
        if isinstance(raw, Mapping):
            paths = raw.get("paths") or raw.get("path") or ()
            paths = (paths,) if isinstance(paths, str) else tuple(str(p) for p in paths)
            return cls(paths, int(raw.get("multiplier_bp", 10_000)))
        raise _err("amount", f"can't read {raw!r}")

    def to_dict(self) -> Any:
        if self.multiplier_bp == 10_000:
            return list(self.paths) if len(self.paths) > 1 else self.paths[0]
        return {"paths": list(self.paths), "multiplier_bp": self.multiplier_bp}


@dataclass(frozen=True)
class RecordMapping:
    occurred_at: tuple[str, ...]                    # first non-empty path wins
    time_format: str = "iso8601"
    reference: tuple[str, ...] = ()
    currency: tuple[str, ...] = ()
    default_currency: str = "USD"
    amounts: Mapping[str, AmountRule] = field(default_factory=dict)   # gross / tips / bonus / tolls
    fees: AmountRule | None = None                   # platform fees (stored positive)
    kind_path: str = ""                              # e.g. "category"
    kind_values: Mapping[str, str] = field(default_factory=dict)     # raw → kind | "skip"
    description: str = ""                            # template over item paths: "{status} · {detail.distance} mi"

    def __post_init__(self) -> None:
        for name in ("occurred_at", "reference", "currency"):
            paths = getattr(self, name)
            if not all(_PATH.match(p) and not p.startswith("-") for p in paths):
                raise _err(f"mapping.{name}", "must be dotted paths")
        if not self.occurred_at:
            raise _err("mapping", "occurred_at is required (the trip / payment time)")
        if self.time_format not in TIME_FORMATS:
            raise _err("mapping", f"time_format must be one of {', '.join(TIME_FORMATS)}")
        unknown = set(self.amounts) - set(AMOUNT_ROLES)
        if unknown:
            raise _err("mapping.amounts", f"unknown role(s) {sorted(unknown)}; use {', '.join(AMOUNT_ROLES)}")
        if not self.amounts and self.fees is None:
            raise _err("mapping", "map at least one amount")
        bad = {k: v for k, v in self.kind_values.items() if v not in (*TRANSACTION_KINDS, SKIP)}
        if bad:
            raise _err("mapping.kind_values", f"unknown kinds {bad}")
        if self.kind_values and not self.kind_path:
            raise _err("mapping", "kind_values needs kind_path")
        if len(self.description) > MAX_TEMPLATE:
            raise _err("mapping.description", "too long")
        if not re.match(r"^[A-Z]{3}$", self.default_currency):
            raise _err("mapping", "default_currency is an ISO 4217 code like USD")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RecordMapping:
        def paths(value: Any) -> tuple[str, ...]:
            if not value:
                return ()
            return (str(value),) if isinstance(value, str) else tuple(str(v) for v in value)

        return cls(paths(raw.get("occurred_at")), str(raw.get("time_format", "iso8601")), paths(raw.get("reference")),
                   paths(raw.get("currency")), str(raw.get("default_currency", "USD")).upper(),
                   {str(k): AmountRule.coerce(v) for k, v in (raw.get("amounts") or {}).items()},
                   AmountRule.coerce(raw["fees"]) if raw.get("fees") else None, str(raw.get("kind_path", "")),
                   {str(k).casefold(): str(v) for k, v in (raw.get("kind_values") or {}).items()},
                   str(raw.get("description", "")))

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"occurred_at": list(self.occurred_at), "time_format": self.time_format,
                             "amounts": {k: v.to_dict() for k, v in self.amounts.items()},
                             "default_currency": self.default_currency}
        for name in ("reference", "currency"):
            if getattr(self, name):
                d[name] = list(getattr(self, name))
        if self.fees:
            d["fees"] = self.fees.to_dict()
        if self.kind_path:
            d.update(kind_path=self.kind_path, kind_values=dict(self.kind_values))
        if self.description:
            d["description"] = self.description
        return d


@dataclass(frozen=True)
class RequestSpec:
    name: str
    path: str                                        # "/v1/partners/payments" (templates allowed in segments)
    query: Mapping[str, str] = field(default_factory=dict)
    items_path: str = ""                             # where the list of items is ("payments"); "" = top level
    pagination: Pagination = field(default_factory=Pagination)
    role: str = "records"                            # records | details (joined into records by key)
    key_path: str = ""                               # details: the item's key (e.g. "trip_id")
    mapping: RecordMapping | None = None

    def __post_init__(self) -> None:
        where = f"request {self.name!r}"
        if not _KEY.match(self.name or ""):
            raise _err(where, "names are lowercase letters, digits and _")
        if not self.path.startswith("/") or self.path.startswith("//") or "://" in self.path or len(self.path) > 200:
            raise _err(where, "path must start with '/' and stay on the API host")
        if self.role not in ("records", "details"):
            raise _err(where, "role must be 'records' or 'details'")
        if self.role == "records" and self.mapping is None:
            raise _err(where, "a records request needs a mapping")
        if self.role == "details" and not _PATH.match(self.key_path or ""):
            raise _err(where, "a details request needs key_path (e.g. trip_id)")
        if self.items_path and not _PATH.match(self.items_path):
            raise _err(where, "items_path must be a dotted path")
        for k, v in self.query.items():
            if not re.match(r"^[A-Za-z0-9_.\-\[\]]{1,40}$", k) or len(str(v)) > MAX_TEMPLATE:
                raise _err(where, f"bad query parameter {k!r}")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RequestSpec:
        return cls(str(raw.get("name", "")), str(raw.get("path", "")),
                   {str(k): str(v) for k, v in (raw.get("query") or {}).items()}, str(raw.get("items_path", "")),
                   Pagination.from_dict(raw.get("pagination")), str(raw.get("role", "records")),
                   str(raw.get("key_path", "")),
                   RecordMapping.from_dict(raw["mapping"]) if raw.get("mapping") else None)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "path": self.path, "role": self.role}
        if self.query:
            d["query"] = dict(self.query)
        if self.items_path:
            d["items_path"] = self.items_path
        if self.pagination.type != "none":
            d["pagination"] = self.pagination.to_dict()
        if self.key_path:
            d["key_path"] = self.key_path
        if self.mapping:
            d["mapping"] = self.mapping.to_dict()
        return d


# --- the schema --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class IntegrationSchema:
    id: str
    version: int
    name: str
    platform: str
    base_url: str
    auth: AuthSpec
    fields: tuple[FormField, ...]
    requests: tuple[RequestSpec, ...]
    description: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if not _ID.match(self.id or ""):
            raise _err("id", "3–50 lowercase letters, digits and dashes (e.g. uber-driver-api)")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or not 1 <= self.version <= 1_000_000:
            raise _err("version", "a whole number ≥ 1")
        for name in ("name", "platform"):
            if not str(getattr(self, name)).strip() or len(getattr(self, name)) > 80:
                raise _err(name, "required (up to 80 characters)")
        object.__setattr__(self, "base_url", https_url(self.base_url, "base_url"))
        if len(self.fields) > MAX_FIELDS or not self.requests or len(self.requests) > MAX_REQUESTS:
            raise _err("schema", f"1–{MAX_REQUESTS} requests and at most {MAX_FIELDS} fields")
        keys = [f.key for f in self.fields]
        if len(set(keys)) != len(keys):
            raise _err("fields", "keys must be unique")
        names = [r.name for r in self.requests]
        if len(set(names)) != len(names):
            raise _err("requests", "names must be unique")
        if not any(r.role == "records" for r in self.requests):
            raise _err("requests", "at least one request must produce records")
        if self.auth.type == "bearer_token":
            tf = next((f for f in self.fields if f.key == self.auth.token_field), None)
            if tf is None or tf.type != "secret":
                raise _err("auth", "token_field must name a 'secret' form field")
        secret_keys = {f.key for f in self.fields if f.type == "secret"}
        allowed = set(keys) | set(BUILTINS)
        for r in self.requests:
            for template in (r.path, *r.query.values()):
                for name, flt in placeholders(template):
                    if name not in allowed:
                        raise _err(f"request {r.name!r}", f"{{{name}}} isn't a form field or built-in")
                    if name in secret_keys:
                        raise _err(f"request {r.name!r}", f"secret field {{{name}}} can't go into a URL")
                    if flt not in FILTERS:
                        raise _err(f"request {r.name!r}", f"unknown filter |{flt}; use {', '.join(FILTERS)}")

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.base_url).hostname or ""

    @property
    def display_name(self) -> str:
        return f"{self.name} (iFlow integration v{self.version})"

    def to_dict(self) -> dict[str, Any]:
        return {"format": SCHEMA_FORMAT, "id": self.id, "version": self.version, "name": self.name,
                "platform": self.platform, "description": self.description, "base_url": self.base_url,
                "auth": self.auth.to_dict(), "fields": [f.to_dict() for f in self.fields],
                "requests": [r.to_dict() for r in self.requests], "notes": self.notes}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> IntegrationSchema:
        if not isinstance(raw, Mapping):
            raise IntegrationSchemaError("An integration schema is a JSON object")
        fmt = raw.get("format", SCHEMA_FORMAT)
        if not isinstance(fmt, int) or fmt > SCHEMA_FORMAT:
            raise IntegrationSchemaError("This integration was made for a newer iFlow Core; update the app")
        try:
            return cls(id=str(raw.get("id", "")), version=raw.get("version", 1), name=str(raw.get("name", "")),
                       platform=str(raw.get("platform", "")), base_url=str(raw.get("base_url", "")),
                       auth=AuthSpec.from_dict(raw.get("auth")),
                       fields=tuple(FormField.from_dict(f) for f in raw.get("fields") or ()),
                       requests=tuple(RequestSpec.from_dict(r) for r in raw.get("requests") or ()),
                       description=str(raw.get("description", ""))[:500], notes=str(raw.get("notes", ""))[:2000])
        except IntegrationSchemaError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise IntegrationSchemaError(f"Schema is incomplete or invalid: {exc}") from None


def parse_all(raw: Iterable[Mapping[str, Any]]) -> tuple[list[IntegrationSchema], list[str]]:
    """(valid schemas, reasons for the ones skipped): one bad entry never blocks the others."""
    ok, skipped = [], []
    for item in raw:
        try:
            ok.append(IntegrationSchema.from_dict(item))
        except IntegrationSchemaError as exc:
            skipped.append(f"{item.get('id', '?') if isinstance(item, Mapping) else '?'}: {exc}")
    return ok, skipped
