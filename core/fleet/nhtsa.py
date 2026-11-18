"""
Base VIN decoder: NHTSA vPIC (free, public, no key).

Endpoint: GET https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValuesExtended/{VIN}?format=json

vPIC gives authoritative make/model/year/body/engine/fuel data and several
safety-equipment flags, but trims are often blank and there is no pricing,
full equipment list, or maintenance data — that is what DataOne adds.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from core.fleet.http import ProviderError, Transport, default_transport, send_with_retry
from core.fleet.models import EquipmentMapper, VehicleProfile, normalize_drive, normalize_fuel
from core.fleet.vin import validate_vin

VPIC_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValuesExtended/{vin}"
PROVIDER = "nhtsa"

# vPIC safety fields whose value "Standard" means the feature is fitted.
_SAFETY_FIELDS: Mapping[str, str] = {
    "BlindSpotMon": "blind spot monitor",
    "AdaptiveCruiseControl": "adaptive cruise",
    "ForwardCollisionWarning": "forward collision warning",
    "CIB": "crash imminent braking",
    "LaneDepartureWarning": "lane departure warning",
    "LaneKeepSystem": "lane keep",
    "RearVisibilitySystem": "backup camera",
    "KeylessIgnition": "keyless entry",
}


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if text in ("", "Not Applicable", "null") else text


def _int(value: Any) -> int | None:
    text = _clean(value)
    if text is None:
        return None
    try:
        return int(Decimal(text))
    except (InvalidOperation, ValueError):
        return None


def parse_vpic(vin: str, payload: Mapping[str, Any], mapper: EquipmentMapper | None = None) -> VehicleProfile:
    """Convert a vPIC DecodeVinValuesExtended response into a VehicleProfile."""
    results = payload.get("Results") or []
    if not results:
        raise ProviderError("vPIC returned no results")
    r = results[0]
    mapper = mapper or EquipmentMapper()

    warnings: list[str] = []
    codes = {c.strip() for c in str(r.get("ErrorCode", "0")).split(",") if c.strip()}
    if codes - {"0"}:
        warnings.append(f"NHTSA: {_clean(r.get('ErrorText')) or 'decode warning ' + ','.join(sorted(codes))}")
    if not _clean(r.get("Make")):
        raise ProviderError(f"NHTSA could not decode VIN: {_clean(r.get('ErrorText')) or 'no make'}")

    displacement_ml = None
    cc = _clean(r.get("DisplacementCC"))
    liters = _clean(r.get("DisplacementL"))
    try:
        if cc:
            displacement_ml = int(Decimal(cc).to_integral_value())
        elif liters:
            displacement_ml = int((Decimal(liters) * 1000).to_integral_value())
    except InvalidOperation:
        pass

    fitted = [label for field, label in _SAFETY_FIELDS.items() if (_clean(r.get(field)) or "").casefold() == "standard"]

    return VehicleProfile(
        vin=vin,
        year=_int(r.get("ModelYear")),
        make=_clean(r.get("Make")),
        model=_clean(r.get("Model")),
        trim=_clean(r.get("Trim")),
        series=_clean(r.get("Series")),
        body_type=_clean(r.get("BodyClass")),
        doors=_int(r.get("Doors")),
        drive_type=normalize_drive(_clean(r.get("DriveType"))),
        fuel_type=normalize_fuel(
            _clean(r.get("FuelTypePrimary")), _clean(r.get("FuelTypeSecondary")), _clean(r.get("ElectrificationLevel"))
        ),
        engine_cylinders=_int(r.get("EngineCylinders")),
        engine_displacement_ml=displacement_ml,
        seating_capacity=_int(r.get("Seats")),
        equipment=mapper.map_items(fitted),
        sources=(PROVIDER,),
        warnings=tuple(warnings),
    )


class NHTSADecoder:
    provider = PROVIDER

    def __init__(self, transport: Transport | None = None, *, url: str = VPIC_URL, timeout: float = 15.0,
                 sleep: Any = None) -> None:
        self._transport = transport or default_transport()
        self._url = url
        self._timeout = timeout
        self._sleep = sleep

    def fetch(self, vin: str, *, model_year: int | None = None) -> dict[str, Any]:
        """Raw vPIC JSON (cacheable)."""
        vin = validate_vin(vin, require_check_digit=False)
        params = {"format": "json"}
        if model_year:
            params["modelyear"] = str(model_year)
        kwargs: dict[str, Any] = {"params": params, "timeout": self._timeout}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        resp = send_with_retry(self._transport, "GET", self._url.format(vin=vin), **kwargs)
        if resp.status != 200:
            raise ProviderError(f"NHTSA vPIC HTTP {resp.status}")
        return resp.json()

    def decode(self, vin: str, *, model_year: int | None = None) -> VehicleProfile:
        vin = validate_vin(vin, require_check_digit=False)
        return parse_vpic(vin, self.fetch(vin, model_year=model_year))
