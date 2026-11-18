"""Phase 10.2: the app's account client against the real API (in-process) and Google desktop PKCE."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import urllib.parse
import urllib.request

import pytest

from core.account import AccountClient, AccountError, AccountSession, MemorySessionStore, SettingsSessionStore
from core.account import google_desktop
from core.account.client import validate_api_url
from core.fleet.http import HttpResponse, ProviderError

from .test_account_api import DEVICE, PASSWORD, Harness, google_token  # noqa: F401  (fixtures reuse)

API = "https://api.iflowcore.test"


class AppTransport:
    """core.fleet.http.Transport → FastAPI TestClient (no network)."""

    def __init__(self, harness: Harness) -> None:
        self.h = harness
        self.calls: list[tuple[str, str]] = []
        self.offline = False

    def request(self, method, url, *, params=None, form=None, headers=None, body=None, timeout=15.0):
        if self.offline:
            raise ProviderError("network down")
        path = url[len(API):]
        self.calls.append((method, path))
        r = self.h.c.request(method, path, content=body, headers=dict(headers or {}))
        return HttpResponse(r.status_code, r.content, dict(r.headers))


@pytest.fixture
def env():
    h = Harness("sqlite://")
    transport = AppTransport(h)
    store = MemorySessionStore()
    client = AccountClient(API, store=store, transport=transport, platform="linux", clock=lambda: h.clock.mono)
    yield h, client, store, transport
    h.app.state.engine.dispose()


def signed_up(h, client):
    client.signup("ann@example.com", PASSWORD)
    return client.verify_email("ann@example.com", h.last_code())


class TestClientFlows:
    def test_signup_verify_persists_session_without_access_token(self, env):
        h, client, store, _ = env
        account = signed_up(h, client)
        assert account["email"] == "ann@example.com" and client.signed_in
        saved = store.load()
        assert saved.email == "ann@example.com" and saved.api_url == API
        assert "access_token" not in json.dumps(saved.to_dict())             # access token: memory only
        assert client.account()["email_verified"] is True

    def test_default_device_name_is_generic(self, env):
        h, client, _, _ = env
        signed_up(h, client)
        assert client.devices()[0]["name"] == "Linux desktop"                  # not the hostname

    def test_access_token_refreshes_itself(self, env):
        h, client, store, transport = env
        signed_up(h, client)
        first_refresh = store.load().refresh_token
        h.clock.advance(20 * 60)                                             # access token expired
        client.account()
        assert ("POST", "/v1/auth/refresh") in transport.calls
        assert store.load().refresh_token != first_refresh                   # rotated and persisted

    def test_restart_uses_stored_session(self, env):
        h, client, store, transport = env
        signed_up(h, client)
        fresh = AccountClient(API, store=store, transport=transport, clock=lambda: h.clock.mono)   # app restart
        assert fresh.account()["email"] == "ann@example.com"

    def test_revoked_elsewhere_signs_out_locally(self, env):
        h, client, store, transport = env
        signed_up(h, client)
        other = AccountClient(API, store=MemorySessionStore(), transport=transport, clock=lambda: h.clock.mono)
        other.login("ann@example.com", PASSWORD)
        assert other.logout_all() == 2
        with pytest.raises(AccountError) as exc:
            client.account()
        assert exc.value.code == "signed_out" and store.load() is None and not client.signed_in

    def test_errors_are_readable(self, env):
        h, client, _, transport = env
        signed_up(h, client)
        client.logout()
        with pytest.raises(AccountError) as exc:
            client.login("ann@example.com", "wrong password 1")
        assert exc.value.code == "invalid_credentials" and exc.value.status == 401
        transport.offline = True
        with pytest.raises(AccountError) as exc:
            client.login("ann@example.com", PASSWORD)
        assert exc.value.code == "offline"

    def test_logout_clears_even_when_offline(self, env):
        h, client, store, transport = env
        signed_up(h, client)
        transport.offline = True
        client.logout()
        assert store.load() is None

    def test_delete_account(self, env):
        h, client, store, _ = env
        signed_up(h, client)
        client.delete_account()
        assert store.load() is None and h.table_counts()["users"] == 0

    def test_google(self, env):
        h, client, _, _ = env
        account = client.google(google_token())
        assert account["providers"] == ["google"] and client.signed_in

    def test_session_for_another_server_is_ignored(self, env):
        h, client, store, transport = env
        store.save(AccountSession("https://other.example", "u", "e@x.com", "d", "t.x"))
        assert not client.signed_in


class TestUrlsAndStorage:
    @pytest.mark.parametrize("url", ["http://api.example.com", "ftp://x", "", "https://"])
    def test_insecure_or_invalid(self, url):
        with pytest.raises(AccountError):
            validate_api_url(url)

    @pytest.mark.parametrize("url,expected", [("https://api.example.com/", "https://api.example.com"),
                                              ("http://localhost:8000", "http://localhost:8000"),
                                              ("http://127.0.0.1:9000/base/", "http://127.0.0.1:9000/base")])
    def test_allowed(self, url, expected):
        assert validate_api_url(url) == expected

    def test_settings_store_seals_on_mobile(self, db):
        from core.crypto import FieldSealer
        from core.device_settings import SettingsStore
        from core.security import generate_raw_key

        store = SettingsStore(db, sealer=FieldSealer(generate_raw_key()), require_sealing=True)
        sessions = SettingsSessionStore(store)
        session = AccountSession(API, "user-1", "ann@example.com", "device-1", "tokenid.SECRETVALUE")
        sessions.save(session)
        assert sessions.load() == session
        with db.connection() as conn:
            raw = conn.execute("SELECT value_json FROM device_settings WHERE key = 'account_session'").fetchone()[0]
        assert "SECRETVALUE" not in raw and "ann@example.com" not in raw and "sealed" in raw
        sessions.save(None)
        assert sessions.load() is None


# --- Google desktop (PKCE, loopback) ---------------------------------------------------------------------


class FakeGoogleToken:
    def __init__(self, id_token="the.id.token", status=200):
        self.id_token, self.status, self.form = id_token, status, None

    def request(self, method, url, *, form=None, **_):
        assert method == "POST" and url == google_desktop.TOKEN_URL
        self.form = form
        body = {"id_token": self.id_token, "access_token": "ignored"} if self.status == 200 else {"error": "bad"}
        return HttpResponse(self.status, json.dumps(body).encode())


def browser(behaviour="approve"):
    """Plays the user's browser: follows Google's redirect back to the loopback listener."""
    seen = {}

    def open_url(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        seen.update(q)
        params = {"approve": {"code": "auth-code-1", "state": q["state"]},
                  "cancel": {"error": "access_denied", "state": q["state"]},
                  "wrong_state": {"code": "auth-code-1", "state": "forged"}}.get(behaviour)
        if params is None:
            return True
        target = q["redirect_uri"] + "?" + urllib.parse.urlencode(params)
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read(), daemon=True).start()
        return True

    return open_url, seen


class TestGoogleDesktop:
    CONFIG = google_desktop.GoogleClientConfig("desktop-client-id", "not-really-secret")

    def test_pkce_flow(self):
        open_url, seen = browser()
        token_endpoint = FakeGoogleToken()
        id_token = google_desktop.sign_in(self.CONFIG, open_url=open_url, transport=token_endpoint, timeout_s=10)
        assert id_token == "the.id.token"
        assert seen["code_challenge_method"] == "S256" and seen["scope"] == "openid email"
        assert seen["redirect_uri"].startswith("http://127.0.0.1:")
        verifier = token_endpoint.form["code_verifier"]
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        assert seen["code_challenge"] == expected                          # verifier matches the challenge
        assert token_endpoint.form["code"] == "auth-code-1"
        assert token_endpoint.form["redirect_uri"] == seen["redirect_uri"]

    def test_forged_state_is_rejected(self):
        open_url, _ = browser("wrong_state")
        with pytest.raises(AccountError, match="failed"):
            google_desktop.sign_in(self.CONFIG, open_url=open_url, transport=FakeGoogleToken(), timeout_s=10)

    def test_cancel(self):
        open_url, _ = browser("cancel")
        with pytest.raises(AccountError) as exc:
            google_desktop.sign_in(self.CONFIG, open_url=open_url, transport=FakeGoogleToken(), timeout_s=10)
        assert exc.value.code == "google_cancelled"

    def test_timeout(self):
        open_url, _ = browser("ignore")
        with pytest.raises(AccountError) as exc:
            google_desktop.sign_in(self.CONFIG, open_url=open_url, transport=FakeGoogleToken(), timeout_s=2)
        assert exc.value.code == "google_timeout"

    def test_token_endpoint_error(self):
        open_url, _ = browser()
        with pytest.raises(AccountError, match="failed"):
            google_desktop.sign_in(self.CONFIG, open_url=open_url, transport=FakeGoogleToken(status=400), timeout_s=10)

    def test_config_from_env(self):
        assert google_desktop.GoogleClientConfig.from_env({}) is None
        cfg = google_desktop.GoogleClientConfig.from_env({"IFLOW_GOOGLE_DESKTOP_CLIENT_ID": "abc"})
        assert cfg.client_id == "abc" and cfg.client_secret is None

    def test_end_to_end_with_api(self):
        """Browser flow → ID token → iFlow Core API session."""
        h = Harness("sqlite://")
        open_url, _ = browser()
        id_token = google_desktop.sign_in(self.CONFIG, open_url=open_url,
                                          transport=FakeGoogleToken(google_token()), timeout_s=10)
        client = AccountClient(API, transport=AppTransport(h), clock=lambda: h.clock.mono)
        assert client.google(id_token)["email"] == "ann@example.com"
        h.app.state.engine.dispose()
