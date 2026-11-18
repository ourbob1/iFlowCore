"""Phase 9: packaging and deployment validation.

Covers the build-time guarantees (allowlist staging, secret scanning, desktop/mobile config split,
signing hygiene), the packaged-app defaults (_build_info.json → production), and the self-test
that ships inside every binary. Real PyInstaller/Flutter builds are exercised by the scripts
themselves (see README "Phase 9 verification"); here they run as dry runs.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_common as bc  # noqa: E402
import package_desktop as pdesk  # noqa: E402
import package_mobile as pmob  # noqa: E402
import validate_deployment as vdep  # noqa: E402

from core import __version__  # noqa: E402
from core import buildinfo, selftest  # noqa: E402
from core.config import Environment, load_config  # noqa: E402
from tests.conftest import requires_sqlcipher  # noqa: E402

FAKE_PEM = ("-----BEGIN PRIVATE KEY-----\n" + "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7" * 3 + "\n"
            "-----END PRIVATE KEY-----\n")


@pytest.fixture
def build_tmp(tmp_path, monkeypatch):
    """Point build/ and dist/ at a temp dir so tests never touch the project's build output."""
    build, dist = tmp_path / "build", tmp_path / "dist"
    for mod in (bc, pdesk, pmob):
        monkeypatch.setattr(mod, "BUILD_DIR", build, raising=False)
        monkeypatch.setattr(mod, "DIST_DIR", dist, raising=False)
    return tmp_path


def _fake_app(root: Path) -> Path:
    (root / "core" / "financial" / "tax_tables").mkdir(parents=True)
    (root / "core" / "__init__.py").write_text("__version__ = '0.0.0'\n")
    (root / "core" / "financial" / "tax_tables" / "us.json").write_text("{}")
    (root / "core" / "__pycache__").mkdir()
    (root / "core" / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\0")
    (root / "database" / "migrations").mkdir(parents=True)
    (root / "database" / "schema.sql").write_text("CREATE TABLE t (id INTEGER);\n")
    (root / "database" / "migrations" / "0002_x.sql").write_text("SELECT 1;\n")
    (root / "ui").mkdir()
    (root / "ui" / "app.py").write_text("x = 1\n")
    (root / "main.py").write_text("print('hi')\n")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("")
    (root / "services").mkdir()
    (root / "services" / "server.py").write_text("")
    return root


# --- build info → packaged defaults --------------------------------------------------------------


class TestBuildInfo:
    def test_source_checkout_has_no_default(self, tmp_path):
        info = buildinfo.load([tmp_path])
        assert info.channel == "source" and not info.packaged and info.default_environment is None

    @pytest.mark.parametrize("channel,env", [("production", "production"), ("beta", "production"),
                                             ("qa", "test"), ("dev", "development")])
    def test_channel_maps_to_environment(self, tmp_path, channel, env):
        (tmp_path / buildinfo.FILENAME).write_text(json.dumps({"channel": channel, "version": "1.0.0"}))
        info = buildinfo.load([tmp_path])
        assert info.packaged and info.default_environment == env

    @pytest.mark.parametrize("content", ["not json", "[]", '{"channel": "god-mode"}', "{}"])
    def test_invalid_file_is_ignored(self, tmp_path, content):
        (tmp_path / buildinfo.FILENAME).write_text(content)
        assert buildinfo.load([tmp_path]).channel == "source"

    def test_first_valid_directory_wins(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir(), b.mkdir()
        (b / buildinfo.FILENAME).write_text('{"channel": "qa"}')
        assert buildinfo.load([a, b]).channel == "qa"

    def test_packaged_build_defaults_to_production(self, tmp_path, monkeypatch):
        monkeypatch.setattr(buildinfo, "load", lambda dirs=None: buildinfo.BuildInfo(channel="production"))
        cfg = load_config({"IFLOW_DATA_DIR": str(tmp_path), "IFLOW_PLATFORM": "linux"})
        assert cfg.environment is Environment.PRODUCTION
        assert cfg.require_encryption and cfg.requires_sqlcipher

    def test_explicit_environment_still_wins(self, tmp_path, monkeypatch):
        monkeypatch.setattr(buildinfo, "load", lambda dirs=None: buildinfo.BuildInfo(channel="production"))
        cfg = load_config({"IFLOW_DATA_DIR": str(tmp_path), "IFLOW_ENV": "test"})
        assert cfg.environment is Environment.TEST

    def test_source_checkout_stays_development(self, tmp_path, monkeypatch):
        monkeypatch.setattr(buildinfo, "load", lambda dirs=None: buildinfo.BuildInfo())
        cfg = load_config({"IFLOW_DATA_DIR": str(tmp_path)})
        assert cfg.environment is Environment.DEVELOPMENT


# --- self-test (ships inside every binary) -------------------------------------------------------


class TestSelfTest:
    def test_core_checks_pass(self):
        report = selftest.run(["runtime", "assets", "db_init", "migrations", "mobile_storage", "security"])
        assert report.ok, report.to_text()
        assert [c.name for c in report.checks] == ["runtime", "assets", "db_init", "migrations",
                                                   "mobile_storage", "security"]

    def test_migrations_cover_every_historical_version(self):
        from database.migrations import discover_migrations

        report = selftest.run(["migrations"])
        latest = max(m.version for m in discover_migrations())
        assert report.ok and f"v1…v{latest - 1}" in report.checks[0].detail

    @requires_sqlcipher
    def test_encryption_handshake(self):
        report = selftest.run(["encryption"])
        check = report.checks[0]
        assert check.status == "pass", check.detail
        assert "wrong key refused" in check.detail and "file encrypted" in check.detail

    def test_ui_startup(self):
        report = selftest.run(["ui_startup"])
        assert report.ok, report.to_text()

    def test_failure_is_reported_not_raised(self, monkeypatch):
        def boom(_tmp):
            raise RuntimeError("schema missing")

        monkeypatch.setattr(selftest, "CHECKS", tuple((n, boom if n == "assets" else f) for n, f in selftest.CHECKS))
        report = selftest.run(["assets"])
        assert not report.ok and report.checks[0].status == "fail" and "schema missing" in report.checks[0].detail

    def test_main_writes_json_report(self, tmp_path):
        out = tmp_path / "r.json"
        assert selftest.main(["--json", "--only=runtime,assets", f"--out={out}"]) == 0
        data = json.loads(out.read_text())
        assert data["ok"] and {c["name"] for c in data["checks"]} == {"runtime", "assets"}

    def test_build_info_with_extra_keys_fails_security(self, tmp_path, monkeypatch):
        (tmp_path / buildinfo.FILENAME).write_text('{"channel": "production", "db_key": "hunter2hunter2"}')
        monkeypatch.setattr(buildinfo, "candidate_dirs", lambda: [tmp_path])
        report = selftest.run(["security"])
        assert not report.ok and "db_key" in report.checks[0].detail


# --- staging -------------------------------------------------------------------------------------


class TestStage:
    def test_only_allowlisted_app_files(self, tmp_path):
        root = _fake_app(tmp_path / "src")
        st = bc.stage("linux", root=root, out=tmp_path / "stage")
        assert "main.py" in st.files and "database/schema.sql" in st.files
        assert "database/migrations/0002_x.sql" in st.files and "core/financial/tax_tables/us.json" in st.files
        assert not any(f.startswith(("tests/", "services/")) or f.endswith(".pyc") for f in st.files)
        assert not (tmp_path / "stage" / "tests").exists()

    def test_build_info_is_metadata_only(self, tmp_path):
        root = _fake_app(tmp_path / "src")
        st = bc.stage("android", channel="beta", root=root, out=tmp_path / "stage")
        info = json.loads((st.path / "_build_info.json").read_text())
        assert set(info) == {"channel", "version", "target", "backend", "built_at", "app"} and info["app"] == "core"
        assert info["channel"] == "beta" and info["target"] == "android" and info["version"] == __version__

    @pytest.mark.parametrize("name", [".env", "iflowcore.db", "preferences.json", "upload.jks", "id_rsa",
                                      "signing.p12", "errors.jsonl", "data.sqlite3", "server.key"])
    def test_refuses_user_data_and_secrets(self, tmp_path, name):
        root = _fake_app(tmp_path / "src")
        (root / "core" / name).write_text("x")
        with pytest.raises(bc.BuildError, match="refusing to package"):
            bc.stage("linux", root=root, out=tmp_path / "stage")

    def test_unknown_channel(self, tmp_path):
        with pytest.raises(bc.BuildError):
            bc.stage("linux", channel="yolo", root=_fake_app(tmp_path / "src"), out=tmp_path / "s")

    def test_real_project_stage_is_clean(self, tmp_path):
        st = bc.stage("linux", out=tmp_path / "stage")
        bc.require_clean(st.path)
        for needed in ("database/schema.sql", "database/migrations/0008_health_engine.sql",
                       "core/financial/tax_tables/us_federal_2026.json", "main.py", "_build_info.json"):
            assert needed in st.files


# --- secret scanning -----------------------------------------------------------------------------


class TestSecretScan:
    def test_private_key_in_source(self, tmp_path):
        (tmp_path / "keys.py").write_text(f'KEY = """{FAKE_PEM}"""')
        assert [f.what for f in bc.scan_tree(tmp_path, env={})] == ["private key block"]

    def test_bare_pem_header_in_library_is_not_a_key(self, tmp_path):
        (tmp_path / "libcrypto.so").write_bytes(b"\0\0-----BEGIN PRIVATE KEY-----\0%s\0")
        assert bc.scan_tree(tmp_path, env={}, binary=True) == []

    @pytest.mark.parametrize("text", ['password = "Sup3rS3cretValue!"', "api_key: 'abcdef0123456789xyz'",
                                      'token = "ghp_' + "a" * 36 + '"', 'k = "AKIAABCDEFGHIJKLMNOP"'])
    def test_hardcoded_credentials(self, tmp_path, text):
        (tmp_path / "settings.py").write_text(text + "\n")
        assert bc.scan_tree(tmp_path, env={})

    @pytest.mark.parametrize("text", ['password = "IFLOW_DB_KEY"', 'secret = ""', "password = getpass()",
                                      'label = "Enter your passphrase"'])
    def test_env_names_and_prompts_are_fine(self, tmp_path, text):
        (tmp_path / "settings.py").write_text(text + "\n")
        assert bc.scan_tree(tmp_path, env={}) == []

    def test_build_machine_secret_value_in_binary(self, tmp_path):
        secret = "local-db-passphrase-9f2c"
        (tmp_path / "libapp.so").write_bytes(b"\x7fELF...." + secret.encode() + b"....")
        findings = bc.scan_tree(tmp_path, env={"IFLOW_DB_KEY": secret}, binary=True)
        assert findings and "IFLOW_DB_KEY" in findings[0].what
        # a text-only scan wouldn't look inside the binary; the artifact scan must
        assert bc.scan_tree(tmp_path, env={"IFLOW_DB_KEY": secret}) == []

    def test_short_env_values_are_ignored(self, tmp_path):
        (tmp_path / "a.txt").write_text("abc")
        assert bc.scan_tree(tmp_path, env={"IFLOW_DB_KEY": "abc"}) == []

    def test_artifact_names(self, tmp_path):
        (tmp_path / "certifi").mkdir()
        (tmp_path / "certifi" / "cacert.pem").write_text("-----BEGIN CERTIFICATE-----\n")
        assert bc.scan_tree(tmp_path, env={}, binary=True) == []
        (tmp_path / "leftover.db").write_bytes(b"SQLite format 3\0")
        assert [f.path for f in bc.scan_tree(tmp_path, env={}, binary=True)] == ["leftover.db"]

    def test_nested_archive(self, tmp_path):
        inner = tmp_path / "app.zip"
        with zipfile.ZipFile(inner, "w") as zf:
            zf.writestr("core/keys.py", f'K = """{FAKE_PEM}"""')
        apk = tmp_path / "app.apk"
        with zipfile.ZipFile(apk, "w") as zf:
            zf.write(inner, "assets/app/app.zip")
            zf.writestr("classes.dex", b"\0" * 64)
        findings = bc.scan_archive(apk, env={})
        assert findings and "app.zip" in findings[0].path and findings[0].what == "private key block"

    def test_archive_path_traversal(self, tmp_path):
        bad = tmp_path / "bad.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("../../evil.py", "x")
        assert any(f.what == "unsafe path inside archive" for f in bc.scan_archive(bad, env={}))
        assert not (tmp_path.parent / "evil.py").exists()

    def test_require_clean_raises(self, tmp_path, monkeypatch):
        (tmp_path / "k.py").write_text(f'K = """{FAKE_PEM}"""')
        with pytest.raises(bc.BuildError, match="secret scan failed"):
            bc.require_clean(tmp_path)


# --- pyproject: desktop vs mobile split ----------------------------------------------------------


class TestPyproject:
    def test_project_config_is_valid(self):
        assert bc.validate_pyproject() == []

    def _data(self):
        return copy.deepcopy(bc.project())

    def test_sqlcipher_on_mobile_is_rejected(self):
        data = self._data()
        data["tool"]["flet"]["android"]["dependencies"].append("sqlcipher3-binary")
        assert any("must not add SQLCipher" in p for p in bc.validate_pyproject(["android"], data))

    def test_desktop_without_sqlcipher_is_rejected(self):
        data = self._data()
        data["tool"]["flet"]["windows"]["dependencies"] = []
        assert any("windows" in p and "SQLCipher" in p for p in bc.validate_pyproject(["windows"], data))

    def test_mobile_without_secure_storage_is_rejected(self):
        data = self._data()
        data["tool"]["flet"]["ios"]["dependencies"] = []
        assert any("flet-secure-storage" in p for p in bc.validate_pyproject(["ios"], data))

    def test_platform_packages_stay_out_of_base_dependencies(self):
        data = self._data()
        data["project"]["dependencies"] += ["sqlcipher3>=0.5", "flet-secure-storage"]
        problems = bc.validate_pyproject(["linux"], data)
        assert any("base dependency" in p for p in problems) and any("mobile targets only" in p for p in problems)

    def test_signing_password_in_pyproject_is_rejected(self):
        data = self._data()
        data["tool"]["flet"]["android"]["signing"] = {"key_store": "x.jks", "key_store_password": "p"}
        assert any("environment variables" in p for p in bc.validate_pyproject(["android"], data))

    def test_missing_permissions_and_plist_keys(self):
        data = self._data()
        del data["tool"]["flet"]["android"]["permission"]["android.permission.ACCESS_FINE_LOCATION"]
        del data["tool"]["flet"]["ios"]["info"]["NSLocationWhenInUseUsageDescription"]
        problems = bc.validate_pyproject(["android", "ios"], data)
        assert any("ACCESS_FINE_LOCATION" in p for p in problems)
        assert any("NSLocationWhenInUseUsageDescription" in p for p in problems)

    def test_flet_build_excludes(self):
        data = self._data()
        data["tool"]["flet"]["app"]["exclude"] = ["venv"]
        assert any("'tests'" in p for p in bc.validate_pyproject(["linux"], data))

    def test_tzdata_must_be_unconditional(self):
        data = self._data()
        data["project"]["dependencies"] = [d if not d.startswith("tzdata") else "tzdata>=2024.1; sys_platform == 'win32'"
                                           for d in data["project"]["dependencies"]]
        assert any("tzdata" in p for p in bc.validate_pyproject(["android"], data))

    def test_version_matches_code(self):
        data = self._data()
        data["project"]["version"] = "0.0.1"
        assert any("!= core.__version__" in p for p in bc.validate_pyproject(["linux"], data))

    @pytest.mark.parametrize("version,number", [("0.9.0", 900), ("1.2.3", 10203), ("2.0", 20000)])
    def test_build_number(self, version, number):
        assert bc.build_number(version) == number


# --- desktop packaging ---------------------------------------------------------------------------


class TestPackageDesktop:
    def test_defaults(self):
        plan, _ = pdesk.parse(["--target", "linux"])
        assert plan.formats == ["tar", "deb", "appimage"] and plan.channel == "production"
        plan, _ = pdesk.parse(["--target", "windows"])
        assert plan.onefile and plan.formats == ["exe"]
        plan, _ = pdesk.parse(["--target", "windows", "--onedir"])
        assert not plan.onefile and plan.formats == ["zip"]

    def test_bad_format(self):
        with pytest.raises(SystemExit):
            pdesk.parse(["--target", "macos", "--formats", "deb"])

    def test_pack_command_bundles_assets_and_sqlcipher(self, tmp_path):
        st = bc.stage("linux", out=tmp_path / "stage")
        plan, _ = pdesk.parse(["--target", "linux"])
        cmd = pdesk.pack_command(plan, st, tmp_path / "work")
        joined = " ".join(cmd)
        sep = os.pathsep
        assert f"schema.sql{sep}database " in joined + " "
        for n in ("0002", "0005", "0008"):
            assert any(n in c and c.endswith(f"{sep}database/migrations") for c in cmd)
        assert any(c.endswith(f"{sep}core/financial/tax_tables") for c in cmd)
        assert any(c.endswith(f"_build_info.json{sep}.") for c in cmd)
        assert "sqlcipher3" in cmd and "--pyinstaller-build-args=--collect-binaries=sqlcipher3" in cmd
        assert "--onedir" in cmd and str(st.path / "main.py") in cmd
        assert not any("tests" in c and "--add-data" in c for c in cmd)

    def test_build_env_strips_secrets(self, monkeypatch):
        monkeypatch.setenv("IFLOW_DB_KEY", "x" * 20)
        monkeypatch.setenv("IFLOW_ENV", "development")
        monkeypatch.setenv("PYTHONPATH", str(ROOT))
        env = pdesk.build_env()
        assert "IFLOW_DB_KEY" not in env and "IFLOW_ENV" not in env and "PYTHONPATH" not in env

    def test_no_cross_compiling(self, build_tmp):
        other = next(t for t in bc.DESKTOP_TARGETS if t != bc.host_target())
        plan, _ = pdesk.parse(["--target", other])
        with pytest.raises(bc.BuildError, match="don't cross-compile"):
            pdesk.preflight(plan, dry_run=False)

    @pytest.mark.parametrize("target", bc.DESKTOP_TARGETS)
    def test_dry_run_every_target(self, build_tmp, target, capsys):
        assert pdesk.main(["--target", target, "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "dry run: nothing built" in out and "secret scan clean" in out
        assert not (build_tmp / "dist").exists()

    def test_dry_run_refuses_a_planted_secret(self, build_tmp, monkeypatch, capsys):
        real_stage = pdesk.stage

        def poisoned(*a, **kw):
            st = real_stage(*a, **kw)
            (st.path / "core" / "leak.py").write_text(f'K = """{FAKE_PEM}"""')
            return st

        monkeypatch.setattr(pdesk, "stage", poisoned)
        assert pdesk.main(["--dry-run"]) == 1
        assert "secret scan failed" in capsys.readouterr().err

    def test_provenance_rejects_working_tree_modules(self, tmp_path):
        work = tmp_path / "work"
        (work / "pyi" / "iFlowCore").mkdir(parents=True)
        st = bc.Stage(tmp_path / "stage", "linux", "production", __version__)
        (work / "pyi" / "iFlowCore" / "Analysis-00.toc").write_text(
            f"[('core.money', '{ROOT / 'core' / 'money.py'}', 'PYMODULE')]")
        with pytest.raises(bc.BuildError, match="working tree"):
            pdesk.check_provenance(work, st)
        (work / "pyi" / "iFlowCore" / "Analysis-00.toc").write_text(
            f"[('core.money', '{st.path / 'core' / 'money.py'}', 'PYMODULE')]")
        assert pdesk.check_provenance(work, st) == 1

    def test_desktop_entry(self):
        plan, _ = pdesk.parse(["--target", "linux", "--dry-run"])
        entry = pdesk.desktop_entry(plan, "/usr/bin/iflowcore", None)
        assert "Exec=/usr/bin/iflowcore" in entry and "Categories=Office;Finance;" in entry
        assert "Name=iFlow Core\n" in entry and "StartupWMClass=dev.iflowcore.app" in entry
        console, _ = pdesk.parse(["--app", "console", "--target", "linux", "--dry-run"])
        entry = pdesk.desktop_entry(console, "/usr/bin/iflowcore-admin", None)
        assert "Name=iFlow Core Admin Console" in entry and "StartupWMClass=dev.iflowcore.admin" in entry

    def test_console_stage_bundles_the_admin_service_only(self, tmp_path):
        st = bc.stage("linux", app=bc.APPS["console"], out=tmp_path / "stage")
        files = set(st.files)
        assert "admin_console.py" in files and "iflow_ai/admin.py" in files and "iflow_ai/format_publisher.py" in files
        assert "main.py" not in files and not any(f.startswith(("services/", "api/", "infra/")) for f in files)
        assert json.loads((st.path / "_build_info.json").read_text())["app"] == "console"
        assert not bc.scan_tree(st.path)                                           # no secrets staged

    def test_the_signing_key_value_is_scanned_for(self, tmp_path, monkeypatch):
        key = tmp_path / "vendor-signing.key"
        key.write_text("ab" * 32 + "\n")
        monkeypatch.setattr(bc, "SECRET_FILES", (key,))
        leak = tmp_path / "bundle"
        leak.mkdir()
        (leak / "blob.bin").write_bytes(b"x" + bytes.fromhex("ab" * 32) + b"y")
        assert any("vendor-signing.key" in f.what for f in bc.scan_tree(leak, binary=True))


# --- mobile packaging ----------------------------------------------------------------------------


class TestPackageMobile:
    def test_defaults_and_python_pin(self):
        plan, _ = pmob.parse(["android"])
        assert plan.fmt == "aab" and plan.python == f"{sys.version_info.major}.{sys.version_info.minor}"
        plan, _ = pmob.parse(["ios"])
        assert plan.fmt == "ipa"

    @pytest.mark.parametrize("argv", [["android", "--format", "ipa"], ["android", "--split-per-abi"],
                                      ["android", "--team-id", "X"], ["android", "--python-version", "2.7"]])
    def test_bad_arguments(self, argv):
        with pytest.raises(SystemExit):
            pmob.parse(argv)

    def test_build_command_never_carries_passwords(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLET_ANDROID_SIGNING_KEY_STORE_PASSWORD", "store-pass-123456")
        plan, _ = pmob.parse(["android", "--format", "apk", "--split-per-abi"])
        cmd = pmob.build_command(plan, tmp_path, tmp_path / "out")
        assert cmd[1:3] == ["build", "apk"] and "--split-per-abi" in cmd
        assert "--python-version" in cmd and "--build-number" in cmd
        assert not any("password" in c.lower() or "store-pass" in c for c in cmd)

    def test_ios_signing_names_pass_through(self, tmp_path):
        plan, _ = pmob.parse(["ios", "--team-id", "ABCDE12345", "--export-method", "app-store-connect"])
        cmd = pmob.build_command(plan, tmp_path, tmp_path / "out")
        assert cmd[cmd.index("--ios-team-id") + 1] == "ABCDE12345"
        assert cmd[cmd.index("--ios-export-method") + 1] == "app-store-connect"

    def test_keystore_inside_project_is_refused(self, monkeypatch, tmp_path):
        inside = ROOT / "tests" / "fixtures"
        keystore = next((p for p in inside.rglob("*") if p.is_file()), None)
        if keystore is None:
            pytest.skip("no fixture file to stand in for a keystore")
        monkeypatch.setenv("FLET_ANDROID_SIGNING_KEY_STORE", str(keystore))
        plan, _ = pmob.parse(["android"])
        with pytest.raises(bc.BuildError, match="outside the project"):
            pmob.check_signing(plan)

    def test_release_signing_needs_all_env(self, monkeypatch, tmp_path):
        ks = tmp_path / "upload.jks"
        ks.write_bytes(b"\0")
        monkeypatch.setenv("FLET_ANDROID_SIGNING_KEY_STORE", str(ks))
        for k in pmob.ANDROID_SIGNING_ENV[1:]:
            monkeypatch.delenv(k, raising=False)
        plan, _ = pmob.parse(["android"])
        with pytest.raises(bc.BuildError, match="FLET_ANDROID_SIGNING_KEY_ALIAS"):
            pmob.check_signing(plan)
        for k in pmob.ANDROID_SIGNING_ENV[1:]:
            monkeypatch.setenv(k, "value-from-ci-secrets")
        pmob.check_signing(plan)
        assert any("passwords from the environment" in s for s in plan.steps)

    def test_unsigned_release_warns(self, monkeypatch):
        monkeypatch.delenv("FLET_ANDROID_SIGNING_KEY_STORE", raising=False)
        plan, _ = pmob.parse(["android"])
        pmob.check_signing(plan)
        assert any("debug-signed" in w for w in plan.warnings)

    def test_compile_stage_catches_syntax_errors(self, tmp_path):
        (tmp_path / "ok.py").write_text("x = 1\n")
        assert pmob.compile_stage(tmp_path) == 1
        (tmp_path / "bad.py").write_text("def f(:\n")
        with pytest.raises(bc.BuildError, match="bad.py"):
            pmob.compile_stage(tmp_path)

    @pytest.mark.parametrize("platform", ["android", "ios"])
    def test_sqlite_fallback_without_sqlcipher(self, build_tmp, platform):
        """The staged app runs as android/ios with sqlcipher3 unimportable."""
        st = bc.stage(platform, out=build_tmp / "stage")
        plan, _ = pmob.parse([platform])
        report = pmob.fallback_selftest(plan, st.path)
        checks = {c["name"]: c for c in report["checks"]}
        assert report["ok"]
        assert checks["encryption"]["status"] == "skip"
        assert "'platform' storage" in checks["mobile_storage"]["detail"]
        assert checks["migrations"]["status"] == "pass" and checks["ui_startup"]["status"] == "pass"

    def test_dry_run(self, build_tmp, capsys):
        assert pmob.main(["android", "--dry-run", "--skip-fallback-check"]) == 0
        out = capsys.readouterr().out
        assert "flet build aab" in out and "--python-version" in out and "byte-compile cleanly" in out

    def test_collect_renames_artifacts(self, build_tmp):
        out = build_tmp / "out"
        (out / "apk").mkdir(parents=True)
        (out / "apk" / "app-release.apk").write_bytes(b"PK")
        plan, _ = pmob.parse(["android", "--format", "apk"])
        [artifact] = pmob.collect(plan, out)
        assert artifact.name == f"iFlowCore-{__version__}-android.apk"


# --- end-to-end validation -----------------------------------------------------------------------


class TestValidateDeployment:
    def test_stage_checks(self, build_tmp):
        assert "secret scan clean" in vdep.v_stage("windows")

    def test_dist_checksums(self, build_tmp, monkeypatch):
        dist = build_tmp / "dist"
        dist.mkdir()
        monkeypatch.setattr(vdep, "DIST_DIR", dist)
        art = dist / "iFlowCore-test.tar.gz"
        art.write_bytes(b"artifact")
        bc.write_manifest([art], target="linux", version="0", channel="production", backend="pyinstaller",
                          selftest=None, out=dist)
        assert "checksums match" in vdep.v_dist()
        art.write_bytes(b"tampered")
        with pytest.raises(bc.BuildError, match="checksum"):
            vdep.v_dist()

    def test_binary_must_be_frozen(self, tmp_path):
        with pytest.raises(bc.BuildError, match="not found"):
            vdep.v_binary(tmp_path / "missing")
