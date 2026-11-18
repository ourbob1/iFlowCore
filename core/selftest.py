"""
Deployment self-test: proves a build (source checkout *or* packaged binary) works end to end.

    python main.py --selftest            # human-readable
    python main.py --selftest --json     # machine-readable (CI, packaging scripts)
    iFlowCore.exe --selftest --json --out=report.json   # windowed builds without a console
    ./iFlowCore --selftest                # inside a PyInstaller / flet build binary

It runs entirely in a temporary directory, never touches the user's data,
needs no network, and uses throwaway keys generated on the spot (nothing is
hardcoded). Checks:

  runtime        Python version, platform detection, build channel
  assets         schema.sql, contiguous migrations, tax tables, statement profiles bundled
  db_init        fresh database reaches the latest schema; integrity + foreign keys OK
  migrations     every historical version (v1 … vN-1) upgrades to the same schema as a fresh
                 install, with data preserved; a database newer than the app is refused
  encryption     SQLCipher: create → reopen with key → wrong key refused → file not plaintext;
                 plaintext → SQLCipher upgrade keeps every row (desktop: required)
  mobile_storage platform-protected fallback + AES-GCM field sealing round-trip
  security       production defaults: encryption required, simulated licenses refused,
                 build-info file carries metadata only (no keys)
  ui_startup     the app shell builds every route at phone and desktop widths without errors

Status per check: pass / fail / skip (skip = not applicable to this build).
"""

from __future__ import annotations

import json
import platform as py_platform
import sqlite3
import sys
import tempfile
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

HEADER_PLAINTEXT = b"SQLite format 3\x00"
ROUTES = ("/", "/finances", "/fleet", "/radar", "/insights", "/health", "/settings", "/plans")


@dataclass
class Check:
    name: str
    status: str = "pass"             # pass | fail | skip
    detail: str = ""
    ms: int = 0


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    python: str = py_platform.python_version()
    system: str = f"{py_platform.system()} {py_platform.machine()}"
    frozen: bool = bool(getattr(sys, "frozen", False))

    @property
    def ok(self) -> bool:
        return all(c.status != "fail" for c in self.checks)

    def to_json(self) -> str:
        return json.dumps({"ok": self.ok, "python": self.python, "system": self.system, "frozen": self.frozen,
                           "checks": [asdict(c) for c in self.checks]}, indent=2)

    def to_text(self) -> str:
        mark = {"pass": "PASS", "fail": "FAIL", "skip": "skip"}
        lines = [f"iFlow Core self-test · Python {self.python} · {self.system}" + (" · frozen" if self.frozen else "")]
        for c in self.checks:
            lines.append(f"  [{mark[c.status]}] {c.name:<15} {c.detail}  ({c.ms} ms)")
        lines.append("RESULT: " + ("OK" if self.ok else "FAILED"))
        return "\n".join(lines)


class Skip(Exception):
    pass


def _run(report: Report, name: str, fn: Callable[[Path], str], workdir: Path) -> None:
    start = time.perf_counter()
    check = Check(name)
    sub = workdir / name
    sub.mkdir(parents=True, exist_ok=True)
    try:
        check.detail = fn(sub) or ""
    except Skip as exc:
        check.status, check.detail = "skip", str(exc)
    except Exception as exc:                  # a failed check must never stop the others
        check.status = "fail"
        check.detail = f"{type(exc).__name__}: {exc}"
        tb = traceback.extract_tb(exc.__traceback__)
        if tb:
            check.detail += f" [{Path(tb[-1].filename).name}:{tb[-1].lineno}]"
    check.ms = int((time.perf_counter() - start) * 1000)
    report.checks.append(check)


# --- checks -------------------------------------------------------------------------------------


def check_runtime(_: Path) -> str:
    from core import __version__
    from core.buildinfo import load
    from core.platform import detect_platform

    if sys.version_info < (3, 10):
        raise RuntimeError(f"Python 3.10+ required, found {py_platform.python_version()}")
    info = load()
    return (f"v{__version__} · platform {detect_platform()} · channel {info.channel}"
            + (f" ({info.target}, {info.backend})" if info.packaged else ""))


def check_timezones(_: Path) -> str:
    """zoneinfo needs a timezone database; phones and browsers have no usable system copy, so the
    bundled `tzdata` package must be present (a missing one broke the first Android build)."""
    from core.device_settings import guess_timezone
    from core.timeutil import zone

    for name in ("UTC", "America/New_York", "Europe/London", "Asia/Tokyo"):
        zone(name)
    guessed = guess_timezone()
    zone(guessed)
    try:
        import tzdata  # noqa: F401
        source = "bundled tzdata"
    except ImportError:
        source = "system database"
    return f"IANA zones load ({source}); this device: {guessed}"


def check_assets(_: Path) -> str:
    from core.financial.ingestion import BUILTIN_PROFILES
    from core.financial.tax import TaxTableRegistry
    from database.migrations import MIGRATIONS_DIR, SCHEMA_FILE, discover_migrations

    if not SCHEMA_FILE.is_file():
        raise FileNotFoundError(f"schema.sql not bundled (looked in {SCHEMA_FILE.parent.name}/)")
    migrations = discover_migrations()                      # also validates contiguity
    if len(migrations) < 2 or not MIGRATIONS_DIR.is_dir():
        raise FileNotFoundError("migration files not bundled")
    from ui.brand import ASSETS_DIR, EMBLEM, LOGO, LOGO_DARK

    missing = [n for n in (LOGO, LOGO_DARK, EMBLEM) if not (ASSETS_DIR / n).is_file()]
    if missing:
        raise FileNotFoundError(f"brand images not bundled: {', '.join(missing)}")
    registry = TaxTableRegistry.load()
    if not registry.has("US"):
        raise FileNotFoundError("federal tax tables not bundled")
    if not BUILTIN_PROFILES:
        raise RuntimeError("no built-in statement profiles")
    return (f"schema v1 + migrations 0002–{migrations[-1].version:04d} · {len(registry.jurisdictions())} tax "
            f"jurisdictions · {len(BUILTIN_PROFILES)} statement profiles")


def _schema(conn: Any) -> list[tuple]:
    return sorted(tuple(r) for r in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))


def _health(conn: Any) -> None:
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("integrity_check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("foreign_key_check found violations")


def check_db_init(tmp: Path) -> str:
    from database.connection import Database
    from database.migrations import discover_migrations

    latest = discover_migrations()[-1].version
    db = Database(tmp / "fresh.db", driver="sqlite")
    try:
        version = db.initialize()
        with db.connection() as conn:
            _health(conn)
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
    finally:
        db.close()
    if version != latest:
        raise RuntimeError(f"fresh database at v{version}, expected v{latest}")
    if fk != 1:
        raise RuntimeError("foreign keys are not enforced")
    return f"v{version} · {tables} tables · journal {journal} · foreign keys on"


def check_migrations(tmp: Path) -> str:
    from database.migrations import MigrationError, discover_migrations, migrate

    migrations = discover_migrations()
    latest = migrations[-1].version

    def connect(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    fresh = connect(tmp / "fresh.db")
    migrate(fresh, sqlite3.complete_statement, migrations)
    expected = _schema(fresh)
    fresh.close()

    for start in range(1, latest):
        conn = connect(tmp / f"from_v{start}.db")
        migrate(conn, sqlite3.complete_statement, migrations[:start])
        # a row that must survive every later migration
        conn.execute("INSERT INTO platforms (id, name, kind) VALUES "
                     "('11111111-1111-4111-8111-111111111111', 'Selftest', 'other')")
        version = migrate(conn, sqlite3.complete_statement, migrations)
        if version != latest:
            raise RuntimeError(f"v{start} upgraded to v{version}, expected v{latest}")
        if _schema(conn) != expected:
            raise RuntimeError(f"schema after upgrading from v{start} differs from a fresh install")
        if conn.execute("SELECT count(*) FROM platforms WHERE name = 'Selftest'").fetchone()[0] != 1:
            raise RuntimeError(f"data lost upgrading from v{start}")
        _health(conn)
        conn.close()

    future = connect(tmp / "future.db")
    future.execute(f"PRAGMA user_version = {latest + 1}")
    try:
        migrate(future, sqlite3.complete_statement, migrations)
    except MigrationError:
        pass
    else:
        raise RuntimeError("a database from a newer app version was not refused")
    finally:
        future.close()
    return f"upgrades from v1…v{latest - 1} all match a fresh v{latest} (schema, integrity, data); newer schema refused"


def _desktop_requires_sqlcipher() -> bool:
    from core.config import load_config

    try:
        cfg = load_config({"IFLOW_DATA_DIR": tempfile.gettempdir()})
    except Exception:
        return False
    return cfg.requires_sqlcipher


def check_encryption(tmp: Path) -> str:
    from core.security import generate_raw_key
    from database.connection import Database, InvalidKeyError, load_sqlcipher_driver
    from database.encryption import is_plaintext_sqlite, migrate_to_sqlcipher

    driver = load_sqlcipher_driver()
    if driver is None:
        from core.platform import detect_platform, is_mobile

        if is_mobile(detect_platform()):
            raise Skip("no SQLCipher driver on mobile: platform-protected storage (see mobile_storage)")
        if _desktop_requires_sqlcipher():
            raise RuntimeError("SQLCipher driver missing, but this build requires encryption")
        raise Skip("SQLCipher driver not installed (development build)")

    key, wrong = generate_raw_key(), generate_raw_key()             # throwaway, never stored
    path = tmp / "enc.db"
    db = Database(path, key=key, driver="sqlcipher")
    db.initialize()
    with db.transaction() as conn:
        conn.execute("INSERT INTO platforms (id, name, kind) VALUES "
                     "('22222222-2222-4222-8222-222222222222', 'Encrypted', 'other')")
        cipher = conn.execute("PRAGMA cipher_version").fetchone()
    db.close()
    if path.read_bytes()[:16] == HEADER_PLAINTEXT:
        raise RuntimeError("database file is plaintext despite a key")
    again = Database(path, key=key, driver="sqlcipher")
    again.initialize()
    with again.connection() as conn:
        if conn.execute("SELECT count(*) FROM platforms WHERE name = 'Encrypted'").fetchone()[0] != 1:
            raise RuntimeError("row not readable after reopening with the key")
    again.close()
    bad = Database(path, key=wrong, driver="sqlcipher")
    quiet = driver.connect(":memory:")               # SQLCipher logs the (expected) HMAC failure natively
    level = quiet.execute("PRAGMA cipher_log_level").fetchone()[0]
    quiet.execute("PRAGMA cipher_log_level = NONE")
    try:
        bad.initialize()
    except InvalidKeyError:
        pass
    else:
        raise RuntimeError("a wrong key opened the database")
    finally:
        bad.close()
        quiet.execute(f"PRAGMA cipher_log_level = {level}")
        quiet.close()

    plain = tmp / "legacy.db"
    legacy = Database(plain, driver="sqlite")
    legacy.initialize()
    with legacy.transaction() as conn:
        conn.execute("INSERT INTO platforms (id, name, kind) VALUES "
                     "('33333333-3333-4333-8333-333333333333', 'Legacy', 'other')")
    legacy.close()
    result = migrate_to_sqlcipher(plain, key)
    if is_plaintext_sqlite(plain):
        raise RuntimeError("plaintext → SQLCipher upgrade left a plaintext file")
    upgraded = Database(plain, key=key, driver="sqlcipher")
    upgraded.initialize()
    with upgraded.connection() as conn:
        if conn.execute("SELECT count(*) FROM platforms WHERE name = 'Legacy'").fetchone()[0] != 1:
            raise RuntimeError("row lost in plaintext → SQLCipher upgrade")
    upgraded.close()
    version = cipher[0] if cipher else "?"
    return (f"SQLCipher {version}: key handshake, wrong key refused, file encrypted; plaintext upgrade kept "
            f"{result.rows} row(s) in {result.tables} tables at v{result.user_version}")


def check_mobile_storage(tmp: Path) -> str:
    from core.crypto import FieldSealer, SealingError
    from core.security import generate_raw_key
    from database.connection import Database

    from core.platform import detect_platform, is_mobile

    detected = detect_platform()
    target = detected if is_mobile(detected) else "android"
    key = generate_raw_key()
    db = Database(tmp / "mobile.db", key=key, platform=target)
    db.initialize()
    protection = db.protection
    db.close()
    if protection not in ("sqlcipher", "platform"):
        raise RuntimeError(f"unexpected mobile protection '{protection}'")
    sealer = FieldSealer(key)
    token = sealer.seal("secret-value", context="selftest.field")
    if "secret-value" in token or sealer.open(token, context="selftest.field") != "secret-value":
        raise RuntimeError("field sealing round-trip failed")
    try:
        FieldSealer(generate_raw_key()).open(token, context="selftest.field")
    except SealingError:
        pass
    else:
        raise RuntimeError("a different device key opened a sealed field")
    label = {"android": "Android", "ios": "iOS"}.get(target, target)
    return f"{label} path opens as '{protection}' storage; AES-GCM sealing bound to the device key"


BUILD_INFO_KEYS = {"channel", "version", "target", "built_at", "backend", "app"}


def check_security(tmp: Path) -> str:
    from core.buildinfo import load
    from core.config import load_config
    from core.licensing import LicenseManager

    info = load()
    prod = load_config({"IFLOW_DATA_DIR": str(tmp), "IFLOW_ENV": "production", "IFLOW_PLATFORM": "linux"})
    if not prod.require_encryption or not prod.requires_sqlcipher:
        raise RuntimeError("production desktop config doesn't require SQLCipher")

    class _Store:
        def load(self) -> Any:
            return type("S", (), {"license_token": None, "license_last_seen": None})()

        def update(self, **_: Any) -> None:
            pass

    if LicenseManager(_Store(), allow_simulation=not prod.is_production).allow_simulation:
        raise RuntimeError("simulated licenses allowed in production")
    notes = ["production requires SQLCipher", "simulated licenses refused"]
    from core.buildinfo import FILENAME, candidate_dirs

    for d in candidate_dirs():
        path = d / FILENAME
        if path.is_file():
            extra = set(json.loads(path.read_text(encoding="utf-8"))) - BUILD_INFO_KEYS
            if extra:
                raise RuntimeError(f"{FILENAME} has unexpected keys: {', '.join(sorted(extra))}")
    if info.packaged:
        packaged = load_config({"IFLOW_DATA_DIR": str(tmp)})
        notes.append(f"this build defaults to '{packaged.environment.value}'")
        if info.channel in ("production", "beta") and not packaged.is_production:
            raise RuntimeError("a production build doesn't default to production")
    return " · ".join(notes)


class _StubPage:
    """Just enough of flet.Page to build the shell headlessly."""

    def __init__(self, width: int, platform: str) -> None:
        import flet as ft

        self.width, self.platform, self.route = width, platform, "/"
        self.views: list[Any] = []
        self.services: list[Any] = []
        self.theme_mode = None
        self.platform_brightness = ft.Brightness.LIGHT
        self.title = ""
        self.theme = self.dark_theme = None
        self.dialogs: list[Any] = []

    def update(self) -> None:
        pass

    def show_dialog(self, dialog: Any) -> None:
        self.dialogs.append(dialog)

    def pop_dialog(self) -> None:
        pass

    def run_thread(self, fn: Any, *args: Any) -> None:
        fn(*args)

    def run_task(self, *args: Any, **kwargs: Any) -> None:
        pass


def check_ui_startup(tmp: Path) -> str:
    from datetime import datetime, timezone

    from ui.app import IFlowCoreApp

    now = datetime.now(timezone.utc)
    shown = 0
    for width, plat in ((390, "android"), (1280, "linux")):
        page = _StubPage(width, plat)
        env = {"IFLOW_DATA_DIR": str(tmp / plat), "IFLOW_ENV": "test", "IFLOW_PLATFORM": "linux",
               "IFLOW_FORMATS_URL": "off"}                       # a self-test never goes online
        app = IFlowCoreApp(page, env=env, clock=lambda: now, services={"motion": None})
        app.services.pop("motion")
        app.start()
        if not app.state.unlocked:
            raise RuntimeError(f"app didn't open its test database ({app.last_error})")
        try:
            for route in ROUTES:
                app.navigate(route)
                if app.last_error:
                    raise RuntimeError(f"{route} at {width}px: {app.last_error}")
                if not page.views:
                    raise RuntimeError(f"{route} at {width}px rendered nothing")
                shown += 1
        finally:
            app.state.lock()
    return f"{shown} screens rendered ({len(ROUTES)} routes × phone + desktop)"


CHECKS: tuple[tuple[str, Callable[[Path], str]], ...] = (
    ("runtime", check_runtime),
    ("timezones", check_timezones),
    ("assets", check_assets),
    ("db_init", check_db_init),
    ("migrations", check_migrations),
    ("encryption", check_encryption),
    ("mobile_storage", check_mobile_storage),
    ("security", check_security),
    ("ui_startup", check_ui_startup),
)


def run(only: list[str] | None = None) -> Report:
    import logging

    report = Report()
    logging.disable(logging.WARNING)            # dev-mode warnings aren't results
    try:
        with tempfile.TemporaryDirectory(prefix="iflowcore-selftest-") as tmp:
            for name, fn in CHECKS:
                if only and name not in only:
                    continue
                _run(report, name, fn, Path(tmp))
    finally:
        logging.disable(logging.NOTSET)
    return report


def main(argv: list[str]) -> int:
    only = [a.split("=", 1)[1] for a in argv if a.startswith("--only=")]
    out = [a.split("=", 1)[1] for a in argv if a.startswith("--out=")]
    report = run(only[0].split(",") if only else None)
    text = report.to_json() if "--json" in argv else report.to_text()
    if out:              # windowed builds (Windows --noconsole) have no stdout
        Path(out[0]).write_text(text + "\n", encoding="utf-8")
    elif sys.stdout is not None:
        print(text)
    return 0 if report.ok else 1
