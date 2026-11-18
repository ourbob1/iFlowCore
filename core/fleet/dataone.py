"""
DataOne Software VIN decoder client — trims, equipment, maintenance.

DataOne enriches the free NHTSA vPIC decode (core/fleet/nhtsa.py) with:
  * trim/style resolution — a VIN often maps to several trims; DataOne
    returns each candidate style with its own data
  * installed equipment — normalized here to stable keys (EquipmentMapper)
  * pricing (MSRP → integer minor units), fuel economy, seating
  * OEM maintenance schedules → `MaintenanceItem`s with meter/month intervals

⚠️  Wire-format status
---------------------
DataOne's API reference is only distributed to account holders (trial or
contract); it is not public. The request/response shapes below follow
DataOne's JSON decoder web service as we understand it (form POST of
`client_id`, `authorization_code`, and a JSON `decoder_query`; response
`query_responses → <request id> → us_market_data → us_styles[]`). Every
assumption lives in `WIRE` and the `_parse_*` helpers of this module, and the
parser accepts common alternate key names. Before production use, record one
real response from your account into tests/fixtures and adjust `WIRE` if
anything differs — nothing outside this module depends on the wire format.

Cost control
------------
DataOne bills per decode. Callers should go through
`core.fleet.decoder.VehicleDecoder`, which validates the VIN check digit
offline first and serves repeats from the encrypted local cache.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from core.config import ConfigError, normalize_currency
from core.financial.money import MoneyError, parse_amount
from core.fleet.http import ProviderAuthError, ProviderError, Transport, default_transport, send_with_retry
from core.fleet.models import (
    EquipmentMapper,
    MaintenanceItem,
    VehicleStyle,
    miles_to_meters,
    normalize_drive,
    normalize_fuel,
)
from core.fleet.vin import validate_vin

PROVIDER = "dataone"

# ---------------------------------------------------------------------------
# Wire format (single source of truth — see module docstring)
# ---------------------------------------------------------------------------

WIRE: Mapping[str, Any] = {
    "endpoint": "https://api.dataonesoftware.com/webservices/vindecoder/decode",
    "form_client_id": "client_id",
    "form_auth": "authorization_code",
    "form_query": "decoder_query",
    "request_id": "iFlowCore-1",
    "decoder_version": "7.2.0",
}

# Data packs requested per style. Trim this list to what your contract
# includes; requesting packs you don't license may error or cost more.
DEFAULT_DATA_PACKS: tuple[str, ...] = (
    "basic_data",
    "pricing",
    "engines",
    "transmissions",
    "specifications",
    "installed_equipment",
    "fuel_efficiency",
    "maintenance",
)

ENV_CLIENT_ID = "IFLOW_DATAONE_CLIENT_ID"
ENV_AUTH_CODE = "IFLOW_DATAONE_AUTH_CODE"
ENV_ENDPOINT = "IFLOW_DATAONE_URL"

_NEGATIVE_VALUES = {"no", "none", "n/a", "na", "not available", "-", "0", "false"}


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DataOneCredentials:
    client_id: str
    authorization_code: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.client_id.strip() or not self.authorization_code.strip():
            raise ProviderAuthError("DataOne client_id and authorization_code are required")

    def __repr__(self) -> str:  # never leak the secret into logs
        return f"DataOneCredentials(client_id={self.client_id!r}, authorization_code='***')"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DataOneCredentials | None":
        env = os.environ if env is None else env
        cid, code = env.get(ENV_CLIENT_ID, "").strip(), env.get(ENV_AUTH_CODE, "").strip()
        if not cid and not code:
            return None
        if not cid or not code:
            raise ProviderAuthError(f"Set both {ENV_CLIENT_ID} and {ENV_AUTH_CODE}")
        return cls(cid, code)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DataOneResult:
    vin: str
    styles: tuple[VehicleStyle, ...]
    warnings: tuple[str, ...] = ()

    @property
    def exact(self) -> bool:
        """True when the VIN resolves to a single trim/style."""
        return len(self.styles) == 1

    def select_style(self, *, style_id: str | None = None, trim: str | None = None) -> VehicleStyle | None:
        """Pick a style by id, else by trim name, else the only one; None if ambiguous."""
        if style_id is not None:
            for s in self.styles:
                if s.style_id == str(style_id):
                    return s
            raise ProviderError(f"Style {style_id} is not a candidate for VIN {self.vin}")
        if trim:
            wanted = _norm(trim)
            exact = [s for s in self.styles if _norm(s.trim or "") == wanted or _norm(s.name) == wanted]
            if len(exact) == 1:
                return exact[0]
            partial = [s for s in self.styles if wanted and (wanted in _norm(s.trim or "") or wanted in _norm(s.name))]
            if len(partial) == 1:
                return partial[0]
        return self.styles[0] if self.exact else None

    def common_style(self) -> VehicleStyle | None:
        """Attributes shared by *every* candidate — safe to use before the
        user picks a trim. Equipment is the intersection."""
        if not self.styles:
            return None
        first = self.styles[0]
        values: dict[str, Any] = {}
        for name in ("year", "make", "model", "body_type", "doors", "drive_type", "fuel_type",
                     "engine_cylinders", "engine_displacement_ml", "seating_capacity"):
            candidates = {getattr(s, name) for s in self.styles}
            values[name] = candidates.pop() if len(candidates) == 1 else None
        equipment = frozenset.intersection(*(s.equipment for s in self.styles))
        maint = first.maintenance if all(s.maintenance == first.maintenance for s in self.styles) else ()
        return VehicleStyle(style_id="", name="(common)", equipment=equipment, maintenance=maint, **values)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


# ---------------------------------------------------------------------------
# Tolerant field access
# ---------------------------------------------------------------------------


def _first(data: Mapping[str, Any] | None, *keys: str) -> Any:
    if not isinstance(data, Mapping):
        return None
    for k in keys:
        v = data.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        # Some payloads wrap lists: {"engine": [...]} or keyed dicts {"1": {...}}.
        inner = [v for v in value.values() if isinstance(v, (list, Mapping))]
        if len(inner) == 1 and isinstance(inner[0], list):
            return inner[0]
        return [v for v in value.values() if isinstance(v, Mapping)] or [value]
    return [value]


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return None
    try:
        return int(Decimal(match.group()).to_integral_value())
    except InvalidOperation:
        return None


def _displacement_ml(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    number = Decimal(match.group())
    text = str(value).casefold()
    if "cc" in text or "ml" in text or number >= 50:     # already cubic centimeters
        return int(number.to_integral_value())
    return int((number * 1000).to_integral_value())      # liters


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_equipment(style: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for item in _as_list(_first(style, "installed_equipment", "standard_equipment", "features")):
        if isinstance(item, str):
            lines.append(item)
            continue
        if not isinstance(item, Mapping):
            continue
        # Nested category groups: {"category": "Safety", "equipment": [...]}
        nested = _first(item, "equipment", "items", "features")
        if isinstance(nested, list):
            category = _first(item, "category", "group") or ""
            for sub in nested:
                if isinstance(sub, Mapping):
                    sub = {**sub, "category": sub.get("category") or category}
                lines.extend(_parse_equipment({"installed_equipment": [sub]}))
            continue
        value = str(_first(item, "value", "attribute", "availability") or "").strip()
        if value.casefold() in _NEGATIVE_VALUES:
            continue
        name = _first(item, "name", "item", "description") or ""
        category = _first(item, "category", "group") or ""
        lines.append(" ".join(p for p in (str(category), str(name), value) if p).strip())
    return [l for l in lines if l]


def _parse_maintenance(style: Mapping[str, Any]) -> tuple[MaintenanceItem, ...]:
    items: list[MaintenanceItem] = []
    for raw in _as_list(_first(style, "maintenance", "maintenance_schedule", "service_schedule")):
        if not isinstance(raw, Mapping):
            continue
        service = _first(raw, "service", "name", "operation", "description")
        if not service:
            continue
        miles = _to_int(_first(raw, "interval_miles", "miles", "mileage_interval"))
        km = _to_int(_first(raw, "interval_km", "kilometers"))
        severe_miles = _to_int(_first(raw, "severe_interval_miles", "severe_miles"))
        meters = miles_to_meters(miles) if miles else (km * 1000 if km else None)
        try:
            items.append(MaintenanceItem(
                service=str(service).strip(),
                category=str(_first(raw, "category", "type", "system") or "general").strip().casefold(),
                interval_meters=meters,
                interval_months=_to_int(_first(raw, "interval_months", "months")),
                severe_interval_meters=miles_to_meters(severe_miles) if severe_miles else None,
                severe_interval_months=_to_int(_first(raw, "severe_interval_months", "severe_months")),
                notes=str(_first(raw, "notes", "comment") or ""),
            ))
        except ValueError:
            continue     # item without any interval — not schedulable
    return tuple(items)


def _parse_seating(style: Mapping[str, Any]) -> int | None:
    direct = _to_int(_first(_first(style, "basic_data") or {}, "seating", "seating_capacity", "std_seating"))
    if direct:
        return direct
    for group in _as_list(_first(style, "specifications")):
        if not isinstance(group, Mapping):
            continue
        for spec in _as_list(_first(group, "specifications", "specs")) or [group]:
            if not isinstance(spec, Mapping):
                continue
            name = str(_first(spec, "name", "specification") or "").casefold()
            if "seating" in name and "capacity" in name:
                measurements = _as_list(_first(spec, "measurements"))
                value = _first(measurements[0], "value") if measurements else _first(spec, "value")
                return _to_int(value)
    return None


def _parse_style(raw: Mapping[str, Any], common: Mapping[str, Any], mapper: EquipmentMapper, currency: str) -> VehicleStyle:
    basic = {**(_first(common, "basic_data") or {}), **(_first(raw, "basic_data") or {})}
    style_id = _first(raw, "id", "style_id") or _first(basic, "style_id", "id")
    if style_id is None:
        raise ProviderError("DataOne style without an id")

    engines = _as_list(_first(raw, "engines") or _first(common, "engines"))
    engine = next(
        (e for e in engines if isinstance(e, Mapping)
         and str(_first(e, "installed", "availability", "order_code_availability") or "").casefold() in {"y", "yes", "true", "installed", "standard", "std"}),
        engines[0] if engines and isinstance(engines[0], Mapping) else {},
    )

    economy = _as_list(_first(raw, "fuel_efficiency") or _first(common, "fuel_efficiency"))
    mpg = economy[0] if economy and isinstance(economy[0], Mapping) else {}

    pricing = _first(raw, "pricing") or _first(common, "pricing") or {}
    msrp_text = _first(pricing, "msrp", "base_msrp")
    msrp = None
    if msrp_text not in (None, ""):
        try:
            msrp = parse_amount(str(msrp_text), currency, rounding="ROUND_HALF_UP")
        except MoneyError:
            msrp = None

    equipment_raw = _parse_equipment(raw) or _parse_equipment(common)
    return VehicleStyle(
        style_id=str(style_id),
        name=str(_first(raw, "name") or _first(basic, "style", "trim") or style_id),
        year=_to_int(_first(basic, "year", "model_year")),
        make=_first(basic, "make"),
        model=_first(basic, "model"),
        trim=_first(basic, "trim", "series"),
        body_type=_first(basic, "body_type", "body_subtype", "vehicle_type"),
        doors=_to_int(_first(basic, "doors")),
        drive_type=normalize_drive(_first(basic, "drive_type", "drivetrain")),
        fuel_type=normalize_fuel(_first(basic, "fuel_type"), _first(engine, "fuel_type", "fuel"),
                                 _first(engine, "name", "engine_type")),
        engine_cylinders=_to_int(_first(engine, "cylinders")),
        engine_displacement_ml=_displacement_ml(_first(engine, "displacement", "liters")),
        seating_capacity=_parse_seating(raw) or _parse_seating(common),
        mpg_city=_to_int(_first(mpg, "city", "fuel_efficiency_city", "mpg_city")),
        mpg_highway=_to_int(_first(mpg, "highway", "fuel_efficiency_highway", "mpg_highway")),
        mpg_combined=_to_int(_first(mpg, "combined", "fuel_efficiency_combined", "mpg_combined")),
        msrp_minor=msrp,
        currency=currency,
        equipment=mapper.map_items(equipment_raw),
        equipment_raw=tuple(equipment_raw),
        maintenance=_parse_maintenance(raw) or _parse_maintenance(common),
    )


def parse_response(
    vin: str,
    payload: Mapping[str, Any],
    *,
    request_id: str = WIRE["request_id"],
    mapper: EquipmentMapper | None = None,
    currency: str = "USD",
) -> DataOneResult:
    """Convert a DataOne decode payload into provider-neutral styles."""
    mapper = mapper or EquipmentMapper()
    currency = normalize_currency(currency)

    messages = _first(payload, "decoder_messages") or {}
    errors = [e for e in _as_list(_first(messages, "decoder_errors")) if e]
    if errors:
        text = "; ".join(str(_first(e, "error_message", "message") or e) if isinstance(e, Mapping) else str(e) for e in errors)
        if re.search(r"auth|client|credential|licen", text, re.I):
            raise ProviderAuthError(f"DataOne rejected credentials: {text}")
        raise ProviderError(f"DataOne decoder error: {text}")

    responses = _first(payload, "query_responses") or {}
    response = responses.get(request_id) if isinstance(responses, Mapping) else None
    if response is None and isinstance(responses, Mapping) and len(responses) == 1:
        response = next(iter(responses.values()))
    if not isinstance(response, Mapping):
        raise ProviderError("DataOne response has no query_responses entry")

    qerr = _first(response, "query_error") or {}
    code = str(_first(qerr, "error_code") or "").strip()
    if code:
        raise ProviderError(f"DataOne query error {code}: {_first(qerr, 'error_message') or 'unknown'}")

    market = _first(response, "us_market_data") or {}
    common = _first(market, "common_us_data") or {}
    raw_styles = _as_list(_first(market, "us_styles", "styles"))
    styles = tuple(_parse_style(s, common, mapper, currency) for s in raw_styles if isinstance(s, Mapping))
    warnings = () if styles else ("DataOne returned no styles for this VIN",)
    return DataOneResult(vin, styles, warnings)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class DataOneClient:
    provider = PROVIDER

    def __init__(
        self,
        credentials: DataOneCredentials,
        *,
        transport: Transport | None = None,
        endpoint: str | None = None,
        data_packs: Sequence[str] = DEFAULT_DATA_PACKS,
        timeout: float = 20.0,
        require_check_digit: bool = True,
        currency: str = "USD",
        mapper: EquipmentMapper | None = None,
        sleep: Any = None,
    ) -> None:
        endpoint = endpoint or os.environ.get(ENV_ENDPOINT) or WIRE["endpoint"]
        if not endpoint.startswith("https://"):
            raise ConfigError("DataOne endpoint must use https:// (credentials are sent in the request)")
        self._creds = credentials
        self._transport = transport or default_transport()
        self._endpoint = endpoint
        self._packs = tuple(dict.fromkeys(data_packs))
        self._timeout = timeout
        self._require_check_digit = require_check_digit
        self._currency = normalize_currency(currency)
        self._mapper = mapper or EquipmentMapper()
        self._sleep = sleep

    @property
    def request_key(self) -> str:
        """Cache discriminator: responses differ by requested data packs."""
        return ",".join(sorted(self._packs))

    def build_query(self, vin: str) -> dict[str, Any]:
        packs = {p: "on" for p in self._packs}
        return {
            "decoder_settings": {
                "display": "full",
                "version": WIRE["decoder_version"],
                "styles": "on",
                "style_data_packs": packs,
                "common_data": "on",
                "common_data_packs": packs,
            },
            "query_requests": {WIRE["request_id"]: {"vin": vin}},
        }

    def fetch(self, vin: str) -> dict[str, Any]:
        """Raw decode payload (one billed lookup). Prefer VehicleDecoder, which caches."""
        vin = validate_vin(vin, require_check_digit=self._require_check_digit)
        form = {
            WIRE["form_client_id"]: self._creds.client_id,
            WIRE["form_auth"]: self._creds.authorization_code,
            WIRE["form_query"]: json.dumps(self.build_query(vin), separators=(",", ":")),
        }
        kwargs: dict[str, Any] = {"form": form, "timeout": self._timeout}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        resp = send_with_retry(self._transport, "POST", self._endpoint, **kwargs)
        if resp.status in (401, 403):
            raise ProviderAuthError(f"DataOne HTTP {resp.status}: check client_id / authorization_code")
        if resp.status != 200:
            raise ProviderError(f"DataOne HTTP {resp.status}")
        payload = resp.json()
        if not isinstance(payload, Mapping):
            raise ProviderError("DataOne returned a non-object payload")
        # Surface credential/decoder errors now so bad payloads are never cached.
        parse_response(vin, payload, mapper=self._mapper, currency=self._currency)
        return dict(payload)

    def parse(self, vin: str, payload: Mapping[str, Any]) -> DataOneResult:
        return parse_response(vin, payload, mapper=self._mapper, currency=self._currency)

    def decode(self, vin: str) -> DataOneResult:
        vin = validate_vin(vin, require_check_digit=self._require_check_digit)
        return self.parse(vin, self.fetch(vin))
