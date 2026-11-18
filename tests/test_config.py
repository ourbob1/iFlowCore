from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.config import (
    ConfigError,
    Environment,
    Preferences,
    UnitSystem,
    load_config,
    load_preferences,
    minor_units,
    normalize_currency,
    save_preferences,
)
from core.security import InvalidKeyMaterialError, generate_raw_key, is_raw_key, validate_key


def env_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"IFLOW_DATA_DIR": str(tmp_path), **{f"IFLOW_{k}": v for k, v in extra.items()}}


# --- app config ---------------------------------------------------------------


def test_defaults_are_development(tmp_path):
    cfg = load_config(env_for(tmp_path))
    assert cfg.environment is Environment.DEVELOPMENT
    assert cfg.debug is True
    assert cfg.require_encryption is False
    assert cfg.db_path == tmp_path / "iflowcore.db"
    assert cfg.sync_enabled is False
    assert cfg.preferences == Preferences()


def test_production_forces_encryption(tmp_path):
    cfg = load_config(env_for(tmp_path, ENV="production"))
    assert cfg.require_encryption is True
    assert cfg.debug is False
    with pytest.raises(ConfigError, match="cannot be disabled"):
        load_config(env_for(tmp_path, ENV="production", REQUIRE_ENCRYPTION="false"))


def test_production_requires_https_api(tmp_path):
    with pytest.raises(ConfigError, match="https"):
        load_config(env_for(tmp_path, ENV="production", API_URL="http://example.com"))
    cfg = load_config(env_for(tmp_path, ENV="production", API_URL="https://sync.example.com"))
    assert cfg.sync_enabled is True


def test_sync_enabled_without_url_rejected(tmp_path):
    with pytest.raises(ConfigError, match="API_URL"):
        load_config(env_for(tmp_path, SYNC_ENABLED="true"))


@pytest.mark.parametrize("name,value", [("ENV", "staging"), ("DEBUG", "maybe"), ("DB_POOL_SIZE", "0")])
def test_invalid_env_values(tmp_path, name, value):
    with pytest.raises(ConfigError):
        load_config(env_for(tmp_path, **{name: value}))


def test_flet_storage_dir_used_when_no_explicit_dir(tmp_path):
    cfg = load_config({"FLET_APP_STORAGE_DATA": str(tmp_path / "flet")})
    assert cfg.data_dir == tmp_path / "flet"


def test_empty_env_values_are_ignored(tmp_path):
    cfg = load_config(env_for(tmp_path, ENV="", DEBUG="  "))
    assert cfg.environment is Environment.DEVELOPMENT


# --- preferences --------------------------------------------------------------


def test_preferences_normalize_and_dedupe():
    p = Preferences(unit_system="METRIC", base_currency="eur", display_currencies=("usd", "EUR", "gbp"))
    assert p.unit_system is UnitSystem.METRIC
    assert p.base_currency == "EUR"
    assert p.display_currencies == ("EUR", "USD", "GBP")


@pytest.mark.parametrize("bad", ["US", "usdd", "XYZ", "12$"])
def test_invalid_currency_rejected(bad):
    with pytest.raises(ConfigError):
        normalize_currency(bad)


def test_minor_units():
    assert minor_units("usd") == 2
    assert minor_units("JPY") == 0
    assert minor_units("KWD") == 3


def test_invalid_locale_rejected():
    with pytest.raises(ConfigError):
        Preferences(locale="english")


def test_env_overrides_preferences(tmp_path):
    save_preferences(Preferences(base_currency="GBP"), tmp_path / "preferences.json")
    cfg = load_config(env_for(tmp_path, UNITS="metric", CURRENCIES="EUR,JPY"))
    assert cfg.preferences.unit_system is UnitSystem.METRIC
    assert cfg.preferences.base_currency == "GBP"            # from file
    assert cfg.preferences.display_currencies == ("GBP", "EUR", "JPY")


def test_preferences_roundtrip(tmp_path):
    path = tmp_path / "sub" / "preferences.json"
    prefs = Preferences(unit_system=UnitSystem.METRIC, base_currency="CAD", locale="fr_CA")
    save_preferences(prefs, path)
    assert load_preferences(path) == prefs
    assert not list(path.parent.glob(".prefs-*"))           # temp file cleaned up


def test_corrupt_preferences_fall_back_and_are_preserved(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text("{not json")
    assert load_preferences(path) == Preferences()
    assert (tmp_path / "preferences.json.corrupt").exists()


def test_unknown_preference_keys_ignored(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"base_currency": "EUR", "future_toggle": True}))
    assert load_preferences(path).base_currency == "EUR"


def test_unit_conversion():
    assert UnitSystem.IMPERIAL.to_meters(1) == 1609
    assert UnitSystem.METRIC.to_meters(2.5) == 2500
    assert UnitSystem.IMPERIAL.from_meters(1609344) == pytest.approx(1000)
    assert UnitSystem.IMPERIAL.distance_unit == "mi"


# --- security -----------------------------------------------------------------


def test_key_validation():
    raw = generate_raw_key()
    assert is_raw_key(raw) and validate_key(raw) == raw
    assert validate_key("a long enough passphrase")
    for bad in ["", "short", "has\x00nul-byte-inside"]:
        with pytest.raises(InvalidKeyMaterialError):
            validate_key(bad)


# --- iFlow Core rebrand: pre-rebrand (Ruby Soho) installs keep working ----------------------------


class TestRebrandCompat:
    def test_legacy_env_names_are_read(self, tmp_path):
        from core.config import Environment

        cfg = load_config({"RUBYSOHO_DATA_DIR": str(tmp_path), "RUBYSOHO_ENV": "test"})
        assert cfg.data_dir == tmp_path and cfg.environment is Environment.TEST

    def test_new_names_win(self, tmp_path):
        other = tmp_path / "old"
        cfg = load_config({"IFLOW_DATA_DIR": str(tmp_path), "RUBYSOHO_DATA_DIR": str(other)})
        assert cfg.data_dir == tmp_path

    def test_promote_legacy_env(self):
        from core.config import promote_legacy_env

        environ = {"RUBYSOHO_DB_KEY": "legacy-value-123", "RUBYSOHO_ENV": "test", "IFLOW_ENV": "production",
                   "RUBYSOHO_AI_URL": "  "}
        promoted = promote_legacy_env(environ)
        assert promoted == ["IFLOW_DB_KEY"]
        assert environ["IFLOW_DB_KEY"] == "legacy-value-123" and environ["IFLOW_ENV"] == "production"
        assert "IFLOW_AI_URL" not in environ

    def _dirs(self, tmp_path, monkeypatch):
        import platformdirs

        base = tmp_path / "share"
        monkeypatch.setattr(platformdirs, "user_data_dir", lambda name, appauthor=None: str(base / name))
        return base / "RubySoho", base / "iFlowCore"

    def test_legacy_data_dir_is_moved(self, tmp_path, monkeypatch):
        from core.config import migrate_legacy_data

        legacy, current = self._dirs(tmp_path, monkeypatch)
        (legacy / "logs").mkdir(parents=True)
        (legacy / "rubysoho.db").write_bytes(b"SQLite format 3\0data")
        (legacy / "preferences.json").write_text("{}")
        steps = migrate_legacy_data({})
        assert len(steps) == 2 and not legacy.exists()
        assert (current / "iflowcore.db").read_bytes() == b"SQLite format 3\0data"
        assert (current / "preferences.json").exists() and (current / "logs").is_dir()
        cfg = load_config({})
        assert cfg.db_path == current / "iflowcore.db"
        assert migrate_legacy_data({}) == []                               # idempotent

    def test_never_overwrites_a_new_install(self, tmp_path, monkeypatch):
        from core.config import migrate_legacy_data

        legacy, current = self._dirs(tmp_path, monkeypatch)
        legacy.mkdir(parents=True)
        (legacy / "rubysoho.db").write_bytes(b"old")
        current.mkdir(parents=True)
        (current / "iflowcore.db").write_bytes(b"new")
        assert migrate_legacy_data({}) == []
        assert (legacy / "rubysoho.db").read_bytes() == b"old" and (current / "iflowcore.db").read_bytes() == b"new"

    def test_unclean_database_keeps_legacy_name(self, tmp_path, monkeypatch):
        """A leftover WAL belongs to rubysoho.db; don't split the pair, keep using the old name."""
        from core.config import migrate_legacy_data

        (tmp_path / "rubysoho.db").write_bytes(b"db")
        (tmp_path / "rubysoho.db-wal").write_bytes(b"wal")
        env = {"IFLOW_DATA_DIR": str(tmp_path)}
        assert migrate_legacy_data(env) == []
        assert load_config(env).db_path == tmp_path / "rubysoho.db"

    def test_unmoved_legacy_dir_is_still_used(self, tmp_path, monkeypatch):
        legacy, current = self._dirs(tmp_path, monkeypatch)
        legacy.mkdir(parents=True)
        (legacy / "rubysoho.db").write_bytes(b"x")
        cfg = load_config({})                         # e.g. the move failed: don't start with an empty app
        assert cfg.data_dir == legacy and cfg.db_path == legacy / "rubysoho.db"

    def test_explicit_db_path_untouched(self, tmp_path):
        from core.config import migrate_legacy_data

        (tmp_path / "rubysoho.db").write_bytes(b"x")
        assert migrate_legacy_data({"IFLOW_DATA_DIR": str(tmp_path), "IFLOW_DB_PATH": str(tmp_path / "x.db")}) == []
        assert (tmp_path / "rubysoho.db").exists()

    def test_frozen_identifiers_unchanged(self):
        """Renaming these would lock existing installs out of their own data."""
        from core import crypto, security
        from database import migrations

        assert crypto._HKDF_INFO == b"rubysoho/field-sealing/v1"
        assert security.DEVICE_KEY_NAME == "rubysoho.device_key.v1"
        assert migrations._DISABLE_FK_MARKER == "-- rubysoho: disable-foreign-keys"


# --- timezones on phones and browsers (the first Android build failed with "Unknown timezone 'UTC'") ------


class TestTimezones:
    def test_tzdata_ships_everywhere(self):
        import tomllib

        deps = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"][
            "dependencies"]
        tz = [d for d in deps if d.startswith("tzdata")]
        assert tz and ";" not in tz[0]

    def test_zones_load_without_the_os_database(self, monkeypatch):
        import zoneinfo

        monkeypatch.setattr(zoneinfo, "TZPATH", ())
        zoneinfo.reset_tzpath([])
        try:
            zoneinfo.ZoneInfo.clear_cache()
            from core.timeutil import zone

            for name in ("UTC", "America/Chicago", "Asia/Kolkata"):
                assert zone(name).key == name
        finally:
            zoneinfo.reset_tzpath()
            zoneinfo.ZoneInfo.clear_cache()

    def test_android_zone_from_system_property(self, monkeypatch):
        import subprocess

        from core import device_settings

        monkeypatch.delenv("TZ", raising=False)
        monkeypatch.setenv("FLET_PLATFORM", "android")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "Europe/Berlin\n"})())
        assert device_settings.guess_timezone() == "Europe/Berlin"

    def test_bad_values_fall_back(self, monkeypatch):
        import subprocess

        from core import device_settings

        monkeypatch.setenv("TZ", "Not/AZone")
        monkeypatch.setenv("FLET_PLATFORM", "android")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "garbage\n"})())
        from zoneinfo import ZoneInfo

        assert ZoneInfo(device_settings.guess_timezone())          # always a loadable zone
