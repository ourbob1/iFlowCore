"""Platform-conditional storage: desktop enforces SQLCipher, mobile falls back safely."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from core.config import ConfigError, load_config
from core.crypto import FieldSealer, SealingError, is_sealed
from core.device_settings import SENSITIVE_FIELDS, SettingsError, SettingsStore
from core.platform import PlatformError, detect_platform, is_desktop, is_mobile
from core.security import (
    DEVICE_KEY_NAME,
    KeyStoreError,
    MemoryKeyStore,
    StoragePolicyError,
    ensure_device_key,
    generate_raw_key,
    resolve_storage_policy,
)
from database.connection import Database, EncryptionUnavailableError
from database.encryption import MigrationFailed, is_plaintext_sqlite, migrate_to_sqlcipher

from .conftest import requires_sqlcipher

ROOT = Path(__file__).resolve().parents[1]
KEY = "ab" * 32
PASS = "correct horse battery staple"


@pytest.fixture
def no_sqlcipher(monkeypatch):
    """Simulate a runtime without any SQLCipher driver (e.g. Flet on Android/iOS)."""
    monkeypatch.setattr("database.connection.load_sqlcipher_driver", lambda: None)


# --- platform detection --------------------------------------------------------------------


@pytest.mark.parametrize("env,sys_platform,android_api,expected", [
    ({"IFLOW_PLATFORM": "iOS"}, "linux", False, "ios"),                 # explicit override wins
    ({"FLET_PLATFORM": "android"}, "linux", False, "android"),             # Flet packaged app
    ({"FLET_PLATFORM": "ios"}, "darwin", False, "ios"),
    ({"FLET_PLATFORM": "macos"}, "darwin", False, "macos"),
    ({}, "android", False, "android"),                                     # CPython ≥ 3.13
    ({}, "ios", False, "ios"),
    ({}, "linux", True, "android"),                                        # sys.getandroidapilevel
    ({}, "linux", False, "linux"),
    ({}, "darwin", False, "macos"),
    ({}, "win32", False, "windows"),
    ({}, "emscripten", False, "web"),
])
def test_detect_platform(env, sys_platform, android_api, expected):
    assert detect_platform(env, sys_platform=sys_platform, android_api=android_api) == expected


def test_detect_platform_rejects_bad_override_and_real_host():
    with pytest.raises(PlatformError):
        detect_platform({"IFLOW_PLATFORM": "playstation"})
    assert detect_platform({}) in ("linux", "macos", "windows")             # this CI host
    assert is_mobile("android") and is_mobile("ios") and not is_mobile("macos")
    assert is_desktop("windows") and not is_desktop("web")


def test_config_carries_platform(tmp_path):
    cfg = load_config({"IFLOW_DATA_DIR": str(tmp_path), "FLET_PLATFORM": "android", "IFLOW_ENV": "production"})
    assert cfg.platform == "android" and cfg.is_mobile
    assert cfg.require_encryption and not cfg.requires_sqlcipher          # met by platform protection
    desk = load_config({"IFLOW_DATA_DIR": str(tmp_path), "IFLOW_ENV": "production"})
    assert desk.requires_sqlcipher
    with pytest.raises(ConfigError):
        load_config({"IFLOW_DATA_DIR": str(tmp_path), "IFLOW_PLATFORM": "nope"})


# --- storage policy -----------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["windows", "macos", "linux"])
def test_desktop_policy_fails_loudly(platform):
    with pytest.raises(StoragePolicyError, match="driver"):
        resolve_storage_policy(platform, require_encryption=True, sqlcipher_available=False, have_key=True)
    with pytest.raises(StoragePolicyError, match="key"):
        resolve_storage_policy(platform, require_encryption=True, sqlcipher_available=True, have_key=False)
    assert resolve_storage_policy(platform, require_encryption=True, sqlcipher_available=True,
                                  have_key=True).mode == "sqlcipher"
    assert resolve_storage_policy(platform, require_encryption=False, sqlcipher_available=False,
                                  have_key=False).mode == "none"


@pytest.mark.parametrize("platform", ["android", "ios"])
def test_mobile_policy_never_raises(platform):
    fallback = resolve_storage_policy(platform, require_encryption=True, sqlcipher_available=False, have_key=True)
    assert fallback.mode == "platform" and fallback.field_sealing and not fallback.encrypted_database
    upgraded = resolve_storage_policy(platform, require_encryption=True, sqlcipher_available=True, have_key=True)
    assert upgraded.mode == "sqlcipher"


# --- database: desktop unchanged ------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["windows", "macos", "linux"])
def test_desktop_requires_sqlcipher(tmp_path, no_sqlcipher, platform):
    with pytest.raises(EncryptionUnavailableError):
        Database(tmp_path / "x.db", require_encryption=True, key=PASS, platform=platform)
    with pytest.raises(EncryptionUnavailableError):
        Database(tmp_path / "x.db", key=PASS, platform=platform)            # a key is never ignored
    with pytest.raises(EncryptionUnavailableError):
        Database(tmp_path / "x.db", require_encryption=True, driver="sqlite", platform=platform)


@requires_sqlcipher
@pytest.mark.parametrize("platform", ["windows", "macos", "linux"])
def test_desktop_encrypts_with_passphrase(tmp_path, platform):
    path = tmp_path / "x.db"
    with Database(path, key=PASS, require_encryption=True, platform=platform) as db:
        db.initialize()
        assert db.encrypted and db.protection == "sqlcipher"
    assert not is_plaintext_sqlite(path)


def test_desktop_dev_without_key_is_marked_unencrypted(tmp_path):
    db = Database(tmp_path / "x.db", platform="linux")
    db.initialize()
    assert db.protection == "none" and not db.encrypted


# --- database: mobile fallback ----------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["android", "ios"])
def test_mobile_without_sqlcipher_opens_platform_sqlite(tmp_path, no_sqlcipher, platform):
    path = tmp_path / "x.db"
    db = Database(path, key=KEY, require_encryption=True, platform=platform)   # would raise on desktop
    assert db.protection == "platform" and not db.encrypted and db.driver_name == "sqlite3"
    assert db.initialize() >= 5
    with db.transaction() as conn:
        conn.execute("INSERT INTO platforms (name) VALUES ('Uber')")
    with db.connection() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1          # hardening still applied
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    db.close()
    assert is_plaintext_sqlite(path)
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "key" not in repr(db).lower() or KEY not in repr(db)


def test_mobile_without_key_also_opens(tmp_path, no_sqlcipher):
    db = Database(tmp_path / "x.db", require_encryption=True, platform="android")
    db.initialize()
    assert db.protection == "platform"


@requires_sqlcipher
def test_mobile_with_bundled_sqlcipher_encrypts(tmp_path):
    path = tmp_path / "x.db"
    db = Database(path, key=KEY, require_encryption=True, platform="ios")
    db.initialize()
    assert db.protection == "sqlcipher"
    db.close()
    assert not is_plaintext_sqlite(path)


def test_mobile_explicit_sqlcipher_request_still_errors(tmp_path, no_sqlcipher):
    with pytest.raises(EncryptionUnavailableError):
        Database(tmp_path / "x.db", key=KEY, driver="sqlcipher", platform="android")


# --- device key storage ------------------------------------------------------------------------------


def test_device_key_created_once_and_reused():
    store = MemoryKeyStore()
    first = asyncio.run(ensure_device_key(store))
    assert len(first) == 64 and asyncio.run(store.get(DEVICE_KEY_NAME)) == first
    assert asyncio.run(ensure_device_key(store)) == first


def test_device_key_errors_are_loud():
    class Broken(MemoryKeyStore):
        async def get(self, name):
            raise OSError("keystore locked")

    class Dropping(MemoryKeyStore):
        async def set(self, name, value):
            pass

    with pytest.raises(KeyStoreError, match="unavailable"):
        asyncio.run(ensure_device_key(Broken()))
    with pytest.raises(KeyStoreError, match="persist"):
        asyncio.run(ensure_device_key(Dropping()))
    with pytest.raises(KeyStoreError, match="malformed"):
        asyncio.run(ensure_device_key(MemoryKeyStore({DEVICE_KEY_NAME: "not-a-key"})))


# --- field sealing -------------------------------------------------------------------------------------


def test_sealer_roundtrip_binding_and_tamper():
    sealer = FieldSealer(KEY)
    token = sealer.seal('{"lat": 27.95}', context="device_settings.home")
    assert is_sealed(token) and "27.95" not in token
    assert sealer.open(token, context="device_settings.home") == '{"lat": 27.95}'
    assert sealer.seal("x", context="c") != sealer.seal("x", context="c")      # random nonce
    with pytest.raises(SealingError):
        sealer.open(token, context="device_settings.work_zones")                # bound to its field
    with pytest.raises(SealingError):
        FieldSealer(generate_raw_key()).open(token, context="device_settings.home")
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(SealingError):
        sealer.open(tampered, context="device_settings.home")
    for bad in ("plain", "rs1:***", "rs1:"):
        with pytest.raises(SealingError):
            sealer.open(bad, context="c")
    with pytest.raises(SealingError):
        FieldSealer("abcd")


def test_sensitive_settings_sealed_at_rest(db):
    store = SettingsStore(db, sealer=FieldSealer(KEY), require_sealing=True)
    store.update(home={"lat": 27.95, "lng": -82.46}, ticketmaster_api_key="tm-secret", timezone="America/New_York")
    with db.connection() as conn:
        raw = dict(conn.execute("SELECT key, value_json FROM device_settings").fetchall())
    assert "27.95" not in raw["home"] and "tm-secret" not in raw["ticketmaster_api_key"]
    assert set(json.loads(raw["home"])) == {"sealed"}
    assert json.loads(raw["timezone"]) == "America/New_York"                   # non-sensitive stays readable
    assert store.load().home == {"lat": 27.95, "lng": -82.46}


def test_platform_mode_refuses_unsealed_sensitive_writes(db):
    store = SettingsStore(db, require_sealing=True)
    store.update(timezone="America/New_York")                                    # fine
    with pytest.raises(SettingsError, match="unsealed"):
        store.update(opensky_client_secret="s3cret")


def test_unreadable_sealed_values_are_not_clobbered(db):
    SettingsStore(db, sealer=FieldSealer(KEY)).update(home={"lat": 1.0, "lng": 2.0})
    wrong = SettingsStore(db, sealer=FieldSealer(generate_raw_key()))
    assert wrong.load().home is None and "home" in wrong.unreadable
    wrong.update(timezone="UTC")                                                 # saving other fields…
    assert SettingsStore(db, sealer=FieldSealer(KEY)).load().home == {"lat": 1.0, "lng": 2.0}   # …keeps it


def test_sensitive_field_list():
    assert {"home", "work_zones", "ticketmaster_api_key", "opensky_client_secret"} <= SENSITIVE_FIELDS
    assert "timezone" not in SENSITIVE_FIELDS


# --- plaintext → SQLCipher upgrade path -------------------------------------------------------------------


@requires_sqlcipher
def test_migrate_platform_db_to_sqlcipher(tmp_path, monkeypatch):
    path = tmp_path / "x.db"
    monkeypatch.setattr("database.connection.load_sqlcipher_driver", lambda: None)
    with Database(path, key=KEY, platform="android") as plain:
        version = plain.initialize()
        with plain.transaction() as conn:
            conn.execute("INSERT INTO platforms (name) VALUES ('Uber')")
    monkeypatch.undo()
    assert is_plaintext_sqlite(path)
    result = migrate_to_sqlcipher(path, KEY)
    assert result.rows == 1 and result.user_version == version
    assert not is_plaintext_sqlite(path) and not Path(f"{path}.encrypting").exists()
    with Database(path, key=KEY, platform="android") as enc:
        assert enc.protection == "sqlcipher" and enc.schema_version() == version
        with enc.connection() as conn:
            assert conn.execute("SELECT name FROM platforms").fetchone()[0] == "Uber"
    with pytest.raises(MigrationFailed):
        migrate_to_sqlcipher(path, KEY)                                           # already encrypted


# --- app startup on each platform class ---------------------------------------------------------------------


def _app(tmp_path, platform, **extra):
    from ui.app import IFlowCoreApp

    from .ui_helpers import FakePage

    env = {"IFLOW_DATA_DIR": str(tmp_path), "IFLOW_REQUIRE_ENCRYPTION": "true",
           "IFLOW_PLATFORM": platform, **extra}
    page = FakePage(390 if platform in ("android", "ios") else 1280, platform)
    return IFlowCoreApp(page, env=env, services={"motion": None}), page


@pytest.mark.parametrize("platform", ["android", "ios"])
def test_mobile_startup_uses_device_key(tmp_path, no_sqlcipher, platform):
    from .ui_helpers import find_key

    app, page = _app(tmp_path, platform)
    app.services["keystore"] = store = MemoryKeyStore()
    app.start()                                          # FakePage.run_task is a no-op: unlock screen shows
    assert page.route == "/unlock" and find_key(page.views[-1], "device-key-progress") is not None
    assert find_key(page.views[-1], "passphrase") is None                        # no passphrase on phones
    assert asyncio.run(app.unlock_with_device_key()) is True
    assert app.state.db.protection == "platform" and app.state.banner is None
    assert page.route == "/"
    app.state.update_settings(home={"lat": 27.95, "lng": -82.46})                # sealed with the device key
    with app.state.db.connection() as conn:
        assert "27.95" not in conn.execute("SELECT value_json FROM device_settings WHERE key='home'").fetchone()[0]
    key = asyncio.run(store.get(DEVICE_KEY_NAME))
    app.state.lock()
    assert asyncio.run(app.unlock_with_device_key()) and asyncio.run(store.get(DEVICE_KEY_NAME)) == key
    assert app.state.settings.home == {"lat": 27.95, "lng": -82.46}


def test_mobile_startup_keystore_failure_shows_retry(tmp_path, no_sqlcipher):
    from .ui_helpers import find_key

    class Broken(MemoryKeyStore):
        async def get(self, name):
            raise OSError("user has no screen lock")

    app, page = _app(tmp_path, "android")
    app.services["keystore"] = Broken()
    app.start()
    assert asyncio.run(app.unlock_with_device_key()) is False
    assert app.state.db is None and "screen lock" in app.last_error
    assert find_key(page.views[-1], "btn-retry-device-key") is not None


@requires_sqlcipher
def test_mobile_upgrade_encrypts_existing_database_on_unlock(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, "android")
    app.services["keystore"] = store = MemoryKeyStore()
    monkeypatch.setattr("database.connection.load_sqlcipher_driver", lambda: None)
    asyncio.run(app.unlock_with_device_key())
    assert app.state.db.protection == "platform"
    app.state.lock()
    monkeypatch.undo()                                   # new build now bundles SQLCipher
    app2, _ = _app(tmp_path, "android")
    app2.services["keystore"] = store
    assert asyncio.run(app2.unlock_with_device_key())
    assert app2.state.db.protection == "sqlcipher"
    assert not is_plaintext_sqlite(app2.config.db_path)


def test_desktop_startup_unchanged(tmp_path, no_sqlcipher):
    app, page = _app(tmp_path, "linux")
    app.start()
    assert page.route == "/unlock" and app.unlocker.plan().mode == "create"     # passphrase flow
    from ui.state import UnlockError

    with pytest.raises(UnlockError, match="SQLCipher"):                         # fails loudly, no fallback
        app.unlocker.open(PASS, confirm=PASS)
    assert app.state.db is None


# --- build configuration -------------------------------------------------------------------------------------


def test_build_dependencies_are_platform_conditional():
    tomllib = pytest.importorskip("tomllib")          # stdlib from 3.11
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    base = " ".join(cfg["project"]["dependencies"])
    assert "sqlcipher" not in base                       # never pulled into mobile builds
    assert "cryptography" in base
    flet = cfg["tool"]["flet"]
    assert flet["linux"]["dependencies"] == ["sqlcipher3-binary>=0.5"]
    assert all("sqlcipher3" in d for p in ("macos", "windows") for d in flet[p]["dependencies"])
    for mobile in ("android", "ios"):
        deps = " ".join(flet[mobile]["dependencies"])
        assert "flet-secure-storage" in deps and "sqlcipher" not in deps
