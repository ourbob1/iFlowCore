"""Phase 11: platform API integrations (Integration Studio → signed bundle → connection wizard → pull).

Schema validation, form values → request URLs, a mock Uber API normalised into integer-cent
transactions, the safety rules (host, redirects, secrets, sizes), OAuth (direct and via the iFlow
token broker), bundle install, the admin routes, the Studio and the user's wizard end to end."""

from __future__ import annotations

import copy
import json
import urllib.parse
import uuid
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from core.financial.ingestion import import_records
from core.financial.parsers import dynamic
from core.fleet.http import HttpResponse
from core.integration import engine, oauth
from core.integration.schema import IntegrationSchema, IntegrationSchemaError
from core.integration.templates import UBER_DRIVER

from .test_account_api import Harness  # noqa: F401,E402  (puts api/ on sys.path for the broker tests)

NY = ZoneInfo("America/New_York")
TODAY = date(2026, 10, 8)
KEY = Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes_raw().hex()

TRIPS = {"count": 2, "limit": 50, "offset": 0, "trips": [
    {"trip_id": "t-1", "distance": 4.21, "duration": 960, "status": "completed",
     "pickup": {"timestamp": 1790000000}, "dropoff": {"timestamp": 1790000960}},
    {"trip_id": "t-2", "distance": 11.5, "duration": 1800, "status": "completed",
     "pickup": {"timestamp": 1790010000}, "dropoff": {"timestamp": 1790011800}}]}
PAYMENTS = {"count": 5, "limit": 50, "offset": 0, "payments": [
    {"payment_id": "p-1", "category": "fare", "event_time": 1790001000, "trip_id": "t-1", "amount": 18.00,
     "currency_code": "USD", "breakdown": {"service_fee": -6.13, "toll": 2.25, "other": 0}},
    {"payment_id": "p-2", "category": "tip", "event_time": 1790002000, "trip_id": "t-1", "amount": 5.00,
     "currency_code": "USD", "breakdown": {}},
    {"payment_id": "p-3", "category": "fare", "event_time": 1790012000, "trip_id": "t-2", "amount": 31.10,
     "currency_code": "USD", "breakdown": {"service_fee": -9.9, "toll": 0}},
    {"payment_id": "p-4", "category": "promotion", "event_time": 1790020000, "amount": 15,
     "currency_code": "USD"},
    {"payment_id": "p-5", "category": "cash_collected", "event_time": 1790021000, "amount": -12,
     "currency_code": "USD"}]}


def uber(**changes):
    raw = copy.deepcopy(UBER_DRIVER)
    raw["auth"]["client_id"] = "uber-test-client"
    raw.update(changes)
    return IntegrationSchema.from_dict(raw)


class FakeUber:
    """api.uber.com with offset pagination, plus a request log (method, url, headers)."""

    def __init__(self, token="tok-1", trips=TRIPS, payments=PAYMENTS, page=50):
        self.token, self.trips, self.payments, self.page = token, trips, payments, page
        self.calls: list[tuple[str, str, dict]] = []
        self.override: HttpResponse | None = None

    def request(self, method, url, *, params=None, form=None, headers=None, body=None, timeout=15.0):
        self.calls.append((method, url, dict(headers or {})))
        if self.override is not None:
            return self.override
        parts = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(parts.query))
        if (headers or {}).get("Authorization") != f"Bearer {self.token}":
            return HttpResponse(401, b'{"message":"unauthorized"}')
        source, key = (self.trips, "trips") if parts.path.endswith("/trips") else (self.payments, "payments")
        offset, limit = int(q.get("offset", 0)), min(int(q.get("limit", 50)), self.page)
        items = source[key][offset:offset + limit]
        return HttpResponse(200, json.dumps({"count": len(source[key]), key: items}).encode())


def creds(token="tok-1"):
    return engine.Credentials(access_token=token, refresh_token="ref-1", expires_at=0)


# --- schema --------------------------------------------------------------------------------------------


class TestSchema:
    def test_uber_template_round_trips(self):
        s = uber()
        assert s.host == "api.uber.com" and s.auth.token_exchange == "broker" and s.auth.redirect_port == 18766
        assert IntegrationSchema.from_dict(json.loads(json.dumps(s.to_dict()))) == s

    @pytest.mark.parametrize("change,msg", [
        ({"base_url": "http://api.uber.com"}, "https"),
        ({"base_url": "https://user:pw@api.uber.com"}, "https"),
        ({"id": "Uber!"}, "lowercase"),
        ({"requests": [{"name": "x", "path": "https://evil.example/steal", "mapping": {"occurred_at": "t",
                                                                                     "amounts": {"gross": "a"}}}]},
         "API host"),
        ({"requests": [{"name": "x", "path": "//evil.example/x", "mapping": {"occurred_at": "t",
                                                                           "amounts": {"gross": "a"}}}]}, "API host"),
        ({"requests": [{"name": "x", "path": "/v1/{nope}", "mapping": {"occurred_at": "t",
                                                                      "amounts": {"gross": "a"}}}]}, "isn't a form field"),
        ({"requests": [{"name": "x", "path": "/v1", "query": {"d": "{start_date|weird}"},
                        "mapping": {"occurred_at": "t", "amounts": {"gross": "a"}}}]}, "unknown filter"),
        ({"requests": [{"name": "x", "path": "/v1", "mapping": {"occurred_at": "t", "amounts": {"salary": "a"}}}]},
         "unknown role"),
        ({"requests": [{"name": "x", "path": "/v1", "role": "details"}]}, "key_path"),
    ])
    def test_rejected(self, change, msg):
        raw = copy.deepcopy(UBER_DRIVER)
        raw.update(change)
        with pytest.raises(IntegrationSchemaError, match=msg):
            IntegrationSchema.from_dict(raw)

    def test_secrets_never_go_into_urls(self):
        raw = {"id": "acme-api", "version": 1, "name": "Acme", "platform": "Acme", "base_url": "https://api.acme.test",
               "auth": {"type": "bearer_token", "token_field": "token"},
               "fields": [{"key": "token", "label": "Token", "type": "secret"}],
               "requests": [{"name": "earnings", "path": "/v1/e", "query": {"key": "{token}"},
                             "mapping": {"occurred_at": "t", "amounts": {"gross": "a"}}}]}
        with pytest.raises(IntegrationSchemaError, match="secret field"):
            IntegrationSchema.from_dict(raw)

    def test_newer_format_is_refused(self):
        with pytest.raises(IntegrationSchemaError, match="newer"):
            IntegrationSchema.from_dict({**UBER_DRIVER, "format": 2})


# --- form values → requests -------------------------------------------------------------------------------


class TestRequests:
    def test_defaults_and_check(self):
        s = uber()
        values = engine.defaults(s, TODAY)
        assert values == {"start_date": "2026-09-08", "end_date": "2026-10-08"}
        assert engine.check_values(s, values) == {}
        assert "required" in engine.check_values(s, {"end_date": "2026-10-08"})["start_date"]
        assert "YYYY-MM-DD" in engine.check_values(s, {"start_date": "10/1/2026", "end_date": "2026-10-08"})["start_date"]
        assert "before" in engine.check_values(s, {"start_date": "2026-10-09", "end_date": "2026-10-08"})["end_date"]

    def test_compiles_the_uber_request(self):
        url = engine.build_url(uber(), uber().requests[1], {"start_date": "2026-09-01", "end_date": "2026-09-30"},
                               tz=NY, offset=100)
        parts = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(parts.query))
        assert parts.scheme == "https" and parts.hostname == "api.uber.com" and parts.path == "/v1/partners/payments"
        assert q["from_time"] == "1788235200"           # 2026-09-01 00:00 New York (EDT) = 04:00 UTC
        assert q["to_time"] == "1790827199"             # 2026-09-30 23:59:59 New York
        assert q["offset"] == "100" and q["limit"] == "50"

    def test_values_are_url_encoded(self):
        raw = {"id": "acme-api", "version": 1, "name": "Acme", "platform": "Acme", "base_url": "https://api.acme.test",
               "fields": [{"key": "account", "label": "Account"}, {"key": "since", "label": "Since", "type": "date"}],
               "requests": [{"name": "earnings", "path": "/v1/accounts/{account}/earnings",
                             "query": {"since": "{since|iso}", "q": "{account}"},
                             "mapping": {"occurred_at": "t", "amounts": {"gross": "a"}}}]}
        s = IntegrationSchema.from_dict(raw)
        url = engine.build_url(s, s.requests[0], {"account": "a/b?x=1&host=evil", "since": ""}, tz=NY)
        parts = urllib.parse.urlsplit(url)
        assert parts.hostname == "api.acme.test"
        assert parts.path == "/v1/accounts/a%2Fb%3Fx%3D1%26host%3Devil/earnings"
        assert dict(urllib.parse.parse_qsl(parts.query)) == {"q": "a/b?x=1&host=evil"}     # empty "since" left out


# --- the pull ----------------------------------------------------------------------------------------------


class TestPull:
    def run(self, api=None, schema=None, values=None, **kw):
        api = api or FakeUber()
        result = engine.pull(schema or uber(), values or {"start_date": "2026-09-01", "end_date": "2026-09-30"},
                             kw.pop("credentials", creds()), transport=api, tz=NY, sleep=lambda s: None, **kw)
        return result, api

    def test_mock_uber_becomes_integer_cent_transactions(self):
        result, _ = self.run()
        got = [(r.kind, r.gross_amount_minor, r.platform_fee_minor, r.external_ref) for r in result.records]
        assert got == [("earning", 2188, 613, "uber:p-1#earning"), ("reimbursement", 225, 0, "uber:p-1#reimbursement"),
                       ("tip", 500, 0, "uber:p-2#tip"), ("earning", 4100, 990, "uber:p-3#earning"),
                       ("bonus", 1500, 0, "uber:p-4#bonus")]
        assert all(isinstance(r.gross_amount_minor, int) and isinstance(r.platform_fee_minor, int)
                   for r in result.records)
        assert result.skipped == 1 and not result.unmapped and not result.errors     # cash collected: skipped
        first = result.records[0]
        assert first.occurred_at == "2026-09-21T14:13:20.000Z"                       # the trip's pickup time
        assert first.description == "Uber trip t-1 · 4.21 mi · 960 s"
        assert result.records[-1].description is None                                # no trip: parts dropped
        assert result.total("earning") == 6288

    def test_pagination(self):
        result, api = self.run(FakeUber(page=2))
        payment_calls = [u for _, u, _ in api.calls if "/payments" in u]
        assert len(payment_calls) == 3 and "offset=2" in payment_calls[1] and "offset=4" in payment_calls[2]
        assert len(result.records) == 5

    def test_unknown_categories_are_counted_not_guessed(self):
        payments = {"payments": [{"payment_id": "p-9", "category": "quest_bonus_v2", "event_time": 1790001000,
                                  "amount": 10, "currency_code": "USD"}]}
        result, _ = self.run(FakeUber(payments=payments))
        assert result.records == [] and result.unmapped == {"quest_bonus_v2": 1}

    def test_money_never_goes_through_floats(self):
        payments = {"payments": [{"payment_id": "p-1", "category": "fare", "event_time": 1790001000,
                                  "amount": 0.1, "currency_code": "USD", "breakdown": {"service_fee": -0.2}}]}
        api = FakeUber(payments=payments)
        result, _ = self.run(api)
        assert result.records[0].gross_amount_minor == 30 and result.records[0].platform_fee_minor == 20
        assert engine._cents(Decimal("19.99"), "USD") == 1999 and engine._cents(0.25, "USD") == 25
        with pytest.raises(Exception, match="decimal places"):
            engine._cents(0.1 + 0.2, "USD")             # an imprecise float is refused, never silently rounded

    def test_import_dedupes_a_second_pull(self, db, platform_id):
        result, _ = self.run()
        first = import_records(db, platform_id, result.records, base_currency="USD")
        again = import_records(db, platform_id, self.run()[0].records, base_currency="USD")
        assert first.inserted == 5 and again.inserted == 0 and again.duplicates == 5
        with db.connection() as conn:
            total = conn.execute("SELECT sum(gross_amount_minor) FROM transactions WHERE deleted_at IS NULL").fetchone()[0]
        assert total == 2188 + 225 + 500 + 4100 + 1500


class TestSafety:
    def test_token_only_goes_to_the_api_host_as_a_header(self):
        _, api = TestPull().run()
        for method, url, headers in api.calls:
            assert method == "GET" and urllib.parse.urlsplit(url).hostname == "api.uber.com"
            assert "tok-1" not in url and headers["Authorization"] == "Bearer tok-1"

    def test_redirects_are_refused(self):
        api = FakeUber()
        api.override = HttpResponse(302, b"", {"Location": "https://evil.example/collect"})
        with pytest.raises(engine.IntegrationError, match="redirect") as exc:
            TestPull().run(api)
        assert "tok-1" not in exc.value.message and "evil" not in exc.value.message

    def test_urllib_transport_can_refuse_redirects(self):
        from core.fleet.http import UrllibTransport, _NoRedirect

        t = UrllibTransport(follow_redirects=False)
        assert t._open.__self__.handlers and any(isinstance(h, _NoRedirect) for h in t._open.__self__.handlers)

    def test_oversized_and_non_json_pages(self):
        api = FakeUber()
        api.override = HttpResponse(200, b"x" * (engine.MAX_PAGE_BYTES + 1))
        with pytest.raises(engine.IntegrationError, match="more data"):
            TestPull().run(api)
        api.override = HttpResponse(200, b"<html>")
        with pytest.raises(engine.IntegrationError, match="wasn't JSON"):
            TestPull().run(api)

    def test_expired_token_refreshes_once(self):
        api = FakeUber(token="tok-2")
        refreshed = []

        def refresh(c):
            refreshed.append(c.refresh_token)
            return engine.Credentials("tok-2", "ref-2", 0)

        c = creds()
        result, _ = TestPull().run(api, credentials=c, refresh=refresh)
        assert refreshed == ["ref-1"] and len(result.records) == 5 and result.credentials_changed
        assert c.access_token == "tok-2" and c.refresh_token == "ref-2"            # caller keeps the new ones
        with pytest.raises(engine.AuthExpired):
            TestPull().run(FakeUber(token="other"), credentials=creds(), refresh=lambda c: engine.Credentials("nope"))

    def test_credentials_never_print(self):
        assert "tok-1" not in repr(creds()) and "ref-1" not in repr(creds())


# --- OAuth + the iFlow token broker -------------------------------------------------------------------------


class TestOAuth:
    def test_authorize_url_uses_pkce(self):
        verifier, challenge = oauth.pkce_pair()
        url = oauth.authorize_url(uber(), redirect="http://127.0.0.1:18766/callback", state="st", challenge=challenge)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        assert url.startswith("https://auth.uber.com/oauth/v2/authorize?")
        assert q["code_challenge_method"] == "S256" and q["client_id"] == "uber-test-client"
        assert q["scope"] == "partner.accounts partner.trips partner.payments" and "client_secret" not in url

    def test_browser_flow_through_the_broker(self, broker_api):
        """Real loopback listener; the 'browser' follows the redirect; the broker adds the secret."""
        import threading
        import urllib.request

        api, posted = broker_api

        def browser(url):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            target = f"{q['redirect_uri']}?code=the-code&state={q['state']}"
            threading.Timer(0.2, lambda: urllib.request.urlopen(target, timeout=5).read()).start()

        schema = uber(auth={**UBER_DRIVER["auth"], "client_id": "uber-test-client", "redirect_port": 0})
        c = oauth.connect(schema, transport=api, broker_url="https://api.iflowcore.test", open_url=browser,
                          timeout_s=10)
        assert c.access_token == "uber-access" and c.refresh_token == "uber-refresh" and c.expires_at > 0
        form = posted[-1]
        assert form["client_secret"] == "s3cret" and form["code"] == "the-code" and form["code_verifier"]
        assert form["redirect_uri"].startswith("http://127.0.0.1:")

    def test_refresh_through_the_broker(self, broker_api):
        api, posted = broker_api
        c = oauth.refresh(uber(), creds(), transport=api, broker_url="https://api.iflowcore.test")
        assert c.access_token == "uber-access" and posted[-1]["grant_type"] == "refresh_token"

    def test_broker_rules(self, broker_api):
        api, _ = broker_api
        c = api.h.c
        assert c.post("/v1/integrations/unknown/token", json={"grant_type": "refresh_token",
                                                              "refresh_token": "x"}).status_code == 404
        bad = c.post("/v1/integrations/uber-driver-api/token", json={
            "grant_type": "authorization_code", "code": "c", "code_verifier": "v",
            "redirect_uri": "https://evil.example/callback"})
        assert bad.status_code == 422                                    # loopback redirects only
        ok = c.post("/v1/integrations/uber-driver-api/token", json={"grant_type": "refresh_token",
                                                                    "refresh_token": "r"})
        assert ok.status_code == 200 and set(ok.json()) <= {"access_token", "refresh_token", "expires_in",
                                                             "token_type", "scope"}
        assert ok.headers["cache-control"] == "no-store" and "s3cret" not in ok.text

    def test_broker_secret_is_not_printable(self):
        from iflow_api.broker import parse_clients

        clients = parse_clients('{"uber-driver-api": {"token_url": "https://auth.uber.com/oauth/v2/token", '
                                '"client_id": "id", "client_secret": "s3cret"}}')
        assert "s3cret" not in repr(clients)
        with pytest.raises(ValueError, match="https"):
            parse_clients('{"x": {"token_url": "http://a", "client_id": "i", "client_secret": "s"}}')


@pytest.fixture
def broker_api():
    """The real account API (in-process) with a broker client and a fake Uber token endpoint."""
    from dataclasses import replace

    from iflow_api.app import create_app
    from iflow_api.broker import parse_clients
    from iflow_api.settings import Settings

    posted: list[dict] = []

    def uber_token_endpoint(url, form):
        assert url == "https://auth.uber.com/oauth/v2/token"
        posted.append(dict(form))
        return 200, json.dumps({"access_token": "uber-access", "refresh_token": "uber-refresh", "expires_in": 2592000,
                                "token_type": "Bearer", "scope": "partner.trips", "uid": "kept-out"}).encode()

    settings = replace(Settings.from_env({"IFLOW_API_ENV": "test", "IFLOW_API_DATABASE_URL": "sqlite://"}),
                       integration_clients=parse_clients(json.dumps({"uber-driver-api": {
                           "token_url": "https://auth.uber.com/oauth/v2/token", "client_id": "uber-test-client",
                           "client_secret": "s3cret"}})))
    client = TestClient(create_app(settings, broker_post=uber_token_endpoint))

    class Transport:
        h = type("H", (), {"c": client})

        def request(self, method, url, *, params=None, form=None, headers=None, body=None, timeout=15.0):
            r = client.request(method, urllib.parse.urlsplit(url).path, content=body, headers=dict(headers or {}))
            return HttpResponse(r.status_code, r.content, dict(r.headers))

    yield Transport(), posted


# --- distribution: bundle + install --------------------------------------------------------------------------


class TestBundle:
    def test_install_and_downgrade(self, db):
        raw = uber().to_dict()
        report = dynamic.install_bundle(db, dynamic.sign_bundle([], KEY, integrations=[raw]), [PUB])
        assert report.integrations_added == ("uber-driver-api",)
        assert [s.id for s in dynamic.installed_integrations(db)] == ["uber-driver-api"]
        v2 = dynamic.sign_bundle([], KEY, integrations=[{**raw, "version": 2}])
        assert dynamic.install_bundle(db, v2, [PUB]).integrations_updated == ("uber-driver-api",)
        old = dynamic.install_bundle(db, dynamic.sign_bundle([], KEY, integrations=[raw]), [PUB])
        assert old.refused_downgrades == ("uber-driver-api",)
        assert dynamic.installed_integrations(db)[0].version == 2

    def test_a_bad_integration_never_blocks_formats(self, db):
        from .test_formats import SCHEMA as FORMAT

        bundle = dynamic.sign_bundle([FORMAT], KEY, integrations=[{**uber().to_dict(), "format": 9},
                                                                  {"id": "broken"}])
        report = dynamic.install_bundle(db, bundle, [PUB])
        assert report.added == ("acme-weekly",) and len(report.integrations_skipped) == 2

    def test_tampered_bundle_rejected(self):
        bundle = dynamic.sign_bundle([], KEY, integrations=[uber().to_dict()])
        head, payload, sig = bundle.split(".")
        with pytest.raises(dynamic.SchemaError):
            dynamic.verify_bundle(f"{head}.{payload[:-4]}AAAA.{sig}", [PUB])

    def test_old_bundles_still_verify(self):
        bundle = dynamic.sign_bundle([], KEY)
        assert dynamic.verify_bundle(bundle, [PUB]).integrations == ()

    def test_never_synced(self):
        from core.sync.engine import LOCAL_ONLY_TABLES

        assert "vendor_integrations" in LOCAL_ONLY_TABLES


# --- the admin service ----------------------------------------------------------------------------------------


class TestAdminRoutes:
    def test_save_publish_and_distribute(self, tmp_path, monkeypatch):
        from .test_admin import SEED, signing_public_hex
        from .test_admin_console import H
        from .test_ai_service import make

        monkeypatch.setenv("RS_AI_FORMATS_DIR", str(tmp_path / "cdn"))
        with TestClient(make(tmp_path, RS_AI_LICENSE_SIGNING_KEY=SEED)) as c:
            raw = uber().to_dict()
            assert c.put("/admin/v1/integrations/uber-driver-api", headers=H, json={"schema": raw}).status_code == 200
            assert c.put("/admin/v1/integrations/uber-driver-api", headers=H,
                         json={"schema": {**raw, "base_url": "http://api.uber.com"}}).status_code == 422
            out = c.post("/admin/v1/integrations/uber-driver-api/publish", headers=H).json()
            assert out["distribution"]["integrations"] == 1
            listed = c.get("/admin/v1/integrations", headers=H).json()["integrations"]
            assert listed[0]["published"]["version"] == 1
            assert "integration.publish" in json.dumps(c.get("/admin/v1/audit?limit=20", headers=H).json())
        bundle = dynamic.verify_bundle((tmp_path / "cdn/v1/bundle.ifpb").read_text(), [signing_public_hex()])
        assert [i.id for i in bundle.integrations] == ["uber-driver-api"]

    def test_permissions(self, tmp_path):
        from .test_admin_console import ready_user
        from .test_ai_service import make

        with TestClient(make(tmp_path, RS_AI_ADMIN_FAILURES_PER_10MIN="100")) as c:
            sup = ready_user(c, "sup", "support")
            assert c.get("/admin/v1/integrations", headers=sup).status_code == 403


# --- UIs ---------------------------------------------------------------------------------------------------


class TestStudioUI:
    def test_template_validate_preview_test_save_publish(self, tmp_path, monkeypatch):
        from tests.ui_helpers import find_key, texts

        from .test_admin import SEED
        from .test_admin_console import GOOD_PASSWORD, console, login, ready_user
        from .test_ai_service import make

        monkeypatch.setenv("RS_AI_FORMATS_DIR", str(tmp_path / "cdn"))
        with TestClient(make(tmp_path, RS_AI_LICENSE_SIGNING_KEY=SEED)) as api:
            ready_user(api)
            app, page = console(api)
            login(app, page, "casey", GOOD_PASSWORD)
            app.go("parsers")
            tabs = find_key(page.views[-1], "studio-tabs")
            tabs.selected = ["integrations"]
            tabs.on_change(type("E", (), {"control": tabs})())
            find_key(page.views[-1], "api-from-template").on_click(None)
            view = page.views[-1]
            assert find_key(view, "api-valid") is not None
            assert find_key(view, "api-preview-start_date").value                      # the user's form, rendered
            find_key(view, "api-preview").on_click(None)
            view = page.views[-1]
            assert "api.uber.com/v1/partners/payments" in find_key(view, "api-url-payments").value
            find_key(view, "api-sample-trips").value = json.dumps(TRIPS)
            find_key(view, "api-sample-payments").value = json.dumps(PAYMENTS)
            find_key(view, "api-test").on_click(None)
            view = page.views[-1]
            cells = texts(find_key(view, "api-test-records"))
            assert "$21.88" in cells and "$6.13" in cells and "$5.00" in cells and "$15.00" in cells
            # a broken edit is reported with its location, and nothing is saved
            find_key(view, "api-schema").value = find_key(view, "api-schema").value.replace('"https://api.uber.com"',
                                                                                             '"http://api.uber.com"')
            find_key(view, "api-save").on_click(None)
            assert "https" in " ".join(texts(find_key(page.views[-1], "api-error")))
            find_key(page.views[-1], "api-from-template").on_click(None)
            schema_text = find_key(page.views[-1], "api-schema").value.replace("REPLACE-WITH-UBER-CLIENT-ID", "uber-id")
            find_key(page.views[-1], "api-schema").value = schema_text
            find_key(page.views[-1], "api-save").on_click(None)
            find_key(page.views[-1], "api-publish").on_click(None)
            find_key(page.dialogs[-1], "api-publish-confirm").on_click(None)
            assert "sent it to the CDN" in " ".join(texts(find_key(page.views[-1], "api-notice")))
            assert '"version": 2' in find_key(page.views[-1], "api-schema").value


class TestConnectionWizard:
    @pytest.fixture
    def app(self, tmp_path):
        from .test_ui_app import make_app

        api = FakeUber(token="pasted-token")
        app, page = make_app(tmp_path, services={"integration_transport": api})
        app.start()
        raw = {"id": "acme-api", "version": 1, "name": "Acme earnings", "platform": "Uber",
               "description": "Pull from Acme.", "base_url": "https://api.uber.com",
               "auth": {"type": "bearer_token", "token_field": "token"},
               "fields": [{"key": "token", "label": "API token", "type": "secret", "required": True},
                          *UBER_DRIVER["fields"]],
               "requests": UBER_DRIVER["requests"]}
        dynamic.install_bundle(app.state.db, dynamic.sign_bundle([], KEY, integrations=[raw]), [PUB])
        with app.state.db.transaction() as conn:
            conn.execute("INSERT INTO platforms (id, name, kind) VALUES (?, 'Uber', 'rideshare')", (str(uuid.uuid4()),))
        app.navigate("/finances")
        return app, page, api

    def test_form_from_schema_then_pull(self, app):
        from tests.ui_helpers import find_key, texts

        app, page, api = app
        assert find_key(page.views[-1], "integrations-card") is not None
        find_key(page.views[-1], "conn-acme-api-toggle").on_click(None)
        view = page.views[-1]
        assert find_key(view, "conn-acme-api-start_date").value                       # defaults filled in
        assert find_key(view, "conn-acme-api-platform").value                         # matched "Uber" by name
        find_key(view, "conn-acme-api-pull").on_click(None)                           # token missing
        assert "API token is required" in " ".join(texts(find_key(page.views[-1], "conn-acme-api")))
        find_key(page.views[-1], "conn-acme-api-token").value = "pasted-token"
        find_key(page.views[-1], "conn-acme-api-pull").on_click(None)
        shown = " ".join(texts(find_key(page.views[-1], "conn-acme-api")))
        assert "5 new, 0 already here" in shown
        saved = app.state.settings.integration_credentials["acme-api"]
        assert saved["secrets"]["token"] == "pasted-token"                             # sealed device setting
        find_key(page.views[-1], "conn-acme-api-pull").on_click(None)                  # token stays saved
        assert "0 new, 5 already here" in " ".join(texts(find_key(page.views[-1], "conn-acme-api")))
        find_key(page.views[-1], "conn-acme-api-disconnect").on_click(None)
        assert "acme-api" not in app.state.settings.integration_credentials

    def test_not_shown_without_integrations(self, tmp_path):
        from tests.ui_helpers import find_key

        from .test_ui_app import make_app

        app, page = make_app(tmp_path)
        app.start()
        app.navigate("/finances")
        assert find_key(page.views[-1], "integrations-card") is None

    def test_credentials_are_a_sealed_setting(self):
        from core.device_settings import SENSITIVE_FIELDS

        assert "integration_credentials" in SENSITIVE_FIELDS
