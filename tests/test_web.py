"""Web build (browser/Pyodide): encrypted vault, vault-backed unlock, XHR transport, web packaging.

The browser-only pieces (OPFS handles, js.XMLHttpRequest) are exercised through the same code
paths with in-memory stand-ins; the real browser run is documented in the README.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from core.fleet.http import HttpResponse, ProviderError, XhrTransport, default_transport, in_browser
from core.web.session import WebVaultSession
from core.web.vault import MemoryStorage, Vault, VaultError, WrongPassphrase, _Header
from ui.state import UnlockError, Unlocker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import package_web  # noqa: E402

PASS = "web vault passphrase"


# --- vault ----------------------------------------------------------------------------------------------


class TestVault:
    def test_roundtrip_and_wrong_passphrase(self):
        storage = MemoryStorage()
        v = Vault(storage)
        assert not v.exists()
        v.create(PASS, {"iflowcore.db": b"SQLite format 3\0" + b"x" * 5000, "preferences.json": b"{}"})
        assert v.exists()
        files = Vault(storage).unlock(PASS)
        assert files["iflowcore.db"].startswith(b"SQLite format 3") and files["preferences.json"] == b"{}"
        with pytest.raises(WrongPassphrase):
            Vault(storage).unlock("some other passphrase")

    def test_nothing_readable_at_rest(self):
        storage = MemoryStorage()
        Vault(storage).create(PASS, {"iflowcore.db": b"SQLite format 3\0 Web Camry 42000 Uber"})
        blob = b"".join(storage.slots.values())
        assert b"Camry" not in blob and b"SQLite" not in blob and b"Uber" not in blob
        assert blob.startswith(b"IFCV")

    def test_alternating_slots_survive_a_torn_write(self):
        storage = MemoryStorage()
        v = Vault(storage)
        v.create(PASS, {"f": b"gen1"})
        v.save({"f": b"gen2"})
        v.save({"f": b"gen3"})
        assert v.generation == 3 and set(storage.slots) == {0, 1}
        newest = 3 % 2
        storage.slots[newest] = storage.slots[newest][:40]          # tab closed mid-write
        assert Vault(storage).unlock(PASS) == {"f": b"gen2"}          # previous good copy
        storage.slots[newest] = b"\0" * 200                         # garbage
        assert Vault(storage).unlock(PASS) == {"f": b"gen2"}

    def test_tampering_is_detected(self):
        storage = MemoryStorage()
        Vault(storage).create(PASS, {"f": b"data"})
        blob = bytearray(storage.slots[1])
        blob[-5] ^= 1
        storage.slots[1] = bytes(blob)
        with pytest.raises(WrongPassphrase):
            Vault(storage).unlock(PASS)

    def test_header_is_authenticated(self):
        storage = MemoryStorage()
        v = Vault(storage)
        v.create(PASS, {"f": b"data"})
        blob = bytearray(storage.slots[1])
        blob[20] ^= 1                                               # inside the header (AAD)
        storage.slots[1] = bytes(blob)
        with pytest.raises(WrongPassphrase):
            Vault(storage).unlock(PASS)

    def test_hostile_kdf_parameters_are_ignored(self):
        h = _Header(t=50, m=4 * 1024 * 1024, p=1, salt=b"s" * 16, generation=1, nonce=b"n" * 12)
        assert _Header.parse(h.pack() + b"x" * 32) is None

    def test_change_passphrase_rewrites_both_slots(self):
        storage = MemoryStorage()
        v = Vault(storage)
        v.create(PASS, {"f": b"one"})
        v.save({"f": b"two"})
        v.change_passphrase("brand new passphrase", {"f": b"two"})
        with pytest.raises(WrongPassphrase):
            Vault(storage).unlock(PASS)
        assert Vault(storage).unlock("brand new passphrase") == {"f": b"two"}

    def test_short_passphrase_and_locked_saves(self):
        v = Vault(MemoryStorage())
        with pytest.raises(VaultError):
            v.create("short", {})
        with pytest.raises(VaultError):
            v.save({})


# --- session + unlock ------------------------------------------------------------------------------------


@pytest.fixture
def web_config(tmp_path):
    from core.config import load_config

    return load_config({"IFLOW_DATA_DIR": str(tmp_path), "IFLOW_ENV": "production", "IFLOW_PLATFORM": "web"})


class TestWebUnlock:
    def test_config_and_policy(self, web_config):
        from core.security import resolve_storage_policy

        assert web_config.is_web and not web_config.requires_sqlcipher
        policy = resolve_storage_policy("web", require_encryption=True, sqlcipher_available=False, have_key=True)
        assert policy.mode == "vault"

    def test_create_use_reopen(self, web_config):
        storage = MemoryStorage()
        session = WebVaultSession(Vault(storage), web_config.db_path, web_config.preferences_path)
        unlocker = Unlocker(web_config, env={}, vault=session)
        assert unlocker.plan().mode == "create"
        with pytest.raises(UnlockError, match="don't match"):
            unlocker.open(PASS, confirm=PASS + "x")
        db = unlocker.open(PASS, confirm=PASS)
        assert db.protection == "vault" and not db.encrypted
        saves = session.saves
        with db.transaction() as conn:
            conn.execute("INSERT INTO platforms (id, name, kind) VALUES "
                         "('11111111-1111-4111-8111-111111111111', 'Web Uber', 'rideshare')")
        assert session.saves == saves + 1                            # saved right after the commit
        web_config.preferences_path.write_text('{"unit_system": "metric"}')
        assert session.save_if_changed() and not session.save_if_changed()   # prefs picked up; no-op after
        db.close()
        session.lock()
        assert not web_config.db_path.exists()                       # plaintext copy wiped on lock

        # page reload: new session over the same browser storage
        again = WebVaultSession(Vault(storage), web_config.db_path, web_config.preferences_path)
        unlocker2 = Unlocker(web_config, env={}, vault=again)
        assert unlocker2.plan().mode == "prompt"
        with pytest.raises(UnlockError, match="didn't unlock"):
            unlocker2.open("wrong wrong wrong")
        db2 = unlocker2.open(PASS)
        with db2.connection() as conn:
            assert conn.execute("SELECT name FROM platforms").fetchone()[0] == "Web Uber"
        assert json.loads(web_config.preferences_path.read_text()) == {"unit_system": "metric"}
        db2.close()

    def test_lockout_applies(self, web_config):
        storage = MemoryStorage()
        session = WebVaultSession(Vault(storage), web_config.db_path, web_config.preferences_path)
        Unlocker(web_config, env={}, vault=session).open(PASS, confirm=PASS).close()
        t = [1000.0]
        u = Unlocker(web_config, env={}, vault=WebVaultSession(Vault(storage), web_config.db_path,
                                                                web_config.preferences_path), clock=lambda: t[0])
        from ui.state import LOCKOUT_AFTER

        for _ in range(LOCKOUT_AFTER):
            with pytest.raises(UnlockError):
                u.open("wrong wrong wrong")
        with pytest.raises(UnlockError, match="Too many"):
            u.open(PASS)

    def test_app_starts_in_web_mode(self, tmp_path, web_config):
        from tests.ui_helpers import FakePage, find_key
        from ui.app import IFlowCoreApp

        session = WebVaultSession(Vault(MemoryStorage()), web_config.db_path, web_config.preferences_path)
        page = FakePage(width=1280, platform="web")
        app = IFlowCoreApp(page, config=web_config, services={"web_vault": session, "motion": None})
        app.start()
        assert app.unlocker.plan().mode == "create" and find_key(page.views[-1], "brand-logo") is not None
        app.services["on_unlocked"](app.unlocker.open(PASS, confirm=PASS))
        assert app.state.unlocked
        app.lock()
        assert not web_config.db_path.exists() and not session.vault.unlocked


# --- XHR transport ---------------------------------------------------------------------------------------


class FakeXhr:
    def __init__(self, status=200, body='{"ok": true}', headers="Content-Type: application/json\r\nRetry-After: 3",
                 fail=False):
        self.status, self.responseText, self._headers, self.fail = status, body, headers, fail
        self.sent = None
        self.opened = None
        self.req_headers = {}

    def open(self, method, url, is_async):
        self.opened = (method, url, is_async)

    def setRequestHeader(self, k, v):  # noqa: N802 (JS API)
        self.req_headers[k] = v

    def send(self, payload=None):
        if self.fail:
            raise RuntimeError("NetworkError")
        self.sent = payload

    def getAllResponseHeaders(self):  # noqa: N802
        return self._headers


class TestXhr:
    def test_request(self):
        xhr = FakeXhr()
        resp = XhrTransport(lambda: xhr).request("POST", "https://api.example.com/v1/x", params={"a": "1"},
                                                  headers={"Authorization": "Bearer t"}, body=b'{"k": 1}')
        assert xhr.opened == ("POST", "https://api.example.com/v1/x?a=1", False)          # synchronous
        assert xhr.req_headers["Authorization"] == "Bearer t" and xhr.sent == '{"k": 1}'
        assert isinstance(resp, HttpResponse) and resp.status == 200 and resp.json() == {"ok": True}
        assert resp.headers["retry-after"] == "3"

    def test_form(self):
        xhr = FakeXhr()
        XhrTransport(lambda: xhr).request("POST", "https://x.test/t", form={"a": "b c"})
        assert xhr.sent == "a=b+c" and xhr.req_headers["Content-Type"] == "application/x-www-form-urlencoded"

    @pytest.mark.parametrize("xhr", [FakeXhr(fail=True), FakeXhr(status=0)])
    def test_network_errors(self, xhr):
        with pytest.raises(ProviderError):
            XhrTransport(lambda: xhr).request("GET", "https://x.test/")

    def test_default_transport_off_browser(self):
        assert not in_browser() and not isinstance(default_transport(), XhrTransport)


# --- packaging ---------------------------------------------------------------------------------------------


class TestWebPackaging:
    def test_requirements(self):
        from build_common import project

        reqs = package_web.web_requirements(project())
        assert "flet==1.0.1" in reqs and "flet-charts==1.0.1" in reqs
        assert "tzdata" in reqs                                         # no system tz database in the browser
        assert not any("sqlcipher" in r or "secure-storage" in r or "flet-desktop" in r for r in reqs)

    def test_rewrite_dependencies(self, tmp_path):
        p = tmp_path / "pyproject.toml"
        p.write_text('[project]\nname = "x"\ndependencies = [\n    "flet>=1.0",\n    "old",\n]\n\n[tool.x]\ny = 1\n')
        package_web._rewrite_dependencies(p, ["flet==1.0.1", "tzdata"])
        import tomllib

        data = tomllib.loads(p.read_text())
        assert data["project"]["dependencies"] == ["flet==1.0.1", "tzdata"] and data["tool"]["x"]["y"] == 1

    def test_htaccess(self):
        h = package_web.HTACCESS
        assert "AddType application/wasm .wasm" in h and "DirectoryIndex index.html" in h
        assert "Options -Indexes" in h and "Require all denied" in h
