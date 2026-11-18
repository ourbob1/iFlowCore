"""Phase 10.5: "Sign in to iFlow" in Settings, end to end against the real account API (in-process).

Two app instances play two devices on one account: create account → email code → sync passphrase
(recovery key shown once) → the second device unlocks with the same passphrase and pulls the first
device's records. Also: wrong passphrase, recovery with the recovery key, sign-out, account
deletion, the sync chip, and that nothing sensitive is kept in plain settings."""

from __future__ import annotations

import json
import uuid

import pytest

from tests.ui_helpers import find_key, texts

from .test_account_api import PASSWORD, Harness
from .test_account_client import API, AppTransport
from .test_ui_app import make_app

PASSPHRASE = "orbit canyon marble tulip"


@pytest.fixture
def server():
    h = Harness("sqlite:///" + str(__import__("tempfile").mkdtemp()) + "/api.db")
    yield h
    h.app.state.engine.dispose()


def device(tmp_path, server, name):
    app, page = make_app(tmp_path / name, services={"account_transport": AppTransport(server),
                                                    "account_server": API})
    app.start()
    app.navigate("/settings")
    return app, page


def card(page):
    return find_key(page.views[-1], "account-card")


def click(page, key):
    find_key(page.views[-1], key).on_click(None)


def fill(page, **values):
    for key, value in values.items():
        find_key(page.views[-1], f"account-{key}").value = value


def shown(page) -> str:
    return "\n".join(texts(card(page)))


def create_account(server, page, email="ann@example.com"):
    click(page, "account-to-signup")
    fill(page, email=email, password=PASSWORD, confirm=PASSWORD)
    click(page, "account-signup")
    assert find_key(page.views[-1], "account-code") is not None
    fill(page, code=server.last_code(email))
    click(page, "account-verify")


class TestSignInToIflow:
    def test_first_device_end_to_end(self, tmp_path, server):
        app, page = device(tmp_path, server, "phone")
        assert find_key(page.views[-1], "account-signin") is not None
        assert find_key(page.views[-1], "sync-chip") is not None
        assert "Sync off" in texts(find_key(page.views[-1], "sync-chip"))
        assert find_key(page.views[-1], "ai-url") is None                        # the old paste-a-URL card is tucked away

        create_account(server, page)
        assert "Signed in as ann@example.com" in shown(page)
        fill(page, passphrase="short", **{"passphrase-confirm": "short"})
        click(page, "account-sync-on")
        assert "at least 10" in shown(page)
        fill(page, passphrase=PASSPHRASE, **{"passphrase-confirm": PASSPHRASE + "x"})
        click(page, "account-sync-on")
        assert "don't match" in shown(page)
        fill(page, passphrase=PASSPHRASE, **{"passphrase-confirm": PASSPHRASE})
        click(page, "account-sync-on")
        key = find_key(page.views[-1], "account-recovery-key").value
        assert len(key.replace("-", "")) >= 40
        assert find_key(page.views[-1], "account-sync-now") is not None
        chip = " ".join(texts(find_key(page.views[-1], "sync-chip")))
        assert chip in ("Synced",) or chip.endswith("to sync")
        click(page, "account-recovery-saved")
        assert find_key(page.views[-1], "account-recovery-key") is None            # shown once
        # nothing sensitive in plain settings: the session and keys are sealed/encrypted fields
        raw = json.dumps(app.state.settings.account_session or {})
        assert PASSWORD not in raw and PASSPHRASE not in raw and key not in raw
        assert PASSPHRASE not in json.dumps(app.state.settings.sync_keys or {})

    def test_second_device_pulls_the_first_devices_data(self, tmp_path, server):
        phone, pp = device(tmp_path, server, "phone")
        create_account(server, pp)
        fill(pp, passphrase=PASSPHRASE, **{"passphrase-confirm": PASSPHRASE})
        click(pp, "account-sync-on")
        with phone.state.db.transaction() as conn:
            conn.execute("INSERT INTO platforms (id, name, kind) VALUES (?, 'Instacart', 'delivery')",
                         (str(uuid.uuid4()),))
        phone.navigate("/settings")
        click(pp, "account-sync-now")
        assert "Synced: sent" in shown(pp)

        laptop, lp = device(tmp_path, server, "laptop")
        fill(lp, email="ann@example.com", password=PASSWORD)
        click(lp, "account-signin")
        fill(lp, passphrase="not the passphrase", **{"passphrase-confirm": "not the passphrase"})
        click(lp, "account-sync-on")
        assert "isn't this account's sync passphrase" in shown(lp)
        fill(lp, passphrase=PASSPHRASE, **{"passphrase-confirm": PASSPHRASE})
        click(lp, "account-sync-on")
        assert find_key(lp.views[-1], "account-recovery-key") is None              # not the first device
        with laptop.state.db.connection() as conn:
            names = [r[0] for r in conn.execute("SELECT name FROM platforms WHERE deleted_at IS NULL")]
        assert "Instacart" in names
        devices = "\n".join(texts(find_key(lp.views[-1], "account-card")))
        assert "(this device)" in devices and devices.count("Linux desktop") == 2

    def test_recovery_key_on_a_new_device(self, tmp_path, server):
        phone, pp = device(tmp_path, server, "phone")
        create_account(server, pp)
        fill(pp, passphrase=PASSPHRASE, **{"passphrase-confirm": PASSPHRASE})
        click(pp, "account-sync-on")
        recovery = find_key(pp.views[-1], "account-recovery-key").value

        laptop, lp = device(tmp_path, server, "laptop")
        fill(lp, email="ann@example.com", password=PASSWORD)
        click(lp, "account-signin")
        click(lp, "account-to-recover")
        new = "harbor willow ember quartz"
        fill(lp, recovery=recovery, passphrase=new, **{"passphrase-confirm": new})
        click(lp, "account-recover")
        assert "new passphrase" in shown(lp) and find_key(lp.views[-1], "account-sync-now") is not None

    def test_sign_out_and_delete(self, tmp_path, server):
        app, page = device(tmp_path, server, "phone")
        create_account(server, page)
        fill(page, passphrase=PASSPHRASE, **{"passphrase-confirm": PASSPHRASE})
        click(page, "account-sync-on")
        click(page, "account-signout")
        assert "Signed out" in shown(page) and find_key(page.views[-1], "account-signin") is not None
        assert app.state.settings.sync_keys is None and app.state.settings.account_session is None

        fill(page, email="ann@example.com", password=PASSWORD)
        click(page, "account-signin")
        fill(page, passphrase=PASSPHRASE, **{"passphrase-confirm": PASSPHRASE})
        click(page, "account-sync-on")
        click(page, "account-delete")
        find_key(page.dialogs[-1], "account-delete-confirm").on_click(None)
        assert "deleted" in shown(page)
        assert server.table_counts()["users"] == 0

    def test_wrong_password_and_unverified(self, tmp_path, server):
        app, page = device(tmp_path, server, "phone")
        click(page, "account-to-signup")
        fill(page, email="bo@example.com", password=PASSWORD, confirm=PASSWORD)
        click(page, "account-signup")
        click(page, "account-back")
        fill(page, email="bo@example.com", password=PASSWORD)
        click(page, "account-signin")                                             # not verified yet → code step
        assert find_key(page.views[-1], "account-code") is not None
        click(page, "account-back")
        fill(page, email="bo@example.com", password="wrong-password-123")
        click(page, "account-signin")
        assert "incorrect" in shown(page)

    def test_own_server_must_be_https(self, tmp_path, server):
        app, page = device(tmp_path, server, "phone")
        fill(page, server="http://sync.example.com")
        click(page, "account-set-server")
        assert "HTTPS" in shown(page)

    def test_default_server_is_iflow_hosted(self, tmp_path):
        from core.config import DEFAULT_ACCOUNT_API_URL
        from ui.viewmodels import account

        app, _ = make_app(tmp_path)
        app.start()
        assert account.server_url(app.state, app.services) == DEFAULT_ACCOUNT_API_URL == "https://api.iflowcore.com"
