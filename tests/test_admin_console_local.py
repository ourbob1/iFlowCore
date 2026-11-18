"""Phase 11: the Admin Console runs its own service ("This computer"), so it opens like a normal app.

A real uvicorn server on a free loopback port and a temporary data folder: first-run owner setup,
sign-in, the signing key (0600, public half only on screen), CDN publishing settings surviving a
service restart, port conflicts, and publishing a format from the built-in service to a folder."""

from __future__ import annotations

import json
import socket
import stat

import pytest

from .test_ai_service import ADMIN  # noqa: F401  (puts services/ai on sys.path)

PASSWORD = "Harbor-lantern-93!"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def local(tmp_path):
    from ui.admin_console.local_service import LocalService

    svc = LocalService(tmp_path / "iflow-ai", port=free_port())
    yield svc
    svc.stop()


def console(local, width=1400):
    from tests.ui_helpers import FakePage
    from ui.admin_console.app import AdminConsoleApp

    page = FakePage(width)
    app = AdminConsoleApp(page, env={}, services={"local_service": local})
    app.start()
    return app, page


def fill(view, **values):
    from tests.ui_helpers import find_key

    for key, value in values.items():
        find_key(view, key).value = value


class TestFirstRun:
    def test_setup_signs_in_and_creates_the_key(self, local):
        from tests.ui_helpers import find_key, texts

        app, page = console(local)
        assert app.services["local_status"] == "ready" and local.healthy()
        view = page.views[-1]
        assert find_key(view, "console-setup") is not None                     # no accounts yet
        fill(view, **{"setup-username": "olive", "setup-name": "Olive Owner", "setup-password": "short",
                      "setup-confirm": "short"})
        find_key(view, "setup-submit").on_click(None)
        view = page.views[-1]
        assert find_key(view, "setup-error") is not None           # weak password refused
        fill(view, **{"setup-username": "olive", "setup-name": "Olive Owner", "setup-password": PASSWORD,
                      "setup-confirm": PASSWORD})
        find_key(view, "setup-submit").on_click(None)
        view = page.views[-1]
        assert find_key(view, "console-rail") is not None                        # straight in: it's their password
        assert "This computer" in " ".join(texts(find_key(view, "console-topbar")))
        mode = stat.S_IMODE(local.key_path.stat().st_mode)
        assert mode == 0o600
        app.go("account")
        view = page.views[-1]
        shown = " ".join(texts(view))
        assert local.public_key_hex() in shown
        assert local.key_path.read_text().strip() not in shown                    # never the private key
        assert find_key(view, "account-signing-status") is not None

    def test_existing_accounts_go_to_sign_in(self, local):
        from tests.ui_helpers import find_key

        local.create_first_owner("olive", "Olive", PASSWORD, PASSWORD)
        app, page = console(local)
        view = page.views[-1]
        assert find_key(view, "console-setup") is None and find_key(view, "console-url").visible is False
        fill(view, **{"console-username": "olive", "console-password": PASSWORD})
        find_key(view, "console-sign-in").on_click(None)
        assert find_key(page.views[-1], "console-rail") is not None

    def test_setup_only_while_empty(self, local):
        from ui.admin_console.local_service import LocalServiceError

        local.create_first_owner("olive", "Olive", PASSWORD, PASSWORD)
        with pytest.raises(LocalServiceError, match="already has"):
            local.create_first_owner("mallory", "M", PASSWORD, PASSWORD)
        with pytest.raises(LocalServiceError, match="match"):
            local.create_first_owner("x", "x", PASSWORD, PASSWORD + "!")

    def test_the_old_temporary_password_flow_still_works(self, local):
        """Accounts made with the bootstrap command (temporary password) still get the forced change."""
        from iflow_ai.admin_users import AdminUsers
        from tests.ui_helpers import find_key

        local.data_dir.mkdir(parents=True)
        users = AdminUsers(local.data_dir / "admin.db")
        users.create("casey", "Casey", "owner", created_by="bootstrap", temp_password="temp-pass-1")
        users.close()
        app, page = console(local)
        fill(page.views[-1], **{"console-username": "casey", "console-password": "temp-pass-1"})
        find_key(page.views[-1], "console-sign-in").on_click(None)
        assert find_key(page.views[-1], "console-password-change") is not None


class TestService:
    def test_port_in_use(self, tmp_path):
        from tests.ui_helpers import find_key
        from ui.admin_console.local_service import LocalService

        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen()
            port = blocker.getsockname()[1]
            svc = LocalService(tmp_path, port=port)
            app, page = console(svc)
            view = page.views[-1]
            assert find_key(view, "console-local-error") is not None
            assert find_key(view, "console-sign-in").disabled
        # the port is free again: "Try again" starts it
        find_key(page.views[-1], "console-local-retry").on_click(None)
        assert app.services["local_status"] == "ready" and find_key(page.views[-1], "console-setup") is not None
        svc.stop()

    def test_reuses_a_running_service(self, local):
        local.start()
        from ui.admin_console.local_service import LocalService

        second = LocalService(local.data_dir, port=local.port)
        assert second.start() == local.url and second._thread is None          # didn't start a second server

    def test_loopback_only(self, local):
        local.start()
        assert local._server.config.host == "127.0.0.1"

    def test_switching_to_another_server(self, local):
        from tests.ui_helpers import find_key

        app, page = console(local)
        app.choose_server("remote")
        view = page.views[-1]
        assert find_key(view, "console-url").visible and find_key(view, "console-setup") is None


class TestPublishing:
    def test_settings_survive_a_restart_and_keep_you_signed_in(self, local):
        from tests.ui_helpers import find_key

        local.create_first_owner("olive", "Olive", PASSWORD, PASSWORD)
        app, page = console(local)
        fill(page.views[-1], **{"console-username": "olive", "console-password": PASSWORD})
        find_key(page.views[-1], "console-sign-in").on_click(None)
        app.go("account")
        view = page.views[-1]
        fill(view, **{"publish-bucket": "iflowcoreformats-bundles-abc", "publish-distribution": "E2ABC",
                      "publish-profile": "iflow"})
        find_key(view, "publish-save").on_click(None)
        saved = json.loads((local.data_dir / "console.json").read_text())
        assert saved["formats_bucket"] == "iflowcoreformats-bundles-abc" and saved["aws_profile"] == "iflow"
        assert "secret" not in json.dumps(saved).lower()
        app.go("parsers")                                                      # still signed in after the restart
        data = app.session.data["parsers"]
        assert data["distribution"]["configured"] and data["distribution"]["target"].startswith("s3://")

    def test_publish_from_the_built_in_service(self, local, tmp_path, monkeypatch):
        """Save → publish in the built-in service → signed files in the CDN folder, verifiable with its key."""
        from core.admin.client import AdminClient
        from core.financial.parsers.dynamic import verify_bundle
        from .test_admin_console import SCHEMA

        monkeypatch.setenv("IFLOW_ADMIN_FORMATS_DIR", str(tmp_path / "cdn"))
        local.create_first_owner("olive", "Olive", PASSWORD, PASSWORD)
        local.start()
        client, _ = AdminClient.login(local.url, "olive", PASSWORD)
        client.save_parser(SCHEMA)
        out = client.publish_parser("acme-weekly")
        assert out["distribution"]["schemas"] == 1
        bundle = (tmp_path / "cdn" / "v1" / "bundle.ifpb").read_text()
        assert verify_bundle(bundle, [local.public_key_hex()]).schemas[0].id == "acme-weekly"
