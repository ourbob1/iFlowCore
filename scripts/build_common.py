"""
Shared packaging helpers: clean staging, secret scanning, pyproject validation, build manifests.

Staging
-------
Builds never run from the working tree. `stage()` copies an explicit allowlist
(main.py, core/, database/, ui/, assets/ if present, pyproject.toml) into
build/stage/<target>/, skipping caches, and refuses anything that looks like
user data or a secret (databases, .env files, keys, logs). It then writes
_build_info.json (channel, version, target: never a secret) so the packaged
app defaults to production (core/buildinfo.py).

Secret scanning
---------------
`scan_tree()` runs on the staged app *and* on the finished artifacts. It
fails the build on: private-key blocks, well-known token formats, hardcoded
credential assignments, database/keystore/.env files, and the build
machine's own secret environment values (IFLOW_DB_KEY, AI/broker tokens),
so a key exported in the build shell can never end up inside a binary.

Encryption keys are never bundled: desktop databases are opened with a
passphrase entered at unlock (or IFLOW_DB_KEY at runtime); mobile
databases use a random key created on first launch in the platform keystore.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"
APP_NAME = "iFlowCore"
PRODUCT = "iFlow Core"


@dataclass(frozen=True)
class AppSpec:
    """One desktop app built from this repo."""
    key: str
    name: str                                    # binary / bundle name
    product: str                                 # human name (menus, installers)
    entry: str                                   # script at the stage root
    deb: str                                     # Linux package name
    bundle_id: str
    comment: str
    allowlist: tuple[str, ...]
    extra: tuple[tuple[str, str], ...] = ()      # (repo path, stage path) copied in addition


APPS: dict[str, AppSpec] = {
    "core": AppSpec("core", APP_NAME, PRODUCT, "main.py", "iflowcore", "dev.iflowcore.app",
                    "Local-first command center and finances for gig workers",
                    ("main.py", "core", "database", "ui", "assets", "pyproject.toml")),
    # The operator app: the console UI plus its built-in admin service (services/ai/iflow_ai, no AI models)
    "console": AppSpec("console", "iFlowCoreAdmin", "iFlow Core Admin Console", "admin_console.py", "iflowcore-admin",
                       "dev.iflowcore.admin", "Operator console: users, licenses, statement formats, crash reports",
                       ("admin_console.py", "core", "database", "ui", "assets", "pyproject.toml"),
                       (("services/ai/iflow_ai", "iflow_ai"),)),
}
# Files whose *contents* must never appear in a build (checked like secret env values). The operator's
# vendor signing key sits on the build machine; this makes sure it can't slip into an artifact.
SECRET_FILES = (Path.home() / ".local" / "share" / "iflow-ai" / "vendor-signing.key",)

APP_ALLOWLIST = ("main.py", "core", "database", "ui", "assets", "pyproject.toml")
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}
# Never allowed inside a package (user data, secrets, signing material).
FORBIDDEN_NAMES = re.compile(
    r"(?i)(^\.env(\..*)?$|\.db$|\.db-wal$|\.db-shm$|\.sqlite3?$|^preferences\.json$|\.pem$|\.p12$|\.pfx$|"
    r"\.jks$|\.keystore$|\.mobileprovision$|^id_(rsa|ed25519)|\.key$|^errors\.jsonl|^telemetry-queue)")
# In built artifacts, third-party CA bundles (certifi's cacert.pem) are expected; user data,
# keystores and signing material never are.
ARTIFACT_FORBIDDEN_NAMES = re.compile(
    r"(?i)(^\.env(\..*)?$|\.db$|\.db-wal$|\.db-shm$|\.sqlite3?$|^preferences\.json$|\.p12$|\.pfx$|"
    r"\.jks$|\.keystore$|\.mobileprovision$|^id_(rsa|ed25519)$|^errors\.jsonl|^telemetry-queue)")
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    # header + a real base64 body: library code (OpenSSL) contains bare PEM header strings
    ("private key block", re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\r?\n"
                                     rb"[A-Za-z0-9+/=\r\n]{64,}")),
    ("AWS access key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Stripe live key", re.compile(rb"\b[sr]k_live_[A-Za-z0-9]{16,}\b")),
    ("hardcoded credential", re.compile(
        rb"(?i)\b(?:db_key|passphrase|password|secret|api_key|auth_token)\s*[:=]\s*[\"']"
        rb"(?-i:(?![A-Z][A-Z0-9_]+[\"']))[^\"'\s]{12,}[\"']")),   # ENV_NAME values are references, not secrets
)
_APP_SECRETS = ("DB_KEY", "AI_TOKEN", "DATAONE_AUTH_CODE", "TICKETMASTER_KEY", "OPENSKY_CLIENT_SECRET")
SECRET_ENV_VARS = (*(f"IFLOW_{n}" for n in _APP_SECRETS),
                   *(f"RUBYSOHO_{n}" for n in _APP_SECRETS),            # pre-rebrand names, still honoured
                   "FLET_ANDROID_SIGNING_KEY_STORE_PASSWORD", "FLET_ANDROID_SIGNING_KEY_PASSWORD",
                   "RS_AI_LICENSE_SIGNING_KEY", "RS_AI_BOOTSTRAP_PASSWORD")
APP_ENV_PREFIXES = ("IFLOW_", "RUBYSOHO_")
SCANNED_SUFFIXES = {".py", ".json", ".toml", ".txt", ".cfg", ".ini", ".yaml", ".yml", ".sql", ".md", ".plist", ""}

DESKTOP_TARGETS = ("linux", "macos", "windows")
MOBILE_TARGETS = ("android", "ios")


class BuildError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def app_version() -> str:
    sys.path.insert(0, str(ROOT))
    from core import __version__

    return __version__


def build_number(version: str) -> int:
    """Monotonic integer for app stores: 1.2.3 -> 10203 (versionCode / CFBundleVersion)."""
    parts = [int(x) for x in (version.split(".") + ["0", "0"])[:3]]
    return parts[0] * 10000 + parts[1] * 100 + parts[2]


def host_target() -> str:
    return {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)


# --- pyproject validation --------------------------------------------------------------------


def _deps(section: dict | None) -> list[str]:
    return [d.lower() for d in (section or {}).get("dependencies", [])]


def validate_pyproject(targets: Iterable[str] = DESKTOP_TARGETS + MOBILE_TARGETS, data: dict | None = None) -> list[str]:
    """Problems with the build configuration (empty list = OK)."""
    data = data if data is not None else project()
    problems: list[str] = []
    proj, flet = data.get("project", {}), data.get("tool", {}).get("flet", {})
    base = _deps(proj)
    if proj.get("version") != app_version():
        problems.append(f"pyproject version {proj.get('version')} != core.__version__ {app_version()}")
    if any("sqlcipher" in d for d in base):
        problems.append("SQLCipher must not be a base dependency (no mobile wheel exists); use [tool.flet.<desktop>]")
    tz = [d for d in base if d.startswith("tzdata")]
    if not tz or any(";" in d for d in tz):
        problems.append("tzdata must be an unconditional dependency (Android, iOS, Windows and browsers "
                        "have no timezone database zoneinfo can read)")
    if any("flet-secure-storage" in d for d in base):
        problems.append("flet-secure-storage belongs to the mobile targets only")
    for key in ("org", "product", "company"):
        if not flet.get(key):
            problems.append(f"[tool.flet] {key} is missing")
    for t in targets:
        deps = _deps(flet.get(t))
        if t in DESKTOP_TARGETS and not any("sqlcipher3" in d for d in deps):
            problems.append(f"[tool.flet.{t}] must add a SQLCipher driver (desktop encryption is mandatory)")
        if t in MOBILE_TARGETS:
            if not any(d.startswith("flet-secure-storage") for d in deps):
                problems.append(f"[tool.flet.{t}] must add flet-secure-storage (device key in the platform keystore)")
            if any("sqlcipher" in d for d in deps):
                problems.append(f"[tool.flet.{t}] must not add SQLCipher (no wheel for Flet's mobile runtime)")
    if "android" in targets:
        perms = (flet.get("android") or {}).get("permission", {})
        for p in ("android.permission.ACCESS_FINE_LOCATION", "android.permission.FOREGROUND_SERVICE_LOCATION",
                  "android.permission.INTERNET"):
            if not perms.get(p):
                problems.append(f"Android permission {p} missing")
        signing = (flet.get("android") or {}).get("signing", {})
        if any("password" in k.lower() for k in signing):
            problems.append("Android signing passwords must come from environment variables, never pyproject.toml")
    if "ios" in targets:
        info = (flet.get("ios") or {}).get("info", {})
        for k in ("NSLocationWhenInUseUsageDescription", "NSLocationAlwaysAndWhenInUseUsageDescription"):
            if not info.get(k):
                problems.append(f"iOS Info.plist key {k} missing")
    excludes = (flet.get("app") or {}).get("exclude", [])
    for must in ("venv", "tests", "services", "build", "dist"):
        if must not in excludes:
            problems.append(f"[tool.flet.app] exclude should list '{must}' (for plain `flet build` runs)")
    return problems


# --- staging -------------------------------------------------------------------------------------


@dataclass
class Stage:
    path: Path
    target: str
    channel: str
    version: str
    files: list[str] = field(default_factory=list)


def _copy(src: Path, dst: Path, rel_root: Path, files: list[str]) -> None:
    if src.is_dir():
        if src.name in SKIP_DIRS:
            return
        for child in sorted(src.iterdir()):
            _copy(child, dst / child.name, rel_root, files)
        return
    rel = src.relative_to(rel_root).as_posix()
    if src.suffix in SKIP_SUFFIXES:
        return
    if FORBIDDEN_NAMES.search(src.name):
        raise BuildError(f"refusing to package {rel}: looks like user data or a secret")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    files.append(rel)


def _remove_tree(path: Path) -> None:
    """Delete a previous stage. A tool that outlived its build (a Gradle daemon still flushing into
    build/) can make rmtree fail; then the old stage is moved aside and removed best-effort."""
    try:
        shutil.rmtree(path)
    except OSError:
        trash = path.with_name(f".trash-{path.name}-{_dt.datetime.now():%Y%m%d%H%M%S%f}")
        path.rename(trash)
        shutil.rmtree(trash, ignore_errors=True)
        log(f"previous stage was busy; moved aside to {trash.name}")


def stage(target: str, *, channel: str = "production", backend: str = "flet", root: Path = ROOT,
          out: Path | None = None, app: AppSpec | None = None) -> Stage:
    if channel not in ("production", "beta", "qa", "dev"):
        raise BuildError(f"unknown channel '{channel}'")
    version = app_version()
    dest = out or (BUILD_DIR / "stage" / target)
    if dest.exists():
        _remove_tree(dest)
    dest.mkdir(parents=True)
    files: list[str] = []
    app = app or APPS["core"]
    for name in app.allowlist:
        src = root / name
        if src.exists():
            _copy(src, dest / name, root, files)
    for src_rel, dst_rel in app.extra:
        extra: list[str] = []
        _copy(root / src_rel, dest / dst_rel, root, extra)
        files += [dst_rel + r[len(src_rel):] for r in extra]            # paths as they are in the stage
    info = {"channel": channel, "version": version, "target": target, "backend": backend, "app": app.key,
            "built_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    (dest / "_build_info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    files.append("_build_info.json")
    log(f"staged {len(files)} files for {target} ({channel}) in {dest.relative_to(root) if dest.is_relative_to(root) else dest}")
    return Stage(dest, target, channel, version, files)


# --- secret scanning -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    path: str
    what: str


def scan_tree(path: Path, *, env: dict[str, str] | None = None, binary: bool = False,
              max_bytes: int = 64 * 1024 * 1024) -> list[Finding]:
    """Look for secrets under `path`. Text files are always scanned; `binary=True` also scans
    everything else (built artifacts) for private keys and the build machine's secret values."""
    env = os.environ if env is None else env
    env_secrets = [(k, env[k].encode()) for k in SECRET_ENV_VARS if env.get(k) and len(env[k]) >= 8]
    for secret_file in SECRET_FILES:
        try:
            value = secret_file.read_bytes().strip()
        except OSError:
            continue
        if len(value) >= 16:
            env_secrets += [(secret_file.name, value), (secret_file.name + " (bytes)", bytes.fromhex(value.decode()))
                            if _is_hex(value) else (secret_file.name, value)]
    findings: list[Finding] = []
    files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
    for f in files:
        rel = f.name if path.is_file() else f.relative_to(path).as_posix()
        if (ARTIFACT_FORBIDDEN_NAMES if binary else FORBIDDEN_NAMES).search(f.name):
            findings.append(Finding(rel, "user data / secret file"))
            continue
        is_text = f.suffix.lower() in SCANNED_SUFFIXES
        if not (is_text or binary) or f.stat().st_size > max_bytes:
            continue
        data = f.read_bytes()
        patterns = SECRET_PATTERNS if is_text else SECRET_PATTERNS[:1]
        for what, rx in patterns:
            if rx.search(data):
                findings.append(Finding(rel, what))
        for name, value in env_secrets:
            if value in data:
                findings.append(Finding(rel, f"value of ${name} from the build environment"))
    return findings


def _is_hex(value: bytes) -> bool:
    try:
        bytes.fromhex(value.decode())
        return True
    except ValueError:
        return False


ARCHIVE_SUFFIXES = {".zip", ".apk", ".aab", ".ipa", ".jar", ".whl"}


def scan_archive(path: Path, *, env: dict[str, str] | None = None, depth: int = 3) -> list[Finding]:
    """Scan a zip-based package (apk/aab/ipa/zip) *after extracting it*, recursing into nested
    archives (Flet mobile apps carry the Python app as an inner zip): compressed bytes hide secrets."""
    import tempfile
    import zipfile

    findings: list[Finding] = []
    with tempfile.TemporaryDirectory(prefix="iflowcore-scan-") as tmp:
        root = Path(tmp)
        try:
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    name = Path(info.filename)
                    if name.is_absolute() or ".." in name.parts:
                        findings.append(Finding(info.filename, "unsafe path inside archive"))
                        continue
                    zf.extract(info, root)
        except zipfile.BadZipFile:
            return scan_tree(path, env=env, binary=True)
        findings += [Finding(f"{path.name}!{f.path}", f.what) for f in scan_tree(root, env=env, binary=True)]
        if depth > 0:
            for inner in root.rglob("*"):
                if inner.is_file() and inner.suffix.lower() in ARCHIVE_SUFFIXES and zipfile.is_zipfile(inner):
                    for f in scan_archive(inner, env=env, depth=depth - 1):
                        findings.append(Finding(f"{path.name}!{inner.relative_to(root).as_posix()}!{f.path}", f.what))
    return findings


def require_clean(path: Path, *, binary: bool = False) -> None:
    if path.is_file() and path.suffix.lower() in ARCHIVE_SUFFIXES:
        findings = scan_archive(path)
    else:
        findings = scan_tree(path, binary=binary)
    if findings:
        lines = "\n".join(f"  {f.path}: {f.what}" for f in findings[:20])
        raise BuildError(f"secret scan failed ({len(findings)} finding(s)):\n{lines}")
    log(f"secret scan clean: {path.name}")


# --- manifest ------------------------------------------------------------------------------------


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(artifacts: list[Path], *, target: str, version: str, channel: str, backend: str,
                   selftest: dict | None, out: Path, app: AppSpec | None = None) -> Path:
    app = app or APPS["core"]
    manifest = {
        "product": app.product, "version": version, "channel": channel, "target": target, "backend": backend,
        "built_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "artifacts": [{"file": a.name, "bytes": a.stat().st_size, "sha256": sha256(a)} for a in artifacts if a.is_file()],
        "selftest": selftest,
    }
    path = out / f"{app.name}-{version}-{target}-manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path
