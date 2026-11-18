#!/usr/bin/env python3
"""
Package iFlow Core for Android and iOS with `flet build` (Flutter + Flet's mobile Python runtime).

    python scripts/package_mobile.py android                   # release .aab for Google Play
    python scripts/package_mobile.py android --format apk --split-per-abi
    python scripts/package_mobile.py ios --team-id ABCDE12345  # .ipa (macOS + Xcode only)
    python scripts/package_mobile.py android --dry-run         # checks + plan, no Flutter needed

Storage on mobile
-----------------
There is no SQLCipher wheel for Flet's mobile Python, so mobile targets deliberately *don't*
ship it ([tool.flet.android|ios].dependencies add only flet-secure-storage). The app detects
the missing driver and opens platform-protected SQLite (Android file-based encryption / iOS
Data Protection) with sensitive device settings sealed by AES-GCM under a random device key
kept in the Android Keystore / iOS Keychain (flet-secure-storage). No key is bundled.

Before building, this script proves that path works *without* SQLCipher: it re-runs the app's
self-test in a subprocess where `sqlcipher3` can't be imported and the platform is forced to
android/ios. A module that imports SQLCipher unconditionally, or a migration that only works on
SQLCipher, fails here instead of crashing on a phone.

Signing (never in pyproject.toml, never in the repo)
----------------------------------------------------
  Android  FLET_ANDROID_SIGNING_KEY_STORE (path, outside the project), FLET_ANDROID_SIGNING_KEY_ALIAS,
           FLET_ANDROID_SIGNING_KEY_STORE_PASSWORD, FLET_ANDROID_SIGNING_KEY_PASSWORD
  iOS      --team-id / --provisioning-profile / --signing-certificate (names, not secrets; the
           certificate itself stays in the macOS keychain)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_common import (  # noqa: E402
    APP_ENV_PREFIXES,
    APP_NAME,
    BUILD_DIR,
    DIST_DIR,
    ROOT,
    BuildError,
    app_version,
    build_number,
    log,
    project,
    require_clean,
    stage,
    validate_pyproject,
    write_manifest,
)

FORMATS = {"android": ("aab", "apk"), "ios": ("ipa", "ios-simulator")}
ANDROID_SIGNING_ENV = ("FLET_ANDROID_SIGNING_KEY_STORE", "FLET_ANDROID_SIGNING_KEY_ALIAS",
                       "FLET_ANDROID_SIGNING_KEY_STORE_PASSWORD", "FLET_ANDROID_SIGNING_KEY_PASSWORD")
FALLBACK_CHECKS = ("timezones", "assets", "db_init", "migrations", "encryption", "mobile_storage", "security", "ui_startup")

# Installed as sitecustomize in the fallback subprocess: the mobile runtime has no SQLCipher.
_BLOCK_SQLCIPHER = '''
import sys
class _NoSQLCipher:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("sqlcipher3", "pysqlcipher3"):
            raise ImportError(f"{name} is not available on mobile (simulated)")
        return None
sys.meta_path.insert(0, _NoSQLCipher())
'''


@dataclass
class Plan:
    platform: str
    fmt: str
    channel: str
    version: str
    python: str
    split_per_abi: bool = False
    ios: dict[str, str] = field(default_factory=dict)
    steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def flet_exe() -> str:
    local = Path(sys.executable).with_name("flet.exe" if os.name == "nt" else "flet")
    found = str(local) if local.exists() else shutil.which("flet")
    if not found:
        raise BuildError("the `flet` CLI isn't installed (pip install -r requirements-build.txt)")
    return found


def mobile_env() -> dict[str, str]:
    """Build environment: signing variables pass through to `flet build`; app secrets never do."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(APP_ENV_PREFIXES)}
    env.pop("PYTHONPATH", None)
    return env


# --- checks --------------------------------------------------------------------------------------


def resolve_python(requested: str | None) -> tuple[str, tuple[str, ...]]:
    """Python version for the mobile runtime (default: the interpreter the tests ran on) and its Android ABIs."""
    try:
        from flet_cli.utils.python_versions import get_release, supported_short_versions
    except ImportError as exc:
        raise BuildError("flet-cli missing: pip install -r requirements-build.txt") from exc
    short = requested or f"{sys.version_info.major}.{sys.version_info.minor}"
    release = get_release(short)
    if release is None:
        raise BuildError(f"Flet can't bundle Python {short} for mobile (supported: "
                         f"{', '.join(supported_short_versions())}); pass --python-version")
    return release.short, tuple(release.android_abis)


def check_signing(plan: Plan) -> None:
    if plan.platform == "android":
        store = os.environ.get("FLET_ANDROID_SIGNING_KEY_STORE")
        if not store:
            msg = "no FLET_ANDROID_SIGNING_KEY_STORE: the build is debug-signed (fine for testing, rejected by Play)"
            if plan.fmt == "aab" and plan.channel in ("production", "beta"):
                plan.warnings.append(msg)
            else:
                plan.steps.append("signing: debug key (test build)")
            return
        path = Path(store).expanduser().resolve()
        if not path.is_file():
            raise BuildError(f"FLET_ANDROID_SIGNING_KEY_STORE points to a missing file: {store}")
        if path.is_relative_to(ROOT):
            raise BuildError("keep the upload keystore outside the project directory (it must never be committed "
                             "or packaged)")
        missing = [k for k in ANDROID_SIGNING_ENV[1:] if not os.environ.get(k)]
        if missing:
            raise BuildError(f"release signing needs {', '.join(missing)} in the environment")
        plan.steps.append(f"signing: upload key '{os.environ['FLET_ANDROID_SIGNING_KEY_ALIAS']}' "
                          "(passwords from the environment)")
    elif plan.fmt == "ipa":
        if not plan.ios.get("team_id") and not project()["tool"]["flet"].get("ios", {}).get("team_id"):
            plan.warnings.append("no --team-id: Xcode signing will fail for a device build")


def fallback_selftest(plan: Plan, app_dir: Path) -> dict:
    """Run the staged app's self-test as the mobile runtime would: no SQLCipher, platform forced."""
    with tempfile.TemporaryDirectory(prefix="iflowcore-mobile-") as tmp:
        (Path(tmp) / "sitecustomize.py").write_text(_BLOCK_SQLCIPHER, encoding="utf-8")
        env = mobile_env()
        env.update({"PYTHONPATH": tmp, "IFLOW_PLATFORM": plan.platform, "HOME": tmp,
                    "PYTHONTZPATH": "",                  # phones have no zoneinfo-readable tz database
                    "XDG_DATA_HOME": tmp, "XDG_CONFIG_HOME": tmp, "APPDATA": tmp, "LOCALAPPDATA": tmp})
        for k in ANDROID_SIGNING_ENV:
            env.pop(k, None)
        out = Path(tmp) / "report.json"
        result = subprocess.run([sys.executable, str(app_dir / "main.py"), "--selftest", "--json",
                                 f"--only={','.join(FALLBACK_CHECKS)}", f"--out={out}"],
                                env=env, cwd=tmp, capture_output=True, text=True, timeout=600)
        if not out.exists():
            raise BuildError(f"mobile fallback self-test crashed (exit {result.returncode}):\n{result.stderr[-3000:]}")
        report = json.loads(out.read_text(encoding="utf-8"))
    status = {c["name"]: c for c in report["checks"]}
    for c in report["checks"]:
        log(f"  [{c['status'].upper():4}] {c['name']:<15} {c['detail'][:100]}")
    if not report["ok"]:
        raise BuildError("the app doesn't run without SQLCipher: " + "; ".join(
            f"{c['name']}: {c['detail'][:300]}" for c in report["checks"] if c["status"] == "fail"))
    if status["encryption"]["status"] != "skip" or "'platform'" not in status["mobile_storage"]["detail"]:
        raise BuildError("SQLCipher-less simulation didn't take effect (mobile storage must be 'platform')")
    return report


def compile_stage(path: Path) -> int:
    """Byte-compile every module in the stage: a syntax error fails here, not on the device."""
    count = 0
    for f in sorted(path.rglob("*.py")):
        try:
            compile(f.read_bytes(), str(f), "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as exc:
            raise BuildError(f"compile error in {f.relative_to(path)}: {exc}") from exc
        count += 1
    return count


def preflight(plan: Plan, *, dry_run: bool) -> None:
    problems = validate_pyproject([plan.platform])
    if problems:
        raise BuildError("pyproject.toml packaging config:\n  " + "\n  ".join(problems))
    if plan.platform == "ios" and sys.platform != "darwin":
        msg = "iOS builds need macOS with Xcode"
        if not dry_run:
            raise BuildError(msg)
        plan.warnings.append(msg)
    flet_flutter = list((Path.home() / "flutter").glob("*/bin/flutter")) + [Path.home() / "flutter/bin/flutter"]
    if not shutil.which("flutter") and not any(f.exists() for f in flet_flutter):
        # `flet build --yes` downloads the Flutter SDK (and the JDK / Android SDK) into the home folder.
        plan.warnings.append("Flutter SDK not found: `flet build` will download and install it (several GB)")
    if plan.platform == "android":
        _, abis = resolve_python(plan.python)
        archs = project()["tool"]["flet"].get("android", {}).get("target_arch", [])
        bad = [a for a in archs if a not in abis]
        if bad:
            raise BuildError(f"[tool.flet.android] target_arch {bad} not supported by mobile Python {plan.python} "
                             f"(supported: {', '.join(abis)})")
        plan.steps.append(f"Android ABIs: {', '.join(archs) or 'all'} (Python {plan.python})")
        if not (os.environ.get("JAVA_HOME") or shutil.which("java") or list((Path.home() / "java").glob("*/bin/java"))):
            plan.warnings.append("no JDK found; `flet build` will offer to install one")
    check_signing(plan)


# --- build ---------------------------------------------------------------------------------------


def stop_gradle(stage_path: Path) -> None:
    """Gradle leaves a daemon running after the build that keeps writing into build/; stop it so the
    next build (and the stage cleanup) isn't racing it."""
    env = mobile_env()
    if not env.get("JAVA_HOME"):                        # Flet's own JDK (installed under ~/java)
        jdks = sorted((Path.home() / "java").glob("*/bin/java"))
        if jdks:
            env["JAVA_HOME"] = str(jdks[-1].parent.parent)
    for gradlew in stage_path.glob("build/flutter/android/gradlew"):
        try:
            subprocess.run([str(gradlew.resolve()), "--stop"], cwd=gradlew.parent.resolve(), env=env,
                           capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            log("couldn't stop the Gradle daemon (harmless; it exits on its own after a few hours idle)")


def build_command(plan: Plan, stage_path: Path, out: Path) -> list[str]:
    cmd = [flet_exe(), "build", plan.fmt, str(stage_path), "--output", str(out), "--yes", "--no-rich-output",
           "--build-version", plan.version, "--build-number", str(build_number(plan.version)),
           "--python-version", plan.python]
    if plan.split_per_abi:
        cmd.append("--split-per-abi")
    for key, flag in (("team_id", "--ios-team-id"), ("export_method", "--ios-export-method"),
                      ("provisioning_profile", "--ios-provisioning-profile"),
                      ("signing_certificate", "--ios-signing-certificate")):
        if plan.ios.get(key):
            cmd += [flag, plan.ios[key]]
    return cmd


def collect(plan: Plan, out: Path) -> list[Path]:
    patterns = {"apk": "*.apk", "aab": "*.aab", "ipa": "*.ipa", "ios-simulator": "*.app"}
    found = sorted(out.rglob(patterns[plan.fmt]))
    if not found:
        raise BuildError(f"`flet build {plan.fmt}` finished but produced no {patterns[plan.fmt]} in {out}")
    DIST_DIR.mkdir(exist_ok=True)
    artifacts = []
    for f in found:
        suffix = f.stem.replace("app-", "").replace("app", "") if plan.fmt == "apk" and len(found) > 1 else ""
        name = f"{APP_NAME}-{plan.version}-{plan.platform}{'-' + suffix if suffix else ''}{f.suffix}"
        if f.is_dir():                                      # simulator .app bundle
            dest = Path(shutil.make_archive(str(DIST_DIR / name), "zip", f.parent, f.name))
        else:
            dest = Path(shutil.copy2(f, DIST_DIR / name))
        artifacts.append(dest)
    return artifacts


def build(plan: Plan, *, dry_run: bool, fallback: bool) -> list[Path]:
    preflight(plan, dry_run=dry_run)
    st = stage(plan.platform, channel=plan.channel, backend="flet")
    require_clean(st.path)
    plan.steps.append(f"{compile_stage(st.path)} modules byte-compile cleanly")
    report = None
    if fallback:
        log(f"staged app self-test as {plan.platform} without SQLCipher (the mobile runtime has none)")
        report = fallback_selftest(plan, st.path)
        plan.steps.append("SQLite fallback verified: app runs on platform-protected storage without SQLCipher")
    out = BUILD_DIR / "flet" / plan.fmt
    cmd = build_command(plan, st.path, out)
    for w in plan.warnings:
        log(f"warning: {w}")
    if dry_run:
        log("dry run: nothing built")
        for s in plan.steps:
            log(f"  plan · {s}")
        log("  command: " + " ".join(cmd))
        return []
    if out.exists():
        shutil.rmtree(out)
    log("$ " + " ".join(cmd[:4]) + " …")
    try:
        result = subprocess.run(cmd, cwd=st.path, env=mobile_env(), timeout=7200)
    finally:
        stop_gradle(st.path)
    if result.returncode != 0:
        raise BuildError(f"flet build {plan.fmt} failed (exit {result.returncode})")
    artifacts = collect(plan, out)
    for a in artifacts:
        require_clean(a, binary=True)       # extracts apk/aab/ipa + the inner app archive before scanning
    manifest = write_manifest(artifacts, target=f"{plan.platform}-{plan.fmt}", version=plan.version,
                              channel=plan.channel, backend="flet", selftest=report, out=DIST_DIR)
    for a in artifacts:
        log(f"built {a.relative_to(ROOT)} ({a.stat().st_size / 1e6:.1f} MB)")
    log(f"manifest {manifest.relative_to(ROOT)}")
    return artifacts


def parse(argv: list[str] | None = None) -> tuple[Plan, argparse.Namespace]:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("platform", choices=tuple(FORMATS))
    ap.add_argument("--format", dest="fmt", help="android: aab (default) | apk · ios: ipa (default) | ios-simulator")
    ap.add_argument("--channel", choices=("production", "beta", "qa", "dev"), default="production")
    ap.add_argument("--python-version", help="mobile Python runtime (default: this interpreter's version)")
    ap.add_argument("--split-per-abi", action="store_true", help="apk: one smaller APK per CPU architecture")
    ap.add_argument("--team-id", help="iOS: Apple developer team ID")
    ap.add_argument("--export-method", help="iOS: app-store-connect | release-testing | enterprise | debugging")
    ap.add_argument("--provisioning-profile", help="iOS: provisioning profile name")
    ap.add_argument("--signing-certificate", help="iOS: signing certificate name (stays in the keychain)")
    ap.add_argument("--dry-run", action="store_true", help="checks + stage + plan; Flutter not required")
    ap.add_argument("--skip-fallback-check", action="store_true", help="don't run the SQLCipher-less self-test")
    ns = ap.parse_args(argv)
    fmt = ns.fmt or FORMATS[ns.platform][0]
    if fmt not in FORMATS[ns.platform]:
        ap.error(f"--format {fmt} isn't a {ns.platform} format ({', '.join(FORMATS[ns.platform])})")
    if ns.split_per_abi and fmt != "apk":
        ap.error("--split-per-abi applies to --format apk only")
    ios = {k: v for k, v in (("team_id", ns.team_id), ("export_method", ns.export_method),
                             ("provisioning_profile", ns.provisioning_profile),
                             ("signing_certificate", ns.signing_certificate)) if v}
    if ios and ns.platform != "ios":
        ap.error("--team-id/--export-method/--provisioning-profile/--signing-certificate are iOS options")
    try:
        python, _ = resolve_python(ns.python_version)
    except BuildError as exc:
        ap.error(str(exc))
    plan = Plan(platform=ns.platform, fmt=fmt, channel=ns.channel, version=app_version(), python=python,
                split_per_abi=ns.split_per_abi, ios=ios)
    return plan, ns


def main(argv: list[str] | None = None) -> int:
    plan, ns = parse(argv)
    log(f"iFlow Core {plan.version} · {plan.platform} {plan.fmt} · {plan.channel} · Python {plan.python}")
    try:
        build(plan, dry_run=ns.dry_run, fallback=not ns.skip_fallback_check)
    except BuildError as exc:
        print(f"[build] FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
