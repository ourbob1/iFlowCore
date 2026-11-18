"""Phase 11: the Admin Console. Console accounts (Argon2id, forced password change, lockout, roles,
owner guardrails), the Parser Studio backend (drafts → publish → signed bundle) cross-checked
against the app's verifier, the app-side ingestion v2 features, bundle install/sync, and the
standalone console UI (login page, password change, users, Parser Studio)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from core.financial.ingestion import MappingProfile, safe_pattern
from core.financial.parsers import dynamic
from core.financial.parsers.dynamic import ParserSchema, SchemaError, test_parse as run_parse, verify_bundle

from .test_admin import SEED, ServiceTransport, signing_public_hex
from .test_ai_service import ADMIN, make

from iflow_ai import parsers as svc_parsers  # noqa: E402  (path set up by test_ai_service)

URL = "https://ai.iflowcore.test"
H = {"Authorization": f"Bearer {ADMIN}"}                 # a service token = owner
GOOD_PASSWORD = "Harbor-lantern-93!"
SAMPLE = ("Date,Trip ID,Fare,Tip Note,Service Fee,Tolls,Paid On\n"
          "2026-09-01 08:15,T-1001,$24.50,Tip: $5.00,-6.13,2.25,2026-09-08\n"
          "2026-09-02 17:40,T-1002,$18.00,,-4.50,0.00,2026-09-08\n")
SCHEMA = {"id": "acme-weekly", "version": 1, "name": "Acme weekly", "platform": "Acme",
          "rules": {"occurred_at": {"columns": ["Date"]}, "gross": {"columns": ["Fare"]},
                    "tips": {"columns": ["Tip Note"], "regex": r"Tip: \$?([\d.]+)"},
                    "fees": {"columns": ["Service Fee"], "multiplier_bp": -10000},
                    "tolls": {"columns": ["Tolls"]}, "paid_out": {"columns": ["Paid On"]},
                    "reference": {"columns": ["Trip ID"]}},
          "timezone": "UTC"}


@pytest.fixture
def api(tmp_path):
    with TestClient(make(tmp_path, RS_AI_LICENSE_SIGNING_KEY=SEED, RS_AI_ADMIN_FAILURES_PER_10MIN="100")) as c:
        yield c


def create_user(c, username="casey", role="developer", headers=H) -> str:
    r = c.post("/admin/v1/users", headers=headers, json={"username": username, "display_name": username.title(),
                                                         "role": role})
    assert r.status_code == 200, r.text
    return r.json()["temporary_password"]


def sign_in(c, username, password) -> dict[str, str]:
    r = c.post("/admin/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session_token']}"}


def ready_user(c, username="casey", role="developer") -> dict[str, str]:
    """A user who has already chosen their own password."""
    temp = create_user(c, username, role)
    h = sign_in(c, username, temp)
    assert c.post("/admin/v1/auth/password", headers=h,
                  json={"current_password": temp, "new_password": GOOD_PASSWORD}).status_code == 200
    return h


# --- console accounts --------------------------------------------------------------------------------------


class TestAccounts:
    def test_temporary_password_forces_a_change(self, api):
        temp = create_user(api)
        h = sign_in(api, "casey", temp)
        me = api.get("/admin/v1/auth/me", headers=h).json()
        assert me["must_change_password"] and me["user"]["username"] == "casey" and me["actor"] == "user:casey"
        blocked = api.get("/admin/v1/overview", headers=h)
        assert blocked.status_code == 403 and blocked.headers.get("x-password-change") == "required"
        weak = api.post("/admin/v1/auth/password", headers=h, json={"current_password": temp,
                                                                    "new_password": "casey-password1"})
        assert weak.status_code == 400
        wrong = api.post("/admin/v1/auth/password", headers=h, json={"current_password": "nope-nope-nope",
                                                                     "new_password": GOOD_PASSWORD})
        assert wrong.status_code == 400                 # not 401: the session itself is fine
        assert api.post("/admin/v1/auth/password", headers=h,
                        json={"current_password": temp, "new_password": GOOD_PASSWORD}).status_code == 200
        assert api.get("/admin/v1/overview", headers=h).status_code == 200
        assert api.post("/admin/v1/auth/login", json={"username": "casey", "password": temp}).status_code == 401

    def test_lockout_and_unlock(self, api):
        ready_user(api)
        for _ in range(5):
            assert api.post("/admin/v1/auth/login", json={"username": "casey", "password": "x" * 12}).status_code == 401
        locked = api.post("/admin/v1/auth/login", json={"username": "casey", "password": GOOD_PASSWORD})
        assert locked.status_code == 429                # even the right password, until unlocked
        uid = next(u["id"] for u in api.get("/admin/v1/users", headers=H).json()["users"] if u["username"] == "casey")
        assert api.post(f"/admin/v1/users/{uid}/unlock", headers=H).status_code == 200
        sign_in(api, "casey", GOOD_PASSWORD)

    def test_role_permissions(self, api):
        dev = ready_user(api, "dev", "developer")
        sup = ready_user(api, "sup", "support")
        assert api.get("/admin/v1/users", headers=dev).status_code == 403
        assert api.get("/admin/v1/licenses", headers=dev).status_code == 403
        assert api.get("/admin/v1/parsers", headers=dev).status_code == 200
        assert api.get("/admin/v1/parsers", headers=sup).status_code == 403
        assert api.get("/admin/v1/licenses", headers=sup).status_code == 200
        assert api.get("/admin/v1/audit", headers=sup).status_code == 403
        assert api.get("/admin/v1/overview", headers=dev).json()["revenue_this_month_minor"] is None

    def test_owner_guardrails(self, api):
        owner = ready_user(api, "olive", "owner")
        admin = ready_user(api, "adam", "admin")
        users = {u["username"]: u for u in api.get("/admin/v1/users", headers=H).json()["users"]}
        # admins can't touch owners or mint them
        assert api.patch(f"/admin/v1/users/{users['olive']['id']}", headers=admin, json={"active": False}).status_code == 403
        assert api.post("/admin/v1/users", headers=admin, json={"username": "boss", "role": "owner"}).status_code == 403
        # nobody changes their own role or disables themselves
        assert api.patch(f"/admin/v1/users/{users['adam']['id']}", headers=admin, json={"role": "support"}).status_code == 403
        # the last active owner can't be demoted
        assert api.patch(f"/admin/v1/users/{users['olive']['id']}", headers=H, json={"role": "admin"}).status_code == 409
        # disabling signs the user out at once
        assert api.patch(f"/admin/v1/users/{users['adam']['id']}", headers=owner, json={"active": False}).status_code == 200
        assert api.get("/admin/v1/overview", headers=admin).status_code in (401, 403)

    def test_reset_password_revokes_sessions(self, api):
        h = ready_user(api)
        uid = api.get("/admin/v1/auth/me", headers=h).json()["user"]["id"]
        temp = api.post(f"/admin/v1/users/{uid}/reset-password", headers=H).json()["temporary_password"]
        assert api.get("/admin/v1/overview", headers=h).status_code in (401, 403)
        assert api.get("/admin/v1/auth/me", headers=sign_in(api, "casey", temp)).json()["must_change_password"]

    def test_passwords_never_stored_or_logged(self, api, tmp_path, caplog):
        temp = create_user(api)
        sign_in(api, "casey", temp)
        raw = b"".join(p.read_bytes() for p in tmp_path.rglob("*.db*") if p.is_file())
        assert temp.encode() not in raw and b"$argon2id$" in raw
        assert temp not in caplog.text
        audit = json.dumps(api.get("/admin/v1/audit?limit=50", headers=H).json())
        assert temp not in audit and "user.create" in audit and "auth.login" in audit


# --- Parser Studio backend ----------------------------------------------------------------------------------


class TestParserStudio:
    def test_draft_publish_bundle_roundtrip(self, api):
        dev = ready_user(api)
        assert api.put("/admin/v1/parsers/acme-weekly", headers=dev, json={"schema": SCHEMA}).status_code == 200
        # apps never get bundles from the service: they download signed static files from the CDN
        assert api.get("/v1/parsers/bundle", headers={"Authorization": "Bearer " + "c" * 40}).status_code == 404
        assert api.post("/admin/v1/parsers/acme-weekly/publish", headers=dev).json()["version"] == 1
        # re-saving the published version is refused: fixes are new versions
        assert api.put("/admin/v1/parsers/acme-weekly", headers=dev, json={"schema": SCHEMA}).status_code == 422
        listed = api.get("/admin/v1/parsers", headers=dev).json()
        assert listed["signing"] and listed["schemas"][0]["published"]["version"] == 1
        bundle = api.get("/admin/v1/parsers/bundle", headers=dev).json()
        assert bundle["public_key"] == signing_public_hex()
        # the app verifies and re-validates what the server signed
        verified = verify_bundle(bundle["bundle"], [signing_public_hex()])
        assert [s.id for s in verified.schemas] == ["acme-weekly"]
        with pytest.raises(SchemaError, match="signed"):
            verify_bundle(bundle["bundle"], ["00" * 32])
        audit = json.dumps(api.get("/admin/v1/audit?limit=50", headers=H).json())
        assert "parser.publish" in audit

    def test_publishing_needs_the_permission(self, api):
        sup = ready_user(api, "sup", "support")
        assert api.put("/admin/v1/parsers/acme-weekly", headers=sup, json={"schema": SCHEMA}).status_code == 403

    @pytest.mark.parametrize("pattern", ["(a+)+", r"(\d+)\1", "[", "no-group", "x" * 301])
    def test_regex_rules_match_the_app(self, pattern):
        with pytest.raises(svc_parsers.ParserSchemaError):
            svc_parsers.check_pattern(pattern, "regex")
        with pytest.raises((ValueError, SchemaError)):
            safe_pattern(pattern)
            ParserSchema.from_dict({**SCHEMA, "rules": {**SCHEMA["rules"], "tips": {"columns": ["Tip Note"],
                                                                                   "regex": pattern}}})

    def test_server_rejects_what_the_app_would(self, api):
        for bad in ({**SCHEMA, "id": "Bad Id"}, {**SCHEMA, "rules": {"gross": {"columns": ["Fare"]}}},
                    {**SCHEMA, "rules": {**SCHEMA["rules"], "reference": {"columns": ["Trip ID"], "multiplier_bp": 5}}},
                    {**SCHEMA, "rules": {**SCHEMA["rules"], "tips": {"columns": ["Tip"], "regex": "(a+)+"}}}):
            assert api.put(f"/admin/v1/parsers/{bad['id']}", headers=H, json={"schema": bad}).status_code in (400, 422)

    def test_bundle_format_is_shared(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        pub = key.public_key().public_bytes_raw().hex()
        server = svc_parsers.sign_bundle([SCHEMA], key, issued_at="2026-09-27T00:00:00Z")
        app = dynamic.sign_bundle([ParserSchema.from_dict(SCHEMA)], key,
                                  issued_at=datetime(2026, 9, 27, tzinfo=timezone.utc))
        assert verify_bundle(server, [pub]).schemas == verify_bundle(app, [pub]).schemas


# --- app side: dynamic schemas, ingestion v2, install --------------------------------------------------------


class TestDynamicEngine:
    def test_test_parse_is_integer_cents(self):
        preview = run_parse(ParserSchema.from_dict(SCHEMA), SAMPLE)
        assert not preview.errors and len(preview.rows) == 2
        first = preview.rows[0]
        assert (first.gross_minor, first.tips_minor, first.fees_minor, first.tolls_minor) == (2450, 500, 613, 225)
        assert first.net_minor == 2450 + 500 - 613                   # tolls are pass-through
        assert first.paid_out_at.startswith("2026-09-08") and first.reference == "T-1001"
        assert all(isinstance(getattr(r, a), int) for r in preview.rows
                   for a in ("gross_minor", "tips_minor", "fees_minor", "tolls_minor", "net_minor"))
        assert preview.total("gross_minor") == 4250

    def test_missing_columns_are_reported(self):
        preview = run_parse(ParserSchema.from_dict(SCHEMA), "Date,Fare\n2026-09-01,1.00\n")
        assert preview.errors and "not in the sample" in preview.errors[0][1]

    def test_schema_version_marks_v2_profiles(self):
        v2 = ParserSchema.from_dict(SCHEMA).profile
        assert v2.uses_v2 and v2.to_dict()["schema_version"] == 2
        plain = MappingProfile.from_dict({"name": "Plain", "platform": "x", "date": {"columns": ["Date"]},
                                          "amounts": {"earning": {"columns": ["Fare"]}}})
        assert not plain.uses_v2 and plain.to_dict().get("schema_version", 1) == 1

    def test_install_refuses_downgrades(self, db):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        pub = [key.public_key().public_bytes_raw().hex()]
        if True:
            v2 = dynamic.sign_bundle([{**SCHEMA, "version": 2}], key)
            v1 = dynamic.sign_bundle([SCHEMA], key)
            assert dynamic.install_bundle(db, v2, pub).added == ("acme-weekly",)
            assert dynamic.install_bundle(db, v2, pub).unchanged == ("acme-weekly",)
            assert dynamic.install_bundle(db, v1, pub).refused_downgrades == ("acme-weekly",)
            installed = dynamic.installed_schemas(db)
            assert [s.version for s in installed] == [2]
            assert dynamic.detect(installed, ["Date", "Fare", "Tolls"]).id == "acme-weekly"
            from core.financial.ingestion import list_profiles

            assert any("Acme weekly" in name for name in list_profiles(db))

    def test_publish_to_cdn_and_install_in_the_app(self, tmp_path, monkeypatch, db):
        """Studio publish → signed static files → the app downloads, verifies and installs them."""
        from core.financial.parsers.sync import fetch_and_install, summary

        cdn = tmp_path / "cdn"
        monkeypatch.setenv("RS_AI_FORMATS_DIR", str(cdn))
        monkeypatch.setenv("RS_AI_FORMATS_PUBLIC_URL", "https://formats.iflowcore.test/v1/bundle.ifpb")
        with TestClient(make(tmp_path, RS_AI_LICENSE_SIGNING_KEY=SEED)) as c:
            info = c.get("/admin/v1/parsers/distribution", headers=H).json()
            assert info["configured"] and info["last"] is None and info["public_url"].endswith("bundle.ifpb")
            c.put("/admin/v1/parsers/acme-weekly", headers=H, json={"schema": SCHEMA})
            published = c.post("/admin/v1/parsers/acme-weekly/publish", headers=H).json()
            dist = published["distribution"]
            assert dist["schemas"] == 1 and len(dist["sha256"]) == 64
            assert c.get("/admin/v1/parsers/distribution", headers=H).json()["last"]["sha256"] == dist["sha256"]
            assert "parser.distribute" in json.dumps(c.get("/admin/v1/audit?limit=20", headers=H).json())
        files = sorted(str(p.relative_to(cdn)) for p in cdn.rglob("*") if p.is_file())
        assert files[1:] == ["v1/bundle.ifpb", "v1/latest.json"] and files[0].startswith("v1/archive/")
        latest = json.loads((cdn / "v1/latest.json").read_text())
        assert latest["sha256"] == dist["sha256"] and latest["schemas"] == 1

        cdn_transport = StaticCdn(cdn)
        url = "https://formats.iflowcore.test/v1/bundle.ifpb"
        first = fetch_and_install(db, url, transport=cdn_transport, keys=[signing_public_hex()])
        assert first.report.added == ("acme-weekly",) and "1 new" in summary(first.report)
        again = fetch_and_install(db, url, transport=cdn_transport, etag=first.etag, keys=[signing_public_hex()])
        assert again.report is None and cdn_transport.last_status == 304          # unchanged: nothing downloaded
        with pytest.raises(SchemaError):                                          # untrusted signer
            fetch_and_install(db, url, transport=cdn_transport, keys=["00" * 32])
        tampered = (cdn / "v1/bundle.ifpb").read_text()
        head, payload, sig = tampered.split(".")
        (cdn / "v1/bundle.ifpb").write_text(f"{head}.{payload[:-4]}AAAA.{sig}")     # a compromised bucket
        with pytest.raises(SchemaError):
            fetch_and_install(db, url, transport=cdn_transport, keys=[signing_public_hex()])

    def test_distribution_without_a_target(self, api):
        api.put("/admin/v1/parsers/acme-weekly", headers=H, json={"schema": SCHEMA})
        out = api.post("/admin/v1/parsers/acme-weekly/publish", headers=H).json()
        assert out["version"] == 1 and "distribution" not in out            # published; nothing to upload to
        assert api.post("/admin/v1/parsers/distribute", headers=H).status_code == 503

    def test_vendor_parsers_never_sync(self):
        from core.sync.engine import LOCAL_ONLY_TABLES

        assert "vendor_parsers" in LOCAL_ONLY_TABLES


class StaticCdn:
    """A CDN in front of a folder: GET with ETag / If-None-Match, like CloudFront."""

    def __init__(self, root):
        self.root, self.last_status = root, None

    def request(self, method, url, *, params=None, form=None, headers=None, body=None, timeout=15.0):
        import hashlib

        from core.fleet.http import HttpResponse

        path = self.root / url.split("/", 3)[3]
        if not path.is_file():
            self.last_status = 404
            return HttpResponse(404, b"", {})
        data = path.read_bytes()
        etag = '"' + hashlib.md5(data).hexdigest() + '"'
        if (headers or {}).get("If-None-Match") == etag:
            self.last_status = 304
            return HttpResponse(304, b"", {"ETag": etag})
        self.last_status = 200
        return HttpResponse(200, data, {"ETag": etag, "Content-Type": "text/plain; charset=utf-8"})


# --- the console UI ------------------------------------------------------------------------------------------


def console(api, width=1400):
    from tests.ui_helpers import FakePage
    from ui.admin_console.app import AdminConsoleApp

    page = FakePage(width)
    app = AdminConsoleApp(page, env={"IFLOW_ADMIN_URL": URL}, services={"admin_transport": ServiceTransport(api)})
    app.start()
    return app, page


def login(app, page, username, password):
    from tests.ui_helpers import find_key

    view = page.views[-1]
    find_key(view, "console-username").value = username
    find_key(view, "console-password").value = password
    find_key(view, "console-sign-in").on_click(None)
    return page.views[-1]


class TestConsoleUI:
    def test_login_forced_password_change_and_shell(self, api):
        from tests.ui_helpers import find_key, texts

        temp = create_user(api, "casey", "owner")
        app, page = console(api)
        view = page.views[-1]
        assert find_key(view, "console-login") is not None and find_key(view, "console-url").value == URL
        view = login(app, page, "casey", "wrong-password")
        assert "incorrect" in " ".join(texts(find_key(view, "console-login-error")))
        assert find_key(view, "console-username").value == "casey"           # kept for the retry
        view = login(app, page, "casey", temp)
        assert find_key(view, "console-password-change") is not None and find_key(view, "console-rail") is None
        find_key(view, "console-pw-current").value = temp
        find_key(view, "console-pw-new").value = GOOD_PASSWORD
        find_key(view, "console-pw-confirm").value = "different-" + GOOD_PASSWORD
        find_key(view, "console-pw-submit").on_click(None)
        view = page.views[-1]
        assert "don't match" in " ".join(texts(find_key(view, "console-pw-error")))
        find_key(view, "console-pw-current").value = temp
        find_key(view, "console-pw-new").value = GOOD_PASSWORD
        find_key(view, "console-pw-confirm").value = GOOD_PASSWORD
        find_key(view, "console-pw-submit").on_click(None)
        view = page.views[-1]
        rail = find_key(view, "console-rail")
        assert rail is not None and len(rail.destinations) == 8                  # owners see every page
        assert find_key(view, "admin-overview") is not None
        from .test_ui_app import _wrap_expand_violations

        for page_id in ("users", "licenses", "parsers", "telemetry", "health", "audit", "account"):
            app.go(page_id)
            assert find_key(page.views[-1], f"console-page-{page_id}") is not None, page_id
            assert _wrap_expand_violations(page.views[-1]) == [], page_id
        find_key(page.views[-1], "console-sign-out").on_click(None)
        assert find_key(page.views[-1], "console-login") is not None and "admin_session" not in app.services

    def test_rail_follows_the_role(self, api):
        from tests.ui_helpers import find_key

        ready_user(api, "dev", "developer")
        app, page = console(api)
        view = login(app, page, "dev", GOOD_PASSWORD)
        labels = [d.label for d in find_key(view, "console-rail").destinations]
        assert labels == ["Overview", "Parser Studio", "Crash reports", "System health", "My account"]
        assert find_key(view, "admin-kpi-revenue") is None
        app.go("users")                                          # not offered, and not reachable either
        assert app.current == "overview"

    def test_users_page(self, api):
        from tests.ui_helpers import find_key

        ready_user(api, "olive", "owner")
        app, page = console(api)
        login(app, page, "olive", GOOD_PASSWORD)
        app.go("users")
        view = page.views[-1]
        assert find_key(view, "users-roles-matrix") is not None
        assert find_key(view, "users-role-olive").disabled                    # not your own role
        assert find_key(view, "users-toggle-olive") is None                   # nor disable yourself
        find_key(view, "users-new-username").value = "Sam"
        find_key(view, "users-new-name").value = "Sam Support"
        find_key(view, "users-new-role").value = "support"
        find_key(view, "users-create").on_click(None)
        view = page.views[-1]
        temp = find_key(view, "users-temp-password").value
        assert len(temp) >= 16 and find_key(view, "users-role-sam") is not None
        # the temporary password works once, then must be changed
        assert api.post("/admin/v1/auth/login", json={"username": "sam", "password": temp}).json()["user"][
            "must_change_password"]
        # disable, with confirmation
        find_key(view, "users-toggle-sam").on_click(None)
        dialog = page.dialogs[-1]
        find_key(dialog, "users-disable-confirm").on_click(None)
        assert api.post("/admin/v1/auth/login", json={"username": "sam", "password": temp}).status_code == 401
        # role change straight from the table
        role = find_key(page.views[-1], "users-role-sam")
        role.value = "developer"
        role.on_select(type("E", (), {"control": role})())
        users = {u["username"]: u for u in api.get("/admin/v1/users", headers=H).json()["users"]}
        assert users["sam"]["role"] == "developer" and not users["sam"]["active"]
        # reset password → a new temporary one, shown once
        find_key(page.views[-1], "users-reset-sam").on_click(None)
        find_key(page.dialogs[-1], "users-reset-confirm").on_click(None)
        assert find_key(page.views[-1], "users-temp-password").value not in ("", temp)

    def test_parser_studio_flow(self, api):
        from tests.ui_helpers import find_key, texts

        ready_user(api)                                          # developer: edit + publish
        app, page = console(api)
        login(app, page, "casey", GOOD_PASSWORD)
        app.go("parsers")
        view = page.views[-1]
        for key, value in (("studio-id", "acme-weekly"), ("studio-name", "Acme weekly"), ("studio-platform", "Acme"),
                           ("studio-timezone", "UTC"), ("studio-sample", SAMPLE)):
            find_key(view, key).value = value
        find_key(view, "studio-read-headers").on_click(None)
        view = page.views[-1]
        assert [o.key for o in find_key(view, "studio-col-gross").options][1:] == [
            "Date", "Trip ID", "Fare", "Tip Note", "Service Fee", "Tolls", "Paid On"]
        assert find_key(view, "studio-name").value == "Acme weekly"            # edits survive the rebuild
        for role, col in (("occurred_at", "Date"), ("gross", "Fare"), ("tips", "Tip Note"), ("fees", "Service Fee"),
                          ("tolls", "Tolls"), ("paid_out", "Paid On"), ("reference", "Trip ID")):
            find_key(view, f"studio-col-{role}").value = col
        # plain-language controls: no regex or basis points needed
        pattern = find_key(view, "studio-pattern-tips")
        pattern.value = "after-label"
        pattern.on_select(None)                                             # shows the label field
        view = page.views[-1]
        assert find_key(view, "studio-label-tips").visible and not find_key(view, "studio-regex-tips").visible
        find_key(view, "studio-label-tips").value = "Tip"
        find_key(view, "studio-amount-fees").value = "flip"
        find_key(view, "studio-test").on_click(None)
        view = page.views[-1]
        shown = "\n".join(texts(view))
        assert "Example from your sample: \u201cTip: $5.00\u201d reads as $5.00" in shown
        assert "Example from your sample: \u201c-6.13\u201d reads as $6.13" in shown
        view = page.views[-1]
        cells = texts(find_key(view, "studio-preview"))
        assert "$24.50" in cells and "$5.00" in cells and "$6.13" in cells and "$23.37" in cells
        # nothing was sent: the server has no drafts yet
        assert api.get("/admin/v1/parsers", headers=H).json()["schemas"] == []
        find_key(view, "studio-save").on_click(None)
        view = page.views[-1]
        assert find_key(view, "studio-notice") is not None and not find_key(view, "studio-publish").disabled
        find_key(view, "studio-publish").on_click(None)
        find_key(page.dialogs[-1], "studio-publish-confirm").on_click(None)
        view = page.views[-1]
        assert "Published acme-weekly v1" in " ".join(texts(find_key(view, "studio-notice")))
        assert find_key(view, "studio-version").value == "2"                 # further edits are the next draft
        find_key(view, "studio-export").on_click(None)
        bundle = find_key(page.views[-1], "studio-bundle").value
        assert verify_bundle(bundle, [signing_public_hex()]).schemas[0].version == 1

    def test_studio_presets_sample_file_and_cdn(self, tmp_path, monkeypatch):
        from tests.ui_helpers import find_key, texts

        cdn = tmp_path / "cdn"
        monkeypatch.setenv("RS_AI_FORMATS_DIR", str(cdn))
        with TestClient(make(tmp_path, RS_AI_LICENSE_SIGNING_KEY=SEED)) as api:
            ready_user(api)
            app, page = console(api)
            login(app, page, "casey", GOOD_PASSWORD)
            app.go("parsers")
            view = page.views[-1]
            assert find_key(view, "studio-cdn-empty") is not None                     # CDN configured, empty
            sample = tmp_path / "statement.csv"
            sample.write_text(SAMPLE)
            app.services["studio_open_sample"](str(sample))                              # "Load sample file…"
            view = page.views[-1]
            assert find_key(view, "studio-sample").value == SAMPLE
            assert "statement.csv" in " ".join(texts(find_key(view, "studio-notice")))
            for key, value in (("studio-id", "acme-weekly"), ("studio-name", "Acme weekly"),
                               ("studio-platform", "Acme"), ("studio-timezone", "UTC")):
                find_key(view, key).value = value
            for role, col in (("occurred_at", "Date"), ("gross", "Fare"), ("tips", "Tip Note"),
                              ("fees", "Service Fee"), ("tolls", "Tolls")):
                find_key(view, f"studio-col-{role}").value = col
            # half-finished choices are kept, and only flagged when testing
            find_key(view, "studio-pattern-tips").value = "after-label"
            amount = find_key(view, "studio-amount-tolls")
            amount.value = "percent"
            amount.on_select(None)
            view = page.views[-1]
            assert find_key(view, "studio-error") is None and find_key(view, "studio-percent-tolls").visible
            assert find_key(view, "studio-pattern-tips").value == "after-label"
            find_key(view, "studio-test").on_click(None)
            view = page.views[-1]
            assert "type the label" in " ".join(texts(find_key(view, "studio-error")))
            find_key(view, "studio-label-tips").value = "Tip"
            find_key(view, "studio-percent-tolls").value = "50"
            find_key(view, "studio-amount-fees").value = "flip"
            find_key(view, "studio-test").on_click(None)
            view = page.views[-1]
            shown = "\n".join(texts(view))
            assert "\u201c2.25\u201d reads as $1.13" in shown                       # 50 % of $2.25, half-up
            cells = texts(find_key(view, "studio-preview"))
            assert "$1.13" in cells and "$5.00" in cells
            find_key(view, "studio-save").on_click(None)
            saved = api.get("/admin/v1/parsers/acme-weekly", headers=H).json()["versions"][0]["schema"]
            assert saved["rules"]["tolls"]["multiplier_bp"] == 5000 and "ui_pattern" not in json.dumps(saved)
            assert saved["rules"]["tips"]["regex"].startswith("Tip")
            find_key(page.views[-1], "studio-publish").on_click(None)
            find_key(page.dialogs[-1], "studio-publish-confirm").on_click(None)
            view = page.views[-1]
            assert "sent it to the CDN" in " ".join(texts(find_key(view, "studio-notice")))
            assert find_key(view, "studio-cdn-live") is not None and (cdn / "v1/bundle.ifpb").is_file()
            first = (cdn / "v1/latest.json").read_text()
            find_key(view, "studio-distribute").on_click(None)                        # re-send
            assert "Sent 1 format(s) to the CDN" in " ".join(texts(find_key(page.views[-1], "studio-notice")))
            assert len(list((cdn / "v1/archive").iterdir())) >= 1 and (cdn / "v1/latest.json").read_text()
            assert json.loads(first)["schemas"] == 1

    def test_studio_validation_errors_are_shown(self, api):
        from tests.ui_helpers import find_key

        app, page = console(api)
        find_key(page.views[-1], "console-toggle-mode").on_click(None)
        view = page.views[-1]
        find_key(view, "console-token").value = ADMIN
        find_key(view, "console-sign-in").on_click(None)
        app.go("parsers")
        view = page.views[-1]
        find_key(view, "studio-id").value = "x"
        find_key(view, "studio-test").on_click(None)
        assert find_key(page.views[-1], "studio-error") is not None

    def test_session_expiry_returns_to_login(self, api):
        from tests.ui_helpers import find_key, texts

        h = ready_user(api)
        app, page = console(api)
        login(app, page, "casey", GOOD_PASSWORD)
        uid = api.get("/admin/v1/auth/me", headers=h).json()["user"]["id"]
        api.post(f"/admin/v1/users/{uid}/reset-password", headers=H)          # revokes every session
        app.reload()
        view = page.views[-1]
        assert find_key(view, "console-login") is not None and "admin_session" not in app.services
        assert "Sign in again" in " ".join(texts(view))

    def test_console_never_touches_user_data(self, api):
        import ui.admin_console.app as console_mod

        src = open(console_mod.__file__).read()
        assert "AppState" not in src.replace("there's no AppState", "") and "open_database" not in src
        app, _ = console(api)
        assert app.context().state is None
