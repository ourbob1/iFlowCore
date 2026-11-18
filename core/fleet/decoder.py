"""
VehicleDecoder — NHTSA base decode, DataOne enrichment, local cache.

    decoder = VehicleDecoder(
        nhtsa=NHTSADecoder(),
        dataone=DataOneClient(creds) if creds else None,
        cache=DecodeCache(db),
    )
    profile = decoder.decode("1HGCM82633A004352", trim_hint="EX-L")

Behavior
--------
* The VIN is validated offline first. A failed check digit still gets the
  free NHTSA decode (which reports the problem) but never a paid DataOne call.
* NHTSA is the base layer; DataOne fields override it when present.
* If DataOne returns several trims and none can be chosen (no `style_id`,
  no matching `trim_hint`), only attributes common to all trims are applied
  and the candidates are returned in `trim_candidates` for the user to pick.
* Provider failures degrade gracefully: the profile carries a warning instead
  of raising, as long as at least one provider succeeded.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

from core.fleet.cache import DecodeCache
from core.fleet.dataone import DataOneClient, DataOneResult
from core.fleet.http import ProviderAuthError, ProviderError
from core.fleet.models import VehicleProfile
from core.fleet.nhtsa import NHTSADecoder, parse_vpic
from core.fleet.vin import VinError, check_digit, normalize_vin

NHTSA_TTL = timedelta(days=90)
DATAONE_TTL = timedelta(days=365)


class VehicleDecoder:
    def __init__(
        self,
        *,
        nhtsa: NHTSADecoder | None = None,
        dataone: DataOneClient | None = None,
        cache: DecodeCache | None = None,
    ) -> None:
        self.nhtsa = nhtsa or NHTSADecoder()
        self.dataone = dataone
        self.cache = cache

    def _cached(self, provider: str, vin: str, fetch: Any, ttl: timedelta, key: str = "") -> Any:
        if self.cache is None:
            return fetch()
        payload, _ = self.cache.get_or_fetch(provider, vin, fetch, ttl=ttl, request_key=key)
        return payload

    def decode(
        self,
        vin: str,
        *,
        style_id: str | None = None,
        trim_hint: str | None = None,
        use_dataone: bool = True,
    ) -> VehicleProfile:
        vin = normalize_vin(vin)
        warnings: list[str] = []
        check_ok = vin[8] == check_digit(vin)
        if not check_ok:
            warnings.append("VIN check digit is invalid (typo?)")

        # --- base layer: NHTSA ------------------------------------------------
        try:
            payload = self._cached("nhtsa", vin, lambda: self.nhtsa.fetch(vin), NHTSA_TTL)
            profile = parse_vpic(vin, payload)
        except (ProviderError, VinError) as exc:
            warnings.append(f"NHTSA decode unavailable: {exc}")
            profile = VehicleProfile(vin=vin)

        # --- enrichment: DataOne ---------------------------------------------
        if use_dataone and self.dataone is not None:
            if not check_ok:
                warnings.append("Skipped DataOne lookup to avoid a billed decode of an invalid VIN")
            else:
                profile = self._enrich(profile, vin, style_id, trim_hint, warnings)

        if not profile.sources:
            raise ProviderError(f"No provider could decode {vin}: {'; '.join(warnings)}")
        return replace(profile, warnings=(*profile.warnings, *warnings))

    def _enrich(
        self, profile: VehicleProfile, vin: str, style_id: str | None, trim_hint: str | None, warnings: list[str]
    ) -> VehicleProfile:
        client = self.dataone
        assert client is not None
        try:
            payload = self._cached("dataone", vin, lambda: client.fetch(vin), DATAONE_TTL, client.request_key)
            result: DataOneResult = client.parse(vin, payload)
        except ProviderAuthError as exc:
            warnings.append(f"DataOne credentials rejected: {exc}")
            return profile
        except (ProviderError, VinError) as exc:
            warnings.append(f"DataOne enrichment unavailable: {exc}")
            return profile

        warnings.extend(result.warnings)
        if not result.styles:
            return profile
        chosen = result.select_style(style_id=style_id, trim=trim_hint)
        if chosen is not None:
            profile = profile.overlay(chosen, "dataone")
            return replace(profile, trim_candidates=result.styles if not result.exact else ())
        common = result.common_style()
        if common is not None:
            profile = profile.overlay(common, "dataone")
        warnings.append(f"VIN matches {len(result.styles)} trims; choose one to complete the profile")
        return replace(profile, style_id=None, trim_candidates=result.styles)
