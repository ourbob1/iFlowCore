"""Phase 11: admin portal. Server auth barriers, license issuing (format-compatible with the app's
offline verifier), telemetry triage, health, and the app's admin client (the console UI: tests/test_admin_console.py)."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from core.admin.client import AdminClient, AdminError
from core.fleet.http import HttpResponse
from core.licensing import tokens as core_tokens
from core.licensing.pricing import PRICE_BOOK as CORE_PRICE_BOOK

from .test_ai_service import ADMIN, AUTH, TOKEN, make, real_events

from iflow_ai import licensing as svc_licensing  # noqa: E402  (path set up by test_ai_service)
from iflow_ai.admin import AdminGate  # noqa: E402
from iflow_ai.settings import Settings, SettingsError  # noqa: E402

SEED = hashlib.sha256(b"phase-11 test signing key").hexdigest()
ADMIN2 = "b" * 40
H = {"Authorization": f"Bearer {ADMIN}"}


def signing_public_hex() -> str:
    return svc_licensing.public_key_hex(svc_licensing.load_signing_key(SEED))


@pytest.fixture
def admin_api(tmp_path):
    with TestClient(make(tmp_path, RS_AI_LICENSE_SIGNING_KEY=SEED, RS_AI_ADMIN_TOKENS=ADMIN2)) as c:
        yield c


# --- settings ------------------------------------------------------------------------------------------------


class TestSettings:
    base = {"RS_AI_TOKENS": TOKEN, "RS_AI_DATA_DIR": "/tmp/x"}

    @pytest.mark.parametrize("admins", ["short", TOKEN, f"{'z' * 40},short"])
    def test_admin_tokens_must_be_strong_and_distinct(self, admins):
        with pytest.raises(SettingsError, match="RS_AI_ADMIN_TOKENS"):
            Settings.from_env({**self.base, "RS_AI_ADMIN_TOKENS": admins})

    def test_legacy_and_list_merge(self):
        s = Settings.from_env({**self.base, "RS_AI_ADMIN_TOKEN": ADMIN, "RS_AI_ADMIN_TOKENS": f"{ADMIN2},{ADMIN}"})
        assert s.admin_tokens == (ADMIN2, ADMIN)

    @pytest.mark.parametrize("key,msg", [("zz", "64 hex"), ("ab" * 16, "64 hex"),
                                         (core_tokens._SIMULATION_SEED.hex(), "public simulation key")])
    def test_signing_key_validation(self, key, msg):
        with pytest.raises(SettingsError, match=msg):
            Settings.from_env({**self.base, "RS_AI_LICENSE_SIGNING_KEY": key})


# --- auth barriers -----------------------------------------------------------------------------------------------


class TestAuth:
    def test_barriers(self, admin_api):
        c = admin_api
        assert c.get("/admin/v1/whoami").status_code == 401                                   # no token
        assert c.get("/admin/v1/whoami", headers=AUTH).status_code == 403                    # a client token
        assert c.get("/admin/v1/whoami", headers={"Authorization": "Bearer " + "x" * 40}).status_code == 403
        assert c.get("/admin/v1/whoami", headers={"Authorization": f"Basic {ADMIN}"}).status_code == 401
        me = c.get("/admin/v1/whoami", headers=H).json()
        assert me["actor"] == "token-1" and me["license_signing"] is True
        assert c.get("/admin/v1/whoami", headers={"Authorization": f"Bearer {ADMIN2}"}).json()["actor"] == "token-0"

    def test_every_admin_route_is_guarded(self, tmp_path):
        # a roomier failure allowance: probing 13 routes with a client token is 13 failed admin attempts
        with TestClient(make(tmp_path, RS_AI_LICENSE_SIGNING_KEY=SEED, RS_AI_ADMIN_FAILURES_PER_10MIN="100")) as c:
            flat = []
            for r in c.app.routes:                     # newer FastAPI nests included routers
                flat.extend(getattr(getattr(r, "original_router", None), "routes", [r]))
            routes = [r for r in flat if (getattr(r, "path", "") or "").startswith("/admin/v1")
                      and r.path != "/admin/v1/auth/login"]                 # the one public route
            assert len(routes) >= 12
            for r in routes:
                method = sorted(r.methods)[0]
                path = r.path.replace("{fingerprint}", "a" * 16).replace("{action}", "resolve")
                for headers in (AUTH, {}):             # a client token, then no token at all
                    resp = c.request(method, path, headers=headers, json={})
                    assert resp.status_code in (401, 403), (method, r.path, resp.status_code)

    def test_disabled_without_admin_tokens(self, tmp_path):
        with TestClient(make(tmp_path, RS_AI_ADMIN_TOKEN="")) as c:
            assert c.get("/admin/v1/whoami", headers=H).status_code == 404
            assert c.get("/v1/telemetry/issues", headers=H).status_code == 404

    def test_lockout_after_failures(self, admin_api):
        bad = {"Authorization": "Bearer " + "y" * 40}
        for _ in range(10):
            assert admin_api.get("/admin/v1/whoami", headers=bad).status_code == 403
        locked = admin_api.get("/admin/v1/whoami", headers=H)                              # even the right token
        assert locked.status_code == 429 and int(locked.headers["Retry-After"]) > 0

    def test_lockout_window_expires(self):
        t = [0.0]
        gate = AdminGate((ADMIN,), failures_allowed=3, window_s=600, clock=lambda: t[0])
        for _ in range(3):
            with pytest.raises(HTTPException):
                gate.check("wrong" * 10, "1.2.3.4")
        with pytest.raises(HTTPException) as exc:
            gate.check(ADMIN, "1.2.3.4")
        assert exc.value.status_code == 429
        assert gate.check(ADMIN, "5.6.7.8") == "token-0"                                  # other IPs unaffected
        t[0] = 601
        assert gate.check(ADMIN, "1.2.3.4") == "token-0"

    def test_comparison_checks_every_token(self, monkeypatch):
        """No early exit: the number of constant-time comparisons doesn't depend on which token matched."""
        tokens = tuple(chr(97 + i) * 40 for i in range(5))
        gate = AdminGate(tokens)
        calls = []
        real = hmac.compare_digest
        monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
        for probe in (tokens[0], tokens[4], "nope" * 10):
            calls.clear()
            gate.match(probe)
            assert len(calls) == 5

    def test_tokens_never_logged(self, admin_api, caplog):
        caplog.set_level(logging.DEBUG)
        admin_api.get("/admin/v1/whoami", headers={"Authorization": "Bearer " + "q" * 40})
        admin_api.get("/admin/v1/overview", headers=H)
        admin_api.post("/admin/v1/licenses", headers=H, json={"tier": "pro", "period": "monthly"})
        text = caplog.text
        assert ADMIN not in text and "q" * 40 not in text and SEED not in text


# --- licenses ---------------------------------------------------------------------------------------------------


class TestLicenses:
    def test_price_book_matches_the_app(self):
        assert {(t.value, p.value, c): v for (t, p, c), v in CORE_PRICE_BOOK.items()} == svc_licensing.PRICE_BOOK
        assert all(isinstance(v, int) for v in svc_licensing.PRICE_BOOK.values())

    def test_issued_token_verifies_in_the_app(self, admin_api):
        r = admin_api.post("/admin/v1/licenses", headers=H, json={"tier": "pro", "period": "annual",
                                                                  "customer_ref": "order-1042"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["amount_minor"] == 6000 and isinstance(data["amount_minor"], int)
        assert data["public_key"] == signing_public_hex()
        claims = core_tokens.verify(data["token"], allow_simulation=False, keys=[signing_public_hex()])
        assert claims.tier.value == "pro" and claims.period.value == "annual" and claims.amount_minor == 6000
        assert claims.issuer == "vendor" and claims.license_id == data["license_id"]
        assert (claims.expires_at.year - claims.issued_at.year) * 12 + claims.expires_at.month - claims.issued_at.month == 12
        # the app with this vendor key configured accepts it through its normal path
        env = {core_tokens.ENV_PUBKEYS: signing_public_hex()}
        assert core_tokens.verify(data["token"], allow_simulation=False, env=env).tier.value == "pro"
        # …and a different vendor key doesn't
        with pytest.raises(core_tokens.LicenseError):
            core_tokens.verify(data["token"], allow_simulation=False, keys=[os.urandom(32).hex()])

    def test_terms_and_complimentary_are_integer_cents(self, admin_api):
        three = admin_api.post("/admin/v1/licenses", headers=H,
                               json={"tier": "guild", "period": "monthly", "terms": 3}).json()
        assert three["amount_minor"] == 3 * 1499
        comp = admin_api.post("/admin/v1/licenses", headers=H,
                              json={"tier": "pro", "period": "monthly", "complimentary": True}).json()
        assert comp["amount_minor"] == 0 and comp["complimentary"] is True
        rev = admin_api.get("/admin/v1/revenue", headers=H).json()
        assert rev["total_minor"] == 4497 and rev["licenses"] == 2
        month = rev["months"][0]
        assert month["tiers"]["guild"] == {"licenses": 1, "amount_minor": 4497}
        assert month["tiers"]["pro"] == {"licenses": 1, "amount_minor": 0}
        overview = admin_api.get("/admin/v1/overview", headers=H).json()
        assert overview["revenue_this_month_minor"] == 4497 and overview["licenses_this_month"] == 2

    @pytest.mark.parametrize("body", [
        {"tier": "guild", "period": "annual"},             # not offered
        {"tier": "solo", "period": "monthly"},             # free tier needs no license
        {"tier": "pro", "period": "weekly"},
        {"tier": "pro", "period": "monthly", "terms": 0},
        {"tier": "pro", "period": "monthly", "terms": 99},
        {"tier": "pro", "period": "monthly", "customer_ref": "ann@example.com"},
        {"tier": "pro", "period": "monthly", "customer_ref": "Ann Smith"},
        {"tier": "pro", "period": "monthly", "amount_minor": 1},    # price can't be set by the caller
    ])
    def test_rejected(self, admin_api, body):
        assert admin_api.post("/admin/v1/licenses", headers=H, json=body).status_code == 422
        assert admin_api.get("/admin/v1/licenses", headers=H).json()["licenses"] == []

    def test_ledger_and_audit(self, admin_api):
        issued = admin_api.post("/admin/v1/licenses", headers=H, json={"tier": "pro", "period": "monthly",
                                                                       "customer_ref": "stripe:cs_123"}).json()
        rows = admin_api.get("/admin/v1/licenses", headers=H).json()["licenses"]
        assert rows[0]["license_id"] == issued["license_id"] and rows[0]["customer_ref"] == "stripe:cs_123"
        assert "token" not in rows[0]                                  # keys aren't stored, only their record
        audit = admin_api.get("/admin/v1/audit", headers=H).json()["entries"]
        assert audit[0]["action"] == "license.issue" and audit[0]["target"] == issued["license_id"]
        assert audit[0]["actor"] == "token-1"

    def test_verify_tool(self, admin_api):
        token = admin_api.post("/admin/v1/licenses", headers=H, json={"tier": "pro", "period": "monthly"}).json()["token"]
        ok = admin_api.post("/admin/v1/licenses/verify", headers=H, json={"token": token}).json()
        assert ok["signature_valid"] and ok["in_ledger"] and ok["matches_ledger"] and not ok["expired"]
        head, body, sig = token.split(".")
        forged = f"{head}.{body}.{sig[:-3]}AAA"
        assert admin_api.post("/admin/v1/licenses/verify", headers=H, json={"token": forged}).json()[
            "signature_valid"] is False
        foreign = core_tokens.sign(core_tokens.verify(token, allow_simulation=False, keys=[signing_public_hex()]),
                                   core_tokens.simulation_private_key())
        assert admin_api.post("/admin/v1/licenses/verify", headers=H, json={"token": foreign}).json()[
            "signature_valid"] is False
        junk = admin_api.post("/admin/v1/licenses/verify", headers=H, json={"token": "hello"}).json()
        assert junk["signature_valid"] is False and junk["error"]

    def test_issuing_needs_a_signing_key(self, tmp_path):
        with TestClient(make(tmp_path)) as c:
            assert c.get("/admin/v1/whoami", headers=H).json()["license_signing"] is False
            assert c.post("/admin/v1/licenses", headers=H, json={"tier": "pro", "period": "monthly"}).status_code == 503

    def test_month_arithmetic(self):
        jan31 = datetime(2026, 1, 31, 12, tzinfo=timezone.utc)
        assert svc_licensing.add_months(jan31, 1).day == 28
        assert svc_licensing.add_months(datetime(2028, 1, 31, tzinfo=timezone.utc), 1).day == 29
        assert svc_licensing.add_months(jan31, 12) == jan31.replace(year=2027)


# --- telemetry triage --------------------------------------------------------------------------------------------


class TestTelemetry:
    def _send(self, c, tmp_path, n=3):
        rep = real_events(tmp_path, n)
        rep.flush(lambda events: c.post("/v1/telemetry/events", json={"events": events}, headers=AUTH)
                  .json()["accepted"])
        return rep

    def test_queue_hides_install_ids(self, admin_api, tmp_path):
        self._send(admin_api, tmp_path)
        data = admin_api.get("/admin/v1/telemetry/issues", headers=H).json()
        assert data["counts"]["open_issues"] == 2 and data["counts"]["events_total"] == 2
        issue = data["issues"][0]
        assert isinstance(issue["installs"], int)                      # a count, never the ids
        events = admin_api.get(f"/admin/v1/telemetry/issues/{issue['fingerprint']}", headers=H).json()["events"]
        assert events and all("install_id" not in e for e in events)
        legacy = admin_api.get(f"/v1/telemetry/issues/{issue['fingerprint']}", headers=H).json()["events"]
        assert all("install_id" not in e for e in legacy)

    def test_resolve_reopen_and_regression(self, admin_api, tmp_path):
        self._send(admin_api, tmp_path)
        fp = admin_api.get("/admin/v1/telemetry/issues", headers=H).json()["issues"][0]["fingerprint"]
        assert admin_api.post(f"/admin/v1/telemetry/issues/{fp}/resolve", headers=H).json()["status"] == "resolved"
        open_now = admin_api.get("/admin/v1/telemetry/issues?status=open", headers=H).json()["issues"]
        assert fp not in {i["fingerprint"] for i in open_now}
        resolved = admin_api.get("/admin/v1/telemetry/issues?status=resolved", headers=H).json()["issues"]
        assert [i["fingerprint"] for i in resolved] == [fp]
        # the same bug strikes again on a device: same fingerprint, a new report id → reopened automatically
        import uuid

        store = admin_api.app.state.services.store
        body = store.events_for(fp, 1)[0]
        recurrence = {**body, "id": str(uuid.uuid4())}
        assert admin_api.post("/v1/telemetry/events", json={"events": [recurrence]}, headers=AUTH).json()["new"] == 1
        again = {i["fingerprint"]: i["status"] for i in admin_api.get("/admin/v1/telemetry/issues", headers=H)
                 .json()["issues"]}
        assert again.get(fp) == "open"
        assert admin_api.post("/admin/v1/telemetry/issues/" + "0" * 16 + "/resolve", headers=H).status_code == 404
        assert admin_api.post(f"/admin/v1/telemetry/issues/{fp}/delete", headers=H).status_code == 404
        actions = [e["action"] for e in admin_api.get("/admin/v1/audit", headers=H).json()["entries"]]
        assert "telemetry.resolve" in actions

    def test_purge(self, admin_api, tmp_path):
        self._send(admin_api, tmp_path)
        assert admin_api.post("/admin/v1/telemetry/purge", headers=H, json={"older_than_days": 30}).json() == {
            "deleted_events": 0}
        store = admin_api.app.state.services.store
        with store._conn:
            store._conn.execute("UPDATE events SET received_at = '2020-01-01T00:00:00Z'")
        assert admin_api.post("/admin/v1/telemetry/purge", headers=H, json={"older_than_days": 30}).json() == {
            "deleted_events": 2}
        assert admin_api.get("/admin/v1/telemetry/issues", headers=H).json()["issues"] == []
        assert admin_api.post("/admin/v1/telemetry/purge", headers=H, json={"older_than_days": 0}).status_code == 422


# --- health -------------------------------------------------------------------------------------------------------


def test_health_monitor(admin_api):
    admin_api.get("/healthz")
    admin_api.get("/v1/status", headers=AUTH)
    h = admin_api.get("/admin/v1/health", headers=H).json()
    assert h["uptime_s"] >= 0 and isinstance(h["container"], bool)
    assert set(h["models"]) == {"llm", "ocr", "vlm"} and "available" in h["ocr"]
    assert h["queue"]["max_concurrency"] == 1 and h["queue"]["waiting"] == 0
    assert h["requests"]["total"] >= 2 and "2xx" in h["requests"]["by_class"]
    assert all("/admin/v1/licenses/" not in a for a in h["requests"]["by_area"])   # no ids in stats
    assert h["disk"]["databases_bytes"] > 0
    blob = str(h)
    assert ADMIN not in blob and TOKEN not in blob and SEED not in blob


# --- the app's admin client + view --------------------------------------------------------------------------------


class ServiceTransport:
    """core.fleet.http.Transport → the AI service in-process."""

    def __init__(self, client: TestClient, base: str = "https://ai.iflowcore.test") -> None:
        self.c, self.base = client, base

    def request(self, method, url, *, params=None, form=None, headers=None, body=None, timeout=15.0):
        r = self.c.request(method, url[len(self.base):], content=body, headers=dict(headers or {}))
        return HttpResponse(r.status_code, r.content, dict(r.headers))


class TestAdminClient:
    def test_client_rules(self, admin_api):
        with pytest.raises(AdminError, match="HTTPS"):
            AdminClient("http://ai.example.com", ADMIN)
        with pytest.raises(AdminError, match="32"):
            AdminClient("https://ai.example.com", "short")
        client = AdminClient("https://ai.iflowcore.test", ADMIN, transport=ServiceTransport(admin_api))
        assert ADMIN not in repr(client)
        assert client.whoami()["actor"] == "token-1"
        issued = client.issue_license("pro", "monthly", customer_ref="order-9")
        assert client.verify_license(issued["token"])["signature_valid"]
        assert client.revenue()["total_minor"] == 699
        wrong = AdminClient("https://ai.iflowcore.test", "w" * 40, transport=ServiceTransport(admin_api))
        with pytest.raises(AdminError) as exc:
            wrong.whoami()
        assert exc.value.status == 403

    def test_viewmodel_formatting(self):
        from ui.viewmodels import admin as vm

        assert vm.cents(6000) == "$60.00" and vm.cents(12.5) == "—" and vm.cents(True) == "—"
        assert vm.cents("6000") == "—"                                 # money is integer cents or nothing
        assert vm.duration(90061) == "1d 1h" and vm.size(3 * 1024 * 1024) == "3.0 MB"
