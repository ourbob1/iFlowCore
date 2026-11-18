"""Phase 11: statement formats reach the app from the CDN, and imports recognise their format.

Detection by header line (published formats > the user's own > built-ins), the Finances import card
choosing format and platform by itself, the once-a-day signed download (ETag, trust, failures), the
Settings card, the service's S3 publisher, and the config switch."""

from __future__ import annotations

import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.financial.ingestion import detect_profile, fit_score, BUILTIN_PROFILES
from core.financial.parsers import dynamic
from core.fleet.http import HttpResponse, ProviderError

SCHEMA = {"id": "acme-weekly", "version": 1, "name": "Acme weekly", "platform": "Acme",
          "rules": {"occurred_at": {"columns": ["Date"]}, "gross": {"columns": ["Fare"]},
                    "tips": {"columns": ["Tip Note"], "regex": r"Tip: \$?([\d.]+)"},
                    "fees": {"columns": ["Service Fee"], "multiplier_bp": -10000}},
          "detect_headers": ["Date", "Fare", "Tip Note", "Service Fee"], "timezone": "UTC"}
ACME_CSV = ("Date,Trip ID,Fare,Tip Note,Service Fee\n"
            "2026-09-01 08:15,T-1,$24.50,Tip: $5.00,-6.13\n")
UBER_CSV = "Date/Time,Trip UUID,Fare,Tip,Service Fee,Tolls\n2026-09-01 08:15,u-1,24.50,5.00,6.13,2.25\n"
KEY = Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes_raw().hex()


def install_acme(db, version=1):
    return dynamic.install_bundle(db, dynamic.sign_bundle([{**SCHEMA, "version": version}], KEY), [PUB])


# --- detection -------------------------------------------------------------------------------------------


class TestDetection:
    def test_builtin_by_columns(self, db, tmp_path):
        path = tmp_path / "uber.csv"
        path.write_text(UBER_CSV)
        found = detect_profile(db, path)
        assert found.name == "uber-driver-csv" and found.origin == "builtin"

    def test_published_format_wins(self, db, tmp_path):
        install_acme(db)
        path = tmp_path / "acme.csv"
        path.write_text(ACME_CSV)
        found = detect_profile(db, path)
        assert found.origin == "vendor" and found.name == "Acme weekly (iFlow format v1)" and found.score > 100

    def test_published_format_needs_every_signature_header(self, db, tmp_path):
        install_acme(db)
        path = tmp_path / "almost.csv"
        path.write_text("Date,Fare,Tip Note\n2026-09-01,1.00,\n")          # no Service Fee column
        found = detect_profile(db, path)
        assert found is None or found.origin != "vendor"

    def test_header_line_below_a_preamble(self, db, tmp_path):
        path = tmp_path / "uber.csv"
        path.write_text("Uber weekly statement\nDriver: (redacted)\n" + UBER_CSV)
        assert detect_profile(db, path).name == "uber-driver-csv"

    def test_nothing_fits(self, db, tmp_path):
        path = tmp_path / "other.csv"
        path.write_text("Foo,Bar\n1,2\n")
        assert detect_profile(db, path) is None
        assert fit_score(BUILTIN_PROFILES["generic-pdf-statement"], ["date", "amount"]) is None   # PDFs: by hand

    def test_recognize_picks_the_platform(self, db, tmp_path, platform_id):
        from ui.viewmodels.finances import recognize

        path = tmp_path / "uber.csv"
        path.write_text(UBER_CSV)
        uber = platform_id                                         # the fixture's platform is "Uber"
        found = recognize(db, str(path), (("other-id", "Lyft"), (uber, "Uber")))
        assert found.profile == "uber-driver-csv" and found.platform_id == uber
        assert found.label.startswith("Recognized: Uber statement")
        assert recognize(db, str(tmp_path / "statement.pdf"), ()) is None


class TestImportCard:
    def test_choosing_a_file_sets_format_and_platform(self, tmp_path):
        from tests.ui_helpers import find_key, of_type, texts

        from .test_ui_app import make_app
        import flet as ft

        app, page = make_app(tmp_path)
        app.start()
        with app.state.db.transaction() as conn:
            for name in ("Lyft", "Acme"):
                conn.execute("INSERT INTO platforms (id, name, kind) VALUES (?, ?, 'rideshare')",
                             (str(uuid.uuid4()), name))
        install_acme(app.state.db)
        app.navigate("/finances")
        path = tmp_path / "acme.csv"
        path.write_text(ACME_CSV)
        app.services["import_load_file"](str(path))
        card = find_key(page.views[-1], "import-card")
        profile, platform = of_type(card, ft.Dropdown)[:2]
        assert profile.value == "Acme weekly (iFlow format v1)"
        acme_id = next(o.key for o in platform.options if o.text == "Acme")
        assert platform.value == acme_id
        shown = "\n".join(texts(card))
        assert "Recognized: Acme weekly (iFlow format v1)" in shown and "2 records from 1 rows" in shown     # fare + tip
        assert not find_key(card, "btn-commit-import").disabled

    def test_unrecognized_file_says_so(self, tmp_path):
        from tests.ui_helpers import find_key, texts

        from .test_ui_app import make_app

        app, page = make_app(tmp_path)
        app.start()
        with app.state.db.transaction() as conn:
            conn.execute("INSERT INTO platforms (id, name, kind) VALUES (?, 'Lyft', 'rideshare')", (str(uuid.uuid4()),))
        app.navigate("/finances")
        path = tmp_path / "odd.csv"
        path.write_text("Foo,Bar\n1,2\n")
        app.services["import_load_file"](str(path))
        assert "Format not recognized" in "\n".join(texts(find_key(page.views[-1], "import-card")))


# --- downloads from the CDN --------------------------------------------------------------------------------


class FakeCdn:
    def __init__(self, body: bytes | None = None, status: int = 200, fail: bool = False):
        self.body, self.status, self.fail, self.requests = body, status, fail, []

    def request(self, method, url, *, params=None, form=None, headers=None, body=None, timeout=15.0):
        self.requests.append((method, url, dict(headers or {})))
        if self.fail:
            raise ProviderError("offline")
        if (headers or {}).get("If-None-Match") == '"v1"':
            return HttpResponse(304, b"", {"ETag": '"v1"'})
        return HttpResponse(self.status, self.body or b"", {"ETag": '"v1"'})


@pytest.fixture
def unlocked(tmp_path, monkeypatch):
    from .test_ui_app import make_app

    monkeypatch.setenv("IFLOW_LICENSE_PUBKEYS", PUB)
    app, page = make_app(tmp_path)
    app.start()
    return app


class TestDailyCheck:
    def test_download_verify_install(self, unlocked):
        from ui.viewmodels import formats

        cdn = FakeCdn(dynamic.sign_bundle([SCHEMA], KEY).encode())
        message = formats.check(unlocked.state, {"formats_transport": cdn})
        assert message == "Statement formats: 1 new."
        method, url, headers = cdn.requests[0]
        assert method == "GET" and url == "https://formats.iflowcore.com/v1/bundle.ifpb"
        # an anonymous request: nothing identifies the user or device
        assert set(headers) <= {"Accept", "If-None-Match"}
        s = unlocked.state.settings
        assert s.formats_etag == '"v1"' and s.formats_checked_at
        assert [f.name for f in formats.installed(unlocked.state.db)] == ["Acme weekly"]

    def test_once_a_day_then_etag(self, unlocked):
        from ui.viewmodels import formats

        cdn = FakeCdn(dynamic.sign_bundle([SCHEMA], KEY).encode())
        formats.check(unlocked.state, {"formats_transport": cdn})
        assert formats.check(unlocked.state, {"formats_transport": cdn}) is None      # not due yet
        assert len(cdn.requests) == 1
        assert formats.check(unlocked.state, {"formats_transport": cdn}, force=True) == \
            "Statement formats are up to date."                                    # 304: nothing downloaded
        assert cdn.requests[-1][2]["If-None-Match"] == '"v1"'

    def test_auto_update_off(self, unlocked):
        from ui.viewmodels import formats

        unlocked.state.update_settings(formats_auto_update=False)
        cdn = FakeCdn(b"")
        assert formats.check(unlocked.state, {"formats_transport": cdn}) is None and not cdn.requests

    @pytest.mark.parametrize("cdn,msg", [
        (FakeCdn(fail=True), "Couldn't reach"),
        (FakeCdn(status=404), "published yet"),
        (FakeCdn(b"IFPB1.garbage.sig"), "isn't signed"),
        (FakeCdn(dynamic.sign_bundle([SCHEMA], Ed25519PrivateKey.generate()).encode()), "isn't signed"),
    ])
    def test_failures_are_quiet_and_retry_tomorrow(self, unlocked, cdn, msg):
        from ui.viewmodels import formats

        message = formats.check(unlocked.state, {"formats_transport": cdn})
        assert msg in message and unlocked.state.settings.formats_checked_at
        assert formats.installed(unlocked.state.db) == []
        assert formats.check(unlocked.state, {"formats_transport": cdn}) is None      # not again today

    def test_build_without_a_trusted_publisher(self, tmp_path, monkeypatch):
        from ui.viewmodels import formats

        from .test_ui_app import make_app

        monkeypatch.delenv("IFLOW_LICENSE_PUBKEYS", raising=False)
        monkeypatch.setattr("core.licensing.tokens.VENDOR_PUBLIC_KEYS", ())
        app, _ = make_app(tmp_path)
        app.start()
        assert "doesn't trust" in formats.check(app.state, {"formats_transport": FakeCdn(b"")})

    def test_downgrade_is_ignored(self, unlocked):
        from ui.viewmodels import formats

        install_acme(unlocked.state.db, version=3)
        cdn = FakeCdn(dynamic.sign_bundle([SCHEMA], KEY).encode())
        assert "older version" in formats.check(unlocked.state, {"formats_transport": cdn})
        assert [f.version for f in formats.installed(unlocked.state.db)] == [3]

    @pytest.mark.live_format_check
    def test_app_checks_after_unlock(self, tmp_path, monkeypatch):
        from .test_ui_app import make_app

        monkeypatch.setenv("IFLOW_LICENSE_PUBKEYS", PUB)
        cdn = FakeCdn(dynamic.sign_bundle([SCHEMA], KEY).encode())
        app, _ = make_app(tmp_path, services={"formats_transport": cdn})
        app.start()                                         # dev mode unlocks at start → background check
        assert cdn.requests and app.services.get("formats_status") == "Statement formats: 1 new."


class TestSettingsCard:
    def test_card(self, unlocked):
        from tests.ui_helpers import find_key, texts

        cdn = FakeCdn(dynamic.sign_bundle([SCHEMA], KEY).encode())
        unlocked.services["formats_transport"] = cdn
        unlocked.navigate("/settings")
        view = unlocked.page.views[-1]
        assert find_key(view, "formats-auto").value is True
        find_key(view, "btn-formats-check").on_click(None)
        card = find_key(unlocked.page.views[-1], "formats-card")
        shown = "\n".join(texts(card))
        assert "Acme weekly · Acme · v1" in shown and "1 new" in shown
        switch = find_key(card, "formats-auto")
        switch.value = False
        switch.on_change(type("E", (), {"control": switch})())
        assert unlocked.state.settings.formats_auto_update is False

    def test_turned_off_by_config(self, tmp_path):
        from tests.ui_helpers import find_key

        from .test_ui_app import make_app

        app, page = make_app(tmp_path, extra_env={"IFLOW_FORMATS_URL": "off"})
        app.start()
        app.navigate("/settings")
        assert find_key(page.views[-1], "btn-formats-check") is None
        assert find_key(page.views[-1], "btn-formats-import") is not None           # offline import still works


class TestConfig:
    def test_formats_url(self, tmp_path):
        from core.config import DEFAULT_FORMATS_URL, ConfigError, load_config

        base = {"IFLOW_DATA_DIR": str(tmp_path)}
        assert load_config(base).formats_url == DEFAULT_FORMATS_URL
        assert load_config({**base, "IFLOW_FORMATS_URL": "off"}).formats_url is None
        assert load_config({**base, "IFLOW_FORMATS_URL": "http://localhost:9000/v1/bundle.ifpb"}).formats_url
        with pytest.raises(ConfigError, match="https"):
            load_config({**base, "IFLOW_FORMATS_URL": "http://cdn.example.com/b.ifpb"})
        with pytest.raises(ConfigError, match="https"):
            load_config({**base, "IFLOW_ENV": "production", "IFLOW_FORMATS_URL": "http://localhost:9000/b"})


# --- the service's S3 publisher ------------------------------------------------------------------------------


class TestS3Publisher:
    def _publisher(self, s3=None, cf=None):
        from .test_ai_service import ADMIN  # noqa: F401  (puts services/ai on sys.path)
        from iflow_ai.format_publisher import S3Publisher

        return S3Publisher("iflow-formats", distribution_id="E123", public_url="https://formats.x/v1/bundle.ifpb",
                           client=s3, cloudfront=cf)

    def test_upload_order_cache_and_invalidation(self):
        class S3:
            puts = []

            def put_object(self, **kw):
                self.puts.append(kw)

        class CF:
            calls = []

            def create_invalidation(self, **kw):
                self.calls.append(kw)

        s3, cf = S3(), CF()
        out = self._publisher(s3, cf).publish("IFPB1.a.b", "2026-09-28T10:00:00Z", 2)
        keys = [p["Key"] for p in s3.puts]
        assert keys[0].startswith("v1/archive/20260928T100000Z-") and keys[1:] == ["v1/bundle.ifpb", "v1/latest.json"]
        assert "immutable" in s3.puts[0]["CacheControl"] and s3.puts[1]["CacheControl"] == "public, max-age=300"
        assert cf.calls[0]["DistributionId"] == "E123"
        assert set(cf.calls[0]["InvalidationBatch"]["Paths"]["Items"]) == {"/v1/bundle.ifpb", "/v1/latest.json"}
        assert out.schemas == 2 and out.target == "s3://iflow-formats/v1/"

    def test_expired_sso_session(self):
        from iflow_ai.format_publisher import PublishError

        class TokenRetrievalError(Exception):
            pass

        class S3:
            def put_object(self, **kw):
                raise TokenRetrievalError("expired")

        with pytest.raises(PublishError, match="aws sso login"):
            self._publisher(S3(), None).publish("IFPB1.a.b", "2026-09-28T10:00:00Z", 1)

    def test_from_env(self, tmp_path):
        from iflow_ai.format_publisher import DirectoryPublisher, S3Publisher, from_env

        assert from_env({}) is None
        assert isinstance(from_env({"RS_AI_FORMATS_DIR": str(tmp_path)}), DirectoryPublisher)
        assert isinstance(from_env({"RS_AI_FORMATS_BUCKET": "b", "RS_AI_FORMATS_DIR": "x"}), S3Publisher)
