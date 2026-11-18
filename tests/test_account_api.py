"""Phase 10.2: iFlow Core account API (api/iflow_api).

Runs on in-memory SQLite. Set IFLOW_API_TEST_DATABASE_URL=postgresql+psycopg://… to run the same
tests against PostgreSQL (what AWS RDS runs).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest

API_ROOT = Path(__file__).resolve().parents[1] / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402

from iflow_api.app import create_app  # noqa: E402
from iflow_api.db import Base, make_engine  # noqa: E402
from iflow_api.google import GoogleVerifier  # noqa: E402
from iflow_api.mailer import MemoryMailer  # noqa: E402
from iflow_api.ratelimit import RateLimiter  # noqa: E402
from iflow_api.settings import Argon2Params, Settings, SettingsError, generate_keypair_pem  # noqa: E402

PG_URL = os.environ.get("IFLOW_API_TEST_DATABASE_URL")
PASSWORD = "correct horse battery 7"
DEVICE = {"name": "Laptop", "platform": "linux"}
GOOGLE_CLIENT = "web-client.apps.googleusercontent.com"
GOOGLE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
        self.mono = 1000.0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.mono += seconds


def google_token(sub="g-123", email="ann@example.com", verified=True, aud=GOOGLE_CLIENT, key=GOOGLE_KEY,
                 iss="https://accounts.google.com", exp_in=3600, now=None):
    now = now or datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    claims = {"iss": iss, "aud": aud, "sub": sub, "email": email, "email_verified": verified,
              "iat": int(now.timestamp()), "exp": int(now.timestamp()) + exp_in}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})


class Harness:
    def __init__(self, url: str) -> None:
        self.clock = Clock()
        self.mailer = MemoryMailer()
        base = Settings.from_env({"IFLOW_API_ENV": "test", "IFLOW_API_DATABASE_URL": url})
        self.settings = replace(base, argon2=Argon2Params(1, 8, 1))
        if not url.startswith("sqlite"):
            engine = make_engine(url)
            Base.metadata.drop_all(engine)
            engine.dispose()
        google = GoogleVerifier((GOOGLE_CLIENT,), resolve_key=lambda t: GOOGLE_KEY.public_key(), clock=self.clock)
        self.limiter = RateLimiter(clock=lambda: self.clock.mono)
        self.app = create_app(self.settings, mailer=self.mailer, google=google, clock=self.clock,
                              limiter=self.limiter)
        self.c = TestClient(self.app)

    def last_code(self, to: str = "ann@example.com") -> str:
        msg = [m for m in self.mailer.sent if m.to == to][-1]
        return re.search(r"\b(\d{6})\b", msg.subject).group(1)

    def signup_verified(self, email="ann@example.com", password=PASSWORD, device=DEVICE) -> dict:
        assert self.c.post("/v1/auth/signup", json={"email": email, "password": password}).status_code == 202
        r = self.c.post("/v1/auth/verify", json={"email": email, "code": self.last_code(email), "device": device})
        assert r.status_code == 200, r.text
        return r.json()

    def login(self, email="ann@example.com", password=PASSWORD):
        return self.c.post("/v1/auth/login", json={"email": email, "password": password, "device": DEVICE})

    @staticmethod
    def auth(tokens: dict) -> dict:
        return {"Authorization": f"Bearer {tokens['access_token']}"}

    def table_counts(self) -> dict[str, int]:
        with self.app.state.engine.connect() as conn:
            return {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                    for t in ("users", "identities", "devices", "refresh_tokens", "email_codes")}


@pytest.fixture(params=["sqlite"] + (["postgres"] if PG_URL else []))
def h(request):
    harness = Harness("sqlite://" if request.param == "sqlite" else PG_URL)
    yield harness
    harness.app.state.engine.dispose()


# --- sign-up & verification -------------------------------------------------------------------------


class TestSignup:
    def test_signup_verify_and_account(self, h):
        tokens = h.signup_verified()
        assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 900
        assert tokens["account"]["email_verified"] is True and tokens["account"]["providers"] == ["password"]
        me = h.c.get("/v1/account", headers=h.auth(tokens)).json()
        assert me["email"] == "ann@example.com"

    def test_email_is_normalised(self, h):
        h.c.post("/v1/auth/signup", json={"email": "  Ann@EXAMPLE.com ", "password": PASSWORD})
        assert h.mailer.sent[-1].to == "ann@example.com"

    def test_weak_passwords(self, h):
        for pw in ("short", "ann@example.com", "aaaaaaaaaaaa"):
            r = h.c.post("/v1/auth/signup", json={"email": "ann@example.com", "password": pw})
            assert r.status_code == 400 and r.json()["error"] == "weak_password"

    def test_existing_account_is_not_revealed(self, h):
        h.signup_verified()
        before = len(h.mailer.sent)
        r = h.c.post("/v1/auth/signup", json={"email": "ann@example.com", "password": "another password 9"})
        assert r.status_code == 202 and r.json() == {"status": "verification_sent"}
        assert "tried to sign up" in h.mailer.sent[-1].subject and len(h.mailer.sent) == before + 1
        assert h.login().status_code == 200                                  # password unchanged

    def test_unverified_signup_cannot_block_the_owner(self, h):
        """Pre-hijacking: an attacker registers the victim's email; the victim's own sign-up wins."""
        h.c.post("/v1/auth/signup", json={"email": "ann@example.com", "password": "attacker password 1"})
        h.signup_verified()                                                 # the real owner
        assert h.login(password="attacker password 1").status_code == 401
        assert h.login().status_code == 200

    def test_wrong_code_attempts_then_lockout_of_code(self, h):
        h.c.post("/v1/auth/signup", json={"email": "ann@example.com", "password": PASSWORD})
        code = h.last_code()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            r = h.c.post("/v1/auth/verify", json={"email": "ann@example.com", "code": wrong, "device": DEVICE})
            assert r.status_code == 400 and r.json()["error"] == "invalid_code"
        r = h.c.post("/v1/auth/verify", json={"email": "ann@example.com", "code": code, "device": DEVICE})
        assert r.status_code == 400                                          # burned after 5 attempts

    def test_code_expires(self, h):
        h.c.post("/v1/auth/signup", json={"email": "ann@example.com", "password": PASSWORD})
        h.clock.advance(15 * 60 + 1)
        r = h.c.post("/v1/auth/verify", json={"email": "ann@example.com", "code": h.last_code(), "device": DEVICE})
        assert r.status_code == 400

    def test_resend_voids_the_old_code(self, h):
        h.c.post("/v1/auth/signup", json={"email": "ann@example.com", "password": PASSWORD})
        old = h.last_code()
        h.c.post("/v1/auth/verify/resend", json={"email": "ann@example.com"})
        new = h.last_code()
        if old != new:
            r = h.c.post("/v1/auth/verify", json={"email": "ann@example.com", "code": old, "device": DEVICE})
            assert r.status_code == 400
        r = h.c.post("/v1/auth/verify", json={"email": "ann@example.com", "code": new, "device": DEVICE})
        assert r.status_code == 200

    def test_resend_and_forgot_for_unknown_email_look_the_same(self, h):
        assert h.c.post("/v1/auth/verify/resend", json={"email": "nobody@example.com"}).status_code == 202
        assert h.c.post("/v1/auth/password/forgot", json={"email": "nobody@example.com"}).status_code == 202
        assert h.mailer.sent == []


# --- sign-in ----------------------------------------------------------------------------------------


class TestLogin:
    def test_login(self, h):
        h.signup_verified()
        r = h.login()
        assert r.status_code == 200 and r.json()["account"]["email"] == "ann@example.com"

    def test_unknown_and_wrong_password_look_the_same(self, h):
        h.signup_verified()
        a, b = h.login(password="wrong password 1"), h.login(email="nobody@example.com")
        assert a.status_code == b.status_code == 401 and a.json() == b.json()

    def test_unverified_account(self, h):
        h.c.post("/v1/auth/signup", json={"email": "ann@example.com", "password": PASSWORD})
        r = h.login()
        assert r.status_code == 403 and r.json()["error"] == "email_not_verified"

    def test_lockout_and_recovery(self, h):
        h.signup_verified()
        for _ in range(5):
            assert h.login(password="wrong password 1").status_code == 401
        r = h.login()                                                        # even the right password waits
        assert r.status_code == 429 and int(r.headers["Retry-After"]) == 30
        h.clock.advance(31)
        assert h.login().status_code == 200

    def test_lockout_doubles(self, h):
        h.signup_verified()
        for _ in range(6):
            h.login(password="wrong password 1")
            h.clock.advance(31)
        h.clock.advance(-31)
        assert int(h.login().headers["Retry-After"]) > 30

    def test_ip_rate_limit(self, h):
        for i in range(10):
            h.c.post("/v1/auth/signup", json={"email": f"u{i}@example.com", "password": PASSWORD})
        r = h.c.post("/v1/auth/signup", json={"email": "u99@example.com", "password": PASSWORD})
        assert r.status_code == 429 and r.json()["error"] == "rate_limited" and "Retry-After" in r.headers


# --- tokens & sessions --------------------------------------------------------------------------------


class TestSessions:
    def test_access_token_expires(self, h):
        tokens = h.signup_verified()
        h.clock.advance(15 * 60 + 10)
        assert h.c.get("/v1/account", headers=h.auth(tokens)).status_code == 401

    def test_refresh_rotates_and_reuse_revokes(self, h):
        t1 = h.signup_verified()
        t2 = h.c.post("/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]}).json()
        assert t2["refresh_token"] != t1["refresh_token"] and t2["device_id"] == t1["device_id"]
        reuse = h.c.post("/v1/auth/refresh", json={"refresh_token": t1["refresh_token"]})
        assert reuse.status_code == 401 and reuse.json()["error"] == "token_reused"
        # the thief's (or owner's) newer tokens are dead too: the device is revoked
        assert h.c.post("/v1/auth/refresh", json={"refresh_token": t2["refresh_token"]}).status_code == 401
        assert h.c.get("/v1/account", headers=h.auth(t2)).status_code == 401

    def test_refresh_token_expiry_and_garbage(self, h):
        t = h.signup_verified()
        for bad in ("nope", "x.y", t["refresh_token"][:-2] + "AA", ""):
            assert h.c.post("/v1/auth/refresh", json={"refresh_token": bad}).status_code in (401, 422)
        h.clock.advance(61 * 24 * 3600)
        assert h.c.post("/v1/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401

    def test_forged_and_foreign_tokens(self, h):
        t = h.signup_verified()
        other = Ed25519PrivateKey.generate()
        claims = jwt.decode(t["access_token"], options={"verify_signature": False})
        forged = jwt.encode(claims, other, algorithm="EdDSA", headers={"kid": "k1"})
        none_alg = jwt.encode(claims, key=None, algorithm="none")
        tampered = t["access_token"][:-4] + ("AAAA" if not t["access_token"].endswith("AAAA") else "BBBB")
        for token in (forged, none_alg, tampered, "garbage"):
            assert h.c.get("/v1/account", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        assert h.c.get("/v1/account").status_code == 401

    def test_logout_is_immediate(self, h):
        t = h.signup_verified()
        assert h.c.post("/v1/auth/logout", headers=h.auth(t)).status_code == 204
        assert h.c.get("/v1/account", headers=h.auth(t)).status_code == 401       # before the token expires
        assert h.c.post("/v1/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401

    def test_devices_and_logout_all(self, h):
        t1 = h.signup_verified()
        t2 = h.login().json()
        devices = h.c.get("/v1/account/devices", headers=h.auth(t1)).json()["devices"]
        assert len(devices) == 2 and sum(d["current"] for d in devices) == 1
        assert all(len(d["last_seen_on"]) == 10 for d in devices)               # day precision only
        assert h.c.delete(f"/v1/account/devices/{t2['device_id']}", headers=h.auth(t1)).status_code == 204
        assert h.c.get("/v1/account", headers=h.auth(t2)).status_code == 401
        assert h.c.post("/v1/account/logout-all", headers=h.auth(t1)).json() == {"revoked": 1}
        assert h.c.get("/v1/account", headers=h.auth(t1)).status_code == 401

    def test_cannot_touch_another_users_device(self, h):
        ann = h.signup_verified()
        bob = h.signup_verified(email="bob@example.com")
        r = h.c.delete(f"/v1/account/devices/{bob['device_id']}", headers=h.auth(ann))
        assert r.status_code == 404
        assert h.c.get("/v1/account", headers=h.auth(bob)).status_code == 200


# --- passwords ----------------------------------------------------------------------------------------


class TestPasswords:
    def test_reset_signs_out_everywhere(self, h):
        t = h.signup_verified()
        h.c.post("/v1/auth/password/forgot", json={"email": "ann@example.com"})
        r = h.c.post("/v1/auth/password/reset", json={"email": "ann@example.com", "code": h.last_code(),
                                                       "new_password": "a brand new secret 3"})
        assert r.status_code == 204
        assert h.c.get("/v1/account", headers=h.auth(t)).status_code == 401
        assert h.login().status_code == 401 and h.login(password="a brand new secret 3").status_code == 200

    def test_reset_clears_lockout(self, h):
        h.signup_verified()
        for _ in range(6):
            h.login(password="wrong password 1")
        h.c.post("/v1/auth/password/forgot", json={"email": "ann@example.com"})
        h.c.post("/v1/auth/password/reset", json={"email": "ann@example.com", "code": h.last_code(),
                                                  "new_password": "a brand new secret 3"})
        assert h.login(password="a brand new secret 3").status_code == 200

    def test_change_password_keeps_this_device(self, h):
        t1 = h.signup_verified()
        t2 = h.login().json()
        r = h.c.post("/v1/account/password", headers=h.auth(t1),
                     json={"current_password": "wrong password 1", "new_password": "another secret 44"})
        assert r.status_code == 401
        r = h.c.post("/v1/account/password", headers=h.auth(t1),
                     json={"current_password": PASSWORD, "new_password": "another secret 44"})
        assert r.status_code == 204
        assert h.c.get("/v1/account", headers=h.auth(t1)).status_code == 200
        assert h.c.get("/v1/account", headers=h.auth(t2)).status_code == 401


# --- Google -------------------------------------------------------------------------------------------


class TestGoogle:
    def google(self, h, token):
        return h.c.post("/v1/auth/google", json={"id_token": token, "device": DEVICE})

    def test_new_google_user(self, h):
        r = self.google(h, google_token())
        assert r.status_code == 200
        acct = r.json()["account"]
        assert acct["email_verified"] and acct["providers"] == ["google"]
        again = self.google(h, google_token())
        assert again.json()["account"]["id"] == acct["id"]

    def test_links_existing_verified_account(self, h):
        t = h.signup_verified()
        r = self.google(h, google_token())
        assert r.json()["account"]["id"] == t["account"]["id"]
        assert r.json()["account"]["providers"] == ["google", "password"]
        assert h.login().status_code == 200                                  # password still works

    def test_takes_over_unverified_squatter(self, h):
        h.c.post("/v1/auth/signup", json={"email": "ann@example.com", "password": "attacker password 1"})
        r = self.google(h, google_token())
        assert r.status_code == 200 and r.json()["account"]["providers"] == ["google"]
        assert h.login(password="attacker password 1").status_code == 401

    def test_rejections(self, h):
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        bad = [google_token(verified=False), google_token(aud="someone-else"), google_token(key=other_key),
               google_token(iss="https://evil.example"), google_token(exp_in=-3600), google_token(email=""),
               "not-a-jwt"]
        for token in bad:
            r = self.google(h, token)
            assert r.status_code == 401 and r.json()["error"] == "invalid_google_token", token[:20]
        assert h.table_counts()["users"] == 0

    def test_disabled_without_client_ids(self):
        settings = replace(Settings.from_env({"IFLOW_API_ENV": "test", "IFLOW_API_DATABASE_URL": "sqlite://"}),
                           argon2=Argon2Params(1, 8, 1))
        c = TestClient(create_app(settings, mailer=MemoryMailer(), google=GoogleVerifier((), resolve_key=lambda t: None)))
        r = c.post("/v1/auth/google", json={"id_token": google_token(), "device": DEVICE})
        assert r.status_code == 401


# --- deletion & privacy -------------------------------------------------------------------------------


class TestDeletionAndPrivacy:
    def test_delete_requires_fresh_sign_in(self, h):
        t = h.signup_verified()
        h.clock.advance(11 * 60)
        refreshed = h.c.post("/v1/auth/refresh", json={"refresh_token": t["refresh_token"]}).json()
        r = h.c.delete("/v1/account", headers=h.auth(refreshed))            # refresh ≠ fresh sign-in
        assert r.status_code == 403 and r.json()["error"] == "reauth_required"
        fresh = h.login().json()
        assert h.c.delete("/v1/account", headers=h.auth(fresh)).status_code == 204
        assert h.table_counts() == {"users": 0, "identities": 0, "devices": 0, "refresh_tokens": 0,
                                    "email_codes": 0}
        assert h.login().status_code == 401

    def test_delete_google_account_removes_everything(self, h):
        t = h.c.post("/v1/auth/google", json={"id_token": google_token(), "device": DEVICE}).json()
        assert h.c.delete("/v1/account", headers=h.auth(t)).status_code == 204
        assert set(h.table_counts().values()) == {0}

    def test_no_secrets_at_rest(self, h):
        t = h.signup_verified()
        h.c.post("/v1/auth/password/forgot", json={"email": "ann@example.com"})
        code = h.last_code()
        refresh_secret = t["refresh_token"].split(".", 1)[1]
        with h.app.state.engine.connect() as conn:
            dump = []
            for table in inspect(conn).get_table_names():
                for row in conn.execute(text(f"SELECT * FROM {table}")):
                    dump.extend(str(v) for v in row)
        blob = "\n".join(dump)
        assert PASSWORD not in blob and code not in blob and refresh_secret not in blob
        assert t["access_token"] not in blob

    def test_errors_never_echo_input(self, h):
        r = h.c.post("/v1/auth/login", json={"email": "ann@example.com", "password": "p" * 300, "device": DEVICE})
        assert r.status_code == 422 and "ppp" not in r.text and r.json()["fields"] == ["password"]
        r = h.c.post("/v1/auth/signup", json={"email": "a@b.co", "password": PASSWORD, "is_admin": True})
        assert r.status_code == 422                                          # unknown fields refused
        big = h.c.post("/v1/auth/signup", content=b"{" + b" " * 20000 + b"}",
                       headers={"Content-Type": "application/json"})
        assert big.status_code == 413

    def test_security_headers(self, h):
        r = h.c.get("/healthz")
        assert r.json()["status"] == "ok" and r.headers["Cache-Control"] == "no-store"
        assert r.headers["X-Content-Type-Options"] == "nosniff"

    def test_invalid_device(self, h):
        h.c.post("/v1/auth/signup", json={"email": "ann@example.com", "password": PASSWORD})
        r = h.c.post("/v1/auth/verify", json={"email": "ann@example.com", "code": h.last_code(),
                                              "device": {"name": "x", "platform": "toaster"}})
        assert r.status_code == 400 and r.json()["error"] == "invalid_device"


# --- configuration & schema ---------------------------------------------------------------------------


class TestSettings:
    def test_production_refuses_dev_defaults(self):
        base = {"IFLOW_API_ENV": "production"}
        with pytest.raises(SettingsError, match="DATABASE_URL"):
            Settings.from_env(base)
        with pytest.raises(SettingsError, match="PostgreSQL"):
            Settings.from_env({**base, "IFLOW_API_DATABASE_URL": "sqlite:///x.db"})
        pg = {**base, "IFLOW_API_DATABASE_URL": "postgresql+psycopg://u:p@db/iflow"}
        with pytest.raises(SettingsError, match="JWT_PRIVATE_KEY"):
            Settings.from_env(pg)
        private, _ = generate_keypair_pem()
        keyed = {**pg, "IFLOW_API_JWT_PRIVATE_KEY": private}
        with pytest.raises(SettingsError, match="CODE_PEPPER"):
            Settings.from_env(keyed)
        peppered = {**keyed, "IFLOW_API_CODE_PEPPER": "ab" * 32}
        with pytest.raises(SettingsError, match="SES"):
            Settings.from_env({**peppered, "IFLOW_API_MAIL_BACKEND": "console"})
        with pytest.raises(SettingsError, match="MAIL_FROM"):
            Settings.from_env(peppered)
        s = Settings.from_env({**peppered, "IFLOW_API_MAIL_FROM": "iFlow Core <no-reply@example.com>"})
        assert s.is_production and s.mail_backend == "ses"

    def test_key_rotation(self):
        old_private, old_public = generate_keypair_pem()
        new_private, _ = generate_keypair_pem()
        env = {"IFLOW_API_ENV": "test", "IFLOW_API_DATABASE_URL": "sqlite://"}
        old = replace(Settings.from_env({**env, "IFLOW_API_JWT_PRIVATE_KEY": old_private, "IFLOW_API_JWT_KEY_ID": "k1"}),
                      argon2=Argon2Params(1, 8, 1))
        new = Settings.from_env({**env, "IFLOW_API_JWT_PRIVATE_KEY": new_private, "IFLOW_API_JWT_KEY_ID": "k2",
                                 "IFLOW_API_JWT_PREVIOUS_KEYS": f"k1={old_public}"})
        from iflow_api.security import issue_access_token, verify_access_token

        now = datetime.now(timezone.utc)
        token = issue_access_token(old, user_id="u", device_id="d", auth_time=now, now=now)
        assert verify_access_token(new, token, now=now).user_id == "u"          # accepted during rotation

    def test_invalid_values(self):
        with pytest.raises(SettingsError):
            Settings.from_env({"IFLOW_API_ENV": "staging"})
        with pytest.raises(SettingsError):
            Settings.from_env({"IFLOW_API_CODE_PEPPER": "abcd"})
        with pytest.raises(SettingsError):
            Settings.from_env({"IFLOW_API_JWT_PRIVATE_KEY": "not a pem"})


def test_alembic_migration_matches_models(tmp_path):
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    url = PG_URL or f"sqlite:///{tmp_path / 'm.db'}"
    if PG_URL:
        engine = make_engine(url)
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        engine.dispose()
    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "migrations"))
    cfg.cmd_opts = type("Opts", (), {"x": [f"url={url}"]})()
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
    command.downgrade(cfg, "base")
    with engine.connect() as conn:
        assert set(inspect(conn).get_table_names()) <= {"alembic_version"}
    engine.dispose()


def test_cors_for_the_web_app():
    settings = replace(Settings.from_env({"IFLOW_API_ENV": "test", "IFLOW_API_DATABASE_URL": "sqlite://",
                                          "IFLOW_API_CORS_ORIGINS": "https://iflowcore.com"}),
                       argon2=Argon2Params(1, 8, 1))
    c = TestClient(create_app(settings, mailer=MemoryMailer()))
    pre = c.options("/v1/auth/login", headers={"Origin": "https://iflowcore.com",
                                               "Access-Control-Request-Method": "POST",
                                               "Access-Control-Request-Headers": "content-type"})
    assert pre.headers.get("access-control-allow-origin") == "https://iflowcore.com"
    evil = c.options("/v1/auth/login", headers={"Origin": "https://evil.example",
                                                "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in evil.headers
    for bad in ("*", "http://iflowcore.com"):
        with pytest.raises(SettingsError):
            Settings.from_env({"IFLOW_API_CORS_ORIGINS": bad, "IFLOW_API_ENV": "production"})


# --- AWS deployment (Phase 10.4) ---------------------------------------------------------------------------


class TestDeploymentSettings:
    SECRET = {"username": "iflow", "password": "p@ss:w/rd%+?#", "host": "db.abc.us-east-1.rds.amazonaws.com",
              "port": 5432, "dbname": "iflow", "engine": "postgres"}

    def test_url_from_rds_secret(self):
        import json

        from sqlalchemy.engine import make_url

        from iflow_api.settings import database_url_from_secret

        url = make_url(database_url_from_secret(json.dumps(self.SECRET)))
        assert url.drivername == "postgresql+psycopg" and url.password == self.SECRET["password"]
        assert url.host == self.SECRET["host"] and url.database == "iflow" and url.query["sslmode"] == "require"

    @pytest.mark.parametrize("raw,msg", [("not json", "RDS secret JSON"), ("[]", "RDS secret JSON"),
                                         ('{"username": "a", "host": "h"}', "password"),
                                         ('{"username": "a", "password": "b", "host": "h", "engine": "mysql"}',
                                          "PostgreSQL")])
    def test_bad_secrets(self, raw, msg):
        with pytest.raises(SettingsError, match=msg):
            Settings.from_env({"IFLOW_API_ENV": "test", "IFLOW_API_DATABASE_SECRET": raw})

    def test_url_or_secret_not_both(self):
        import json

        with pytest.raises(SettingsError, match="not both"):
            Settings.from_env({"IFLOW_API_ENV": "test", "IFLOW_API_DATABASE_URL": "sqlite://",
                               "IFLOW_API_DATABASE_SECRET": json.dumps(self.SECRET)})

    def test_production_accepts_the_secret(self):
        import json

        private, _ = generate_keypair_pem()
        s = Settings.from_env({"IFLOW_API_ENV": "production", "IFLOW_API_DATABASE_SECRET": json.dumps(self.SECRET),
                               "IFLOW_API_JWT_PRIVATE_KEY": private, "IFLOW_API_CODE_PEPPER": "ab" * 32,
                               "IFLOW_API_MAIL_FROM": "iFlow Core <no-reply@iflowcore.com>"})
        assert s.is_production and s.database_url.endswith("sslmode=require")

    def test_readyz(self, h, monkeypatch):
        r = h.c.get("/readyz")
        assert r.status_code == 200 and r.json()["status"] == "ready"

        def down(*a, **k):
            raise OSError("connection refused: db.internal")
        monkeypatch.setattr(h.app.state.engine, "connect", down)
        r = h.c.get("/readyz")
        assert r.status_code == 503 and r.json() == {"status": "unavailable"}      # no internals in the reply

    def test_alembic_handles_percent_in_the_url(self, tmp_path):
        import subprocess

        db = tmp_path / "a%b.db"                     # a URL-escaped password has % in it too
        url = "sqlite:///" + str(db).replace("%", "%25")
        result = subprocess.run([sys.executable, "-m", "alembic", "-x", f"url={url}", "upgrade", "head"],
                                cwd=API_ROOT, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr[-800:]
        assert db.exists()
