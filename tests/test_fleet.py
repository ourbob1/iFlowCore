"""Vehicle data: VIN validation, NHTSA base decode, DataOne client, decoder facade.

No network: a FakeTransport records requests and replays canned responses.
The DataOne fixture follows the wire format documented in core/fleet/dataone.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.config import ConfigError
from core.fleet.cache import DecodeCache
from core.fleet.dataone import (
    DEFAULT_DATA_PACKS,
    DataOneClient,
    DataOneCredentials,
    parse_response,
)
from core.fleet.decoder import VehicleDecoder
from core.fleet.http import HttpResponse, ProviderAuthError, ProviderError, send_with_retry
from core.fleet.models import (
    EquipmentMapper,
    MaintenanceItem,
    maintenance_due,
    miles_to_meters,
    normalize_drive,
    normalize_fuel,
)
from core.fleet.nhtsa import NHTSADecoder, parse_vpic
from core.fleet.vin import VinError, check_digit, model_year_candidates, validate_vin

FIXTURES = Path(__file__).parent / "fixtures"
HONDA = "1HGCM82633A004352"
CAMRY = "4T1B11HKXKU000001"
CAMRY_TYPO = "4T1B11HK1KU000001"      # wrong check digit
CREDS = DataOneCredentials("client-123", "secret-abc")


def dataone_payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "dataone_decode_multi_trim.json").read_text())


def vpic_payload(**overrides: str) -> dict[str, Any]:
    result = {
        "ErrorCode": "0", "ErrorText": "0 - VIN decoded clean. Check Digit (9th position) is correct",
        "Make": "TOYOTA", "Model": "Camry", "ModelYear": "2019", "Trim": "", "Series": "",
        "BodyClass": "Sedan/Saloon", "Doors": "4", "DriveType": "FWD/Front-Wheel Drive",
        "FuelTypePrimary": "Gasoline", "FuelTypeSecondary": "", "ElectrificationLevel": "",
        "EngineCylinders": "4", "DisplacementL": "2.5", "DisplacementCC": "2500.0", "Seats": "",
        "BlindSpotMon": "", "AdaptiveCruiseControl": "Standard", "ForwardCollisionWarning": "Standard",
        "CIB": "Standard", "LaneDepartureWarning": "Standard", "LaneKeepSystem": "",
        "RearVisibilitySystem": "Standard", "KeylessIgnition": "Not Applicable",
    }
    result.update(overrides)
    return {"Count": 1, "Message": "Results returned successfully", "Results": [result]}


@dataclass
class FakeTransport:
    responses: list[HttpResponse]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected extra HTTP call")
        return self.responses.pop(0)


def ok(payload: Any) -> HttpResponse:
    return HttpResponse(200, json.dumps(payload).encode())


def no_sleep(_s: float) -> None:
    pass


# --- VIN ------------------------------------------------------------------------------


def test_vin_check_digit_and_normalization():
    assert check_digit(HONDA) == "3"
    assert validate_vin(" 1hgcm8-2633a004352 ") == HONDA
    with pytest.raises(VinError, match="check digit"):
        validate_vin(CAMRY_TYPO)
    assert validate_vin(CAMRY_TYPO, require_check_digit=False) == CAMRY_TYPO


@pytest.mark.parametrize("bad,msg", [("1HGCM82633A00435", "17"), ("1HGCM82633A0O4352", "invalid"), (123, "string")])
def test_vin_rejects_malformed(bad, msg):
    with pytest.raises(VinError, match=msg):
        validate_vin(bad)  # type: ignore[arg-type]


def test_model_year_candidates():
    assert model_year_candidates(HONDA) == (2003,)
    assert model_year_candidates(CAMRY, max_year=2027) == (2019,)
    assert model_year_candidates(CAMRY, max_year=2050) == (2049, 2019)


# --- normalizers & maintenance ------------------------------------------------------------


@pytest.mark.parametrize("texts,expected", [
    (("Gasoline",), "gasoline"),
    (("Gasoline", "Electric"), "hybrid"),
    (("Electric", None, "BEV (Battery Electric Vehicle)"), "electric"),
    (("Gasoline", "Electric", "PHEV (Plug-in Hybrid Electric Vehicle)"), "plug_in_hybrid"),
    (("Flexible Fuel Vehicle (FFV)",), "flex_fuel"),
    (("Diesel",), "diesel"),
    ((None, ""), None),
])
def test_normalize_fuel(texts, expected):
    assert normalize_fuel(*texts) == expected


def test_normalize_drive():
    assert normalize_drive("FWD/Front-Wheel Drive") == "fwd"
    assert normalize_drive("AWD/All-Wheel Drive") == "awd"
    assert normalize_drive("4WD/4-Wheel Drive/4x4") == "4wd"
    assert normalize_drive(None) is None


def test_equipment_mapper_and_extension():
    mapper = EquipmentMapper(extra={"ev_charging_port": ("ccs", "nacs")})
    found = mapper.map_items(["Safety Blind Spot Monitor", "Apple CarPlay/Android Auto", "NACS charge port"])
    assert found == {"blind_spot_monitor", "apple_carplay", "android_auto", "ev_charging_port"}
    assert mapper.map_text("usbc") == set()                      # word-boundary guard


def test_miles_to_meters_exact():
    assert miles_to_meters(1) == 1609
    assert miles_to_meters(5000) == 8_046_720
    assert miles_to_meters("10000") == 16_093_440


def test_maintenance_due_statuses():
    items = [
        MaintenanceItem("Oil", interval_meters=miles_to_meters(10_000), interval_months=12,
                        severe_interval_meters=miles_to_meters(5_000), severe_interval_months=6),
        MaintenanceItem("Tires", interval_meters=miles_to_meters(5_000)),
        MaintenanceItem("Cabin filter", interval_meters=miles_to_meters(30_000)),
    ]
    today = date(2026, 9, 26)
    last = {"Oil": (miles_to_meters(40_000), date(2026, 5, 1)), "Tires": (miles_to_meters(40_300), date(2026, 8, 1))}
    due = maintenance_due(items, odometer_m=miles_to_meters(45_000), today=today, last_service=last)
    status = {d.item.service: d.status for d in due}
    assert status == {"Oil": "ok", "Tires": "due_soon", "Cabin filter": "overdue"}   # never done → from 0

    severe = {d.item.service: d for d in maintenance_due(items, odometer_m=miles_to_meters(45_000), today=today,
                                                         last_service=last, severe=True)}
    assert severe["Oil"].status == "overdue"                   # 5k-mile severe interval reached
    assert severe["Oil"].remaining_meters == 0
    assert [d.status for d in due] == ["overdue", "due_soon", "ok"]   # sorted by urgency

    with pytest.raises(ValueError):
        MaintenanceItem("Nothing")


# --- HTTP retry --------------------------------------------------------------------------


def test_retry_on_429_then_success_honors_retry_after():
    slept: list[float] = []
    t = FakeTransport([HttpResponse(429, b"", {"Retry-After": "2"}), HttpResponse(503, b""), ok({"x": 1})])
    resp = send_with_retry(t, "GET", "https://x.test", sleep=slept.append, retries=2, backoff_s=0.5)
    assert resp.status == 200 and slept == [2.0, 1.0]


def test_retry_gives_up():
    t = FakeTransport([HttpResponse(500, b"")] * 3)
    assert send_with_retry(t, "GET", "https://x.test", sleep=no_sleep, retries=2).status == 500


# --- NHTSA -----------------------------------------------------------------------------


def test_parse_vpic():
    profile = parse_vpic(CAMRY, vpic_payload())
    assert (profile.year, profile.make, profile.model) == (2019, "TOYOTA", "Camry")
    assert profile.trim is None                               # vPIC often lacks trims
    assert profile.fuel_type == "gasoline" and profile.drive_type == "fwd"
    assert profile.engine_displacement_ml == 2500 and profile.engine_cylinders == 4
    assert profile.equipment == {"adaptive_cruise", "forward_collision_warning", "automatic_emergency_braking",
                                 "lane_departure_warning", "backup_camera"}
    assert profile.sources == ("nhtsa",) and profile.warnings == ()


def test_parse_vpic_errors_and_warnings():
    warn = parse_vpic(CAMRY, vpic_payload(ErrorCode="1", ErrorText="1 - Check Digit (9th position) does not calculate properly"))
    assert "Check Digit" in warn.warnings[0]
    with pytest.raises(ProviderError):
        parse_vpic(CAMRY, vpic_payload(Make="", ErrorCode="8", ErrorText="8 - No detailed data available"))
    with pytest.raises(ProviderError):
        parse_vpic(CAMRY, {"Results": []})


def test_nhtsa_request_shape():
    t = FakeTransport([ok(vpic_payload())])
    NHTSADecoder(t).decode(CAMRY, model_year=2019)
    call = t.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValuesExtended/{CAMRY}"
    assert call["params"] == {"format": "json", "modelyear": "2019"}


# --- DataOne: client & parsing -----------------------------------------------------------


def test_credentials_are_masked_and_loaded_from_env():
    assert "secret" not in repr(CREDS)
    creds = DataOneCredentials.from_env({"IFLOW_DATAONE_CLIENT_ID": "c", "IFLOW_DATAONE_AUTH_CODE": "s"})
    assert creds.client_id == "c"
    assert DataOneCredentials.from_env({}) is None
    with pytest.raises(ProviderAuthError):
        DataOneCredentials.from_env({"IFLOW_DATAONE_CLIENT_ID": "c"})


def test_dataone_request_shape():
    t = FakeTransport([ok(dataone_payload())])
    client = DataOneClient(CREDS, transport=t, data_packs=("basic_data", "installed_equipment", "maintenance"))
    client.decode(CAMRY)
    call = t.calls[0]
    assert call["method"] == "POST" and call["url"].startswith("https://")
    form = call["form"]
    assert form["client_id"] == "client-123" and form["authorization_code"] == "secret-abc"
    query = json.loads(form["decoder_query"])
    assert query["query_requests"] == {"iFlowCore-1": {"vin": CAMRY}}
    assert query["decoder_settings"]["style_data_packs"] == {"basic_data": "on", "installed_equipment": "on", "maintenance": "on"}
    assert client.request_key == "basic_data,installed_equipment,maintenance"


def test_dataone_refuses_invalid_vin_before_billing():
    t = FakeTransport([])
    with pytest.raises(VinError):
        DataOneClient(CREDS, transport=t).decode(CAMRY_TYPO)
    assert t.calls == []


def test_dataone_requires_https():
    with pytest.raises(ConfigError):
        DataOneClient(CREDS, endpoint="http://api.example.com/decode")


def test_parse_trims_equipment_pricing_maintenance():
    result = parse_response(CAMRY, dataone_payload())
    assert not result.exact and len(result.styles) == 2
    le, xle = result.styles
    assert (le.style_id, le.trim, le.year, le.make, le.model) == ("401738771", "LE", 2019, "Toyota", "Camry")
    assert le.msrp_minor == 2_442_500 and xle.msrp_minor == 2_900_000        # integer cents
    assert (le.mpg_city, le.mpg_highway, le.mpg_combined) == (29, 41, 34)
    assert le.seating_capacity == 5 and le.engine_displacement_ml == 2500 and le.engine_cylinders == 4
    assert le.fuel_type == "gasoline" and le.drive_type == "fwd"

    assert {"automatic_emergency_braking", "lane_departure_warning", "adaptive_cruise", "backup_camera",
            "bluetooth"} <= le.equipment
    assert "blind_spot_monitor" not in le.equipment          # value "No" is not installed
    assert "apple_carplay" not in le.equipment               # "N/A"
    assert {"blind_spot_monitor", "heated_seats", "leather_seats", "sunroof"} <= xle.equipment   # nested groups

    oil = next(m for m in le.maintenance if m.service == "Engine oil & filter")
    assert oil.interval_meters == 16_093_440 and oil.interval_months == 12
    assert oil.severe_interval_meters == 8_046_720 and oil.severe_interval_months == 6
    assert oil.category == "engine"
    assert [m.service for m in le.maintenance] == ["Engine oil & filter", "Rotate tires", "Cabin air filter"]


def test_style_selection():
    result = parse_response(CAMRY, dataone_payload())
    assert result.select_style(style_id="401738775").trim == "XLE"
    assert result.select_style(trim="le").trim == "LE"                     # exact beats substring of XLE
    assert result.select_style(trim="xle").trim == "XLE"
    assert result.select_style() is None                                   # ambiguous
    with pytest.raises(ProviderError):
        result.select_style(style_id="999")
    common = result.common_style()
    assert common.make == "Toyota" and common.trim is None
    assert common.equipment == {"automatic_emergency_braking", "forward_collision_warning", "adaptive_cruise",
                                "backup_camera"}


@pytest.mark.parametrize("payload,exc,msg", [
    ({"decoder_messages": {"decoder_errors": [{"error_message": "Invalid client_id or authorization code"}]}},
     ProviderAuthError, "credentials"),
    ({"decoder_messages": {"decoder_errors": ["Service unavailable"]}}, ProviderError, "decoder error"),
    ({"query_responses": {"iFlowCore-1": {"query_error": {"error_code": "IV", "error_message": "Invalid VIN"}}}},
     ProviderError, "Invalid VIN"),
    ({"query_responses": {}}, ProviderError, "no query_responses"),
])
def test_dataone_error_payloads(payload, exc, msg):
    with pytest.raises(exc, match=msg):
        parse_response(CAMRY, payload)


def test_dataone_http_auth_failure():
    t = FakeTransport([HttpResponse(401, b"")])
    with pytest.raises(ProviderAuthError):
        DataOneClient(CREDS, transport=t, sleep=no_sleep).decode(CAMRY)


def test_default_packs_include_maintenance_and_equipment():
    assert {"installed_equipment", "maintenance", "basic_data"} <= set(DEFAULT_DATA_PACKS)


# --- decoder facade + cache ----------------------------------------------------------------


def make_decoder(db, nhtsa_responses, dataone_responses, clock=None):
    nt, dt = FakeTransport(list(nhtsa_responses)), FakeTransport(list(dataone_responses))
    cache = DecodeCache(db, clock=clock) if clock else DecodeCache(db)
    decoder = VehicleDecoder(
        nhtsa=NHTSADecoder(nt, sleep=no_sleep),
        dataone=DataOneClient(CREDS, transport=dt, sleep=no_sleep),
        cache=cache,
    )
    return decoder, nt, dt


def test_decoder_merges_nhtsa_and_chosen_trim(db):
    decoder, nt, dt = make_decoder(db, [ok(vpic_payload())], [ok(dataone_payload())])
    profile = decoder.decode(CAMRY, trim_hint="XLE")
    assert profile.sources == ("nhtsa", "dataone")
    assert profile.make == "Toyota"                        # DataOne overrides NHTSA casing
    assert profile.trim == "XLE" and profile.style_id == "401738775"
    assert profile.msrp_minor == 2_900_000 and profile.currency == "USD"
    assert {"heated_seats", "forward_collision_warning"} <= profile.equipment   # union of both providers
    assert profile.maintenance and len(profile.trim_candidates) == 2
    assert profile.label == "2019 Toyota Camry XLE"


def test_decoder_ambiguous_trim_applies_only_common_data(db):
    decoder, *_ = make_decoder(db, [ok(vpic_payload())], [ok(dataone_payload())])
    profile = decoder.decode(CAMRY)
    assert profile.trim is None and profile.style_id is None and profile.msrp_minor is None
    assert len(profile.trim_candidates) == 2
    assert any("2 trims" in w for w in profile.warnings)


def test_decoder_caches_paid_lookups(db):
    decoder, nt, dt = make_decoder(db, [ok(vpic_payload())], [ok(dataone_payload())])
    first = decoder.decode(CAMRY, trim_hint="LE")
    second = decoder.decode(CAMRY, style_id="401738775")    # different trim, same cached payload
    assert len(nt.calls) == 1 and len(dt.calls) == 1
    assert (first.trim, second.trim) == ("LE", "XLE")


def test_cache_expiry_and_purge(db):
    now = [datetime(2026, 9, 26, tzinfo=timezone.utc)]
    decoder, nt, dt = make_decoder(db, [ok(vpic_payload())] * 2, [ok(dataone_payload())] * 2, clock=lambda: now[0])
    decoder.decode(CAMRY, trim_hint="LE")
    now[0] += timedelta(days=100)                          # NHTSA TTL (90d) expired, DataOne (365d) not
    decoder.decode(CAMRY, trim_hint="LE")
    assert (len(nt.calls), len(dt.calls)) == (2, 1)
    now[0] += timedelta(days=400)
    assert decoder.cache.purge_expired() == 2


def test_invalid_check_digit_skips_paid_lookup(db):
    decoder, nt, dt = make_decoder(db, [ok(vpic_payload(ErrorCode="1", ErrorText="1 - Check Digit error"))], [])
    profile = decoder.decode(CAMRY_TYPO)
    assert dt.calls == [] and profile.sources == ("nhtsa",)
    assert any("billed" in w for w in profile.warnings)


def test_dataone_failure_degrades_to_nhtsa(db):
    decoder, nt, dt = make_decoder(db, [ok(vpic_payload())], [HttpResponse(403, b"")])
    profile = decoder.decode(CAMRY)
    assert profile.sources == ("nhtsa",) and profile.make == "TOYOTA"
    assert any("credentials rejected" in w for w in profile.warnings)
    with db.connection() as conn:                          # failed responses are never cached
        assert conn.execute("SELECT count(*) FROM vehicle_decode_cache WHERE provider='dataone'").fetchone()[0] == 0


def test_all_providers_failing_raises(db):
    decoder, *_ = make_decoder(db, [HttpResponse(500, b"")] * 3, [HttpResponse(500, b"")] * 3)
    with pytest.raises(ProviderError, match="No provider"):
        decoder.decode(CAMRY)


def test_nhtsa_only_mode(db):
    decoder = VehicleDecoder(nhtsa=NHTSADecoder(FakeTransport([ok(vpic_payload())])), cache=DecodeCache(db))
    assert decoder.decode(CAMRY).sources == ("nhtsa",)
