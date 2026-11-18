#!/usr/bin/env python3
"""
Package iFlow Core as a standalone desktop app.

    python scripts/package_desktop.py                         # host target, PyInstaller, production
    python scripts/package_desktop.py --formats deb,tar       # Linux: pick outputs (tar, deb, appimage)
    python scripts/package_desktop.py --backend flet          # Flutter shell via `flet build` (needs Flutter)
    python scripts/package_desktop.py --target windows --dry-run   # show the plan without building

Targets and outputs
-------------------
  linux    dist/iFlowCore-<v>-linux-<arch>.tar.gz, iflowcore_<v>_<arch>.deb, iFlowCore-<v>-<arch>.AppImage
  macos    dist/iFlowCore-<v>-macos-<arch>.zip (iFlowCore.app), .dmg when hdiutil is available
  windows  dist/iFlowCore-<v>-windows-<arch>.exe (single file) or .zip (--onedir)

Desktop binaries must be built on their own OS (PyInstaller and Flutter don't cross-compile).
A dry run works for any target on any host.

Pipeline
--------
 1. Preflight: pyproject packaging config (core of the desktop/mobile split), the SQLCipher driver
    is importable *here* (it's what gets bundled), and the build tools exist.
 2. Stage an allowlisted copy of the app (scripts/build_common.py) and secret-scan it.
 3. Build from the stage, never the working tree. Every non-Python file in the stage (schema.sql,
    migrations/*.sql, tax tables, _build_info.json) is bundled at the same relative path, and the
    SQLCipher extension plus its native library are collected with it.
 4. Check provenance: every module PyInstaller bundled from this project came from the stage.
 5. Run the frozen app's own self-test (`--selftest`): assets, DB init, every migration,
    the SQLCipher key handshake, mobile fallback, production defaults, UI startup.
 6. Secret-scan the finished artifacts (binary mode) and write a SHA-256 manifest.

No key is ever needed or bundled: desktop databases are keyed by the passphrase the user enters
at unlock. Secret environment variables are removed from the build subprocess, and the final
scan fails the build if any of their values appear in an artifact anyway.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_common import (  # noqa: E402
    APP_ENV_PREFIXES,
    APPS,
    BUILD_DIR,
    DESKTOP_TARGETS,
    DIST_DIR,
    AppSpec,
    ROOT,
    SECRET_ENV_VARS,
    BuildError,
    Stage,
    app_version,
    host_target,
    log,
    project,
    require_clean,
    stage,
    validate_pyproject,
    write_manifest,
)

FORMATS = {"linux": ("tar", "deb", "appimage"), "macos": ("zip", "dmg"), "windows": ("exe", "zip")}
DEFAULT_FORMATS = {"linux": ("tar", "deb", "appimage"), "macos": ("zip", "dmg"), "windows": ("exe",)}
DEFAULT_ICON_EXT = {"linux": "png", "macos": "icns", "windows": "ico"}


@dataclass
class Plan:
    target: str
    backend: str
    channel: str
    formats: list[str]
    explicit_formats: bool
    onefile: bool
    icon: Path | None
    arch: str
    codesign_identity: str | None
    version: str
    app: AppSpec = APPS["core"]
    steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def machine_arch() -> str:
    m = platform.machine().lower()
    return {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(m, m)


def deb_arch(arch: str) -> str:
    return {"x86_64": "amd64", "arm64": "arm64"}.get(arch, arch)


def flet_exe() -> str:
    local = Path(sys.executable).with_name("flet.exe" if os.name == "nt" else "flet")
    found = str(local) if local.exists() else shutil.which("flet")
    if not found:
        raise BuildError("the `flet` CLI isn't installed (pip install -r requirements-build.txt)")
    return found


def build_env() -> dict[str, str]:
    """Environment for build subprocesses: no secrets, no IFLOW_* overrides."""
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV_VARS and not k.startswith(APP_ENV_PREFIXES)}
    env.pop("PYTHONPATH", None)            # modules must come from the stage, never the checkout
    env["PYTHONNOUSERSITE"] = "1"
    return env


# --- preflight -----------------------------------------------------------------------------------


def sqlcipher_driver() -> str:
    try:
        import sqlcipher3  # noqa: F401
        from sqlcipher3 import dbapi2
    except ImportError as exc:
        raise BuildError("SQLCipher driver missing in the build environment: desktop builds must bundle it. "
                         "Install 'sqlcipher3-binary' (Linux) or 'sqlcipher3' against a system SQLCipher "
                         f"(macOS: brew install sqlcipher; Windows: see README). ({exc})") from exc
    conn = dbapi2.connect(":memory:")
    try:
        version = conn.execute("PRAGMA cipher_version").fetchone()
    finally:
        conn.close()
    if not version or not version[0]:
        raise BuildError("the sqlcipher3 module is linked against plain SQLite (PRAGMA cipher_version is empty)")
    return str(version[0])


def preflight(plan: Plan, *, dry_run: bool) -> None:
    host = host_target()
    if plan.target != host and not dry_run:
        raise BuildError(f"can't build {plan.target} on {host}: desktop builds don't cross-compile. "
                         f"Run this on {plan.target} (or use --dry-run to see the plan).")
    problems = validate_pyproject([plan.target])
    if problems:
        raise BuildError("pyproject.toml packaging config:\n  " + "\n  ".join(problems))
    requires = project()["project"].get("requires-python", "")
    if sys.version_info < (3, 10):
        raise BuildError(f"Python 3.10+ required to build (pyproject: {requires})")

    try:
        plan.steps.append(f"SQLCipher driver: {sqlcipher_driver()}")
    except BuildError:
        if not dry_run:
            raise
        plan.warnings.append("SQLCipher driver not importable here; the real build will refuse to run")

    if plan.backend == "pyinstaller":
        try:
            import PyInstaller  # noqa: F401
        except ImportError as exc:
            if not dry_run:
                raise BuildError("PyInstaller missing: pip install -r requirements-build.txt") from exc
            plan.warnings.append("PyInstaller not installed")
    elif not shutil.which("flutter"):
        msg = "`flet build` needs the Flutter SDK on PATH (https://docs.flutter.dev/get-started/install)"
        if not dry_run:
            raise BuildError(msg)
        plan.warnings.append(msg)

    tools = {"deb": "dpkg-deb", "appimage": "appimagetool", "dmg": "hdiutil"}
    for fmt in list(plan.formats):
        tool = tools.get(fmt)
        if tool and not shutil.which(tool):
            if plan.explicit_formats and not dry_run:
                raise BuildError(f"--formats {fmt} needs `{tool}` on PATH")
            plan.formats.remove(fmt)
            plan.warnings.append(f"skipping {fmt}: `{tool}` not found")
    if plan.target == "windows" and plan.onefile and "zip" in plan.formats:
        plan.warnings.append("zip packs the one-file .exe; use --onedir for a folder build")


# --- PyInstaller (flet pack) ---------------------------------------------------------------------


def data_args(st: Stage) -> list[str]:
    """--add-data for every non-Python file in the stage, grouped by directory (same relative path)."""
    dirs: dict[str, list[str]] = {}
    for rel in st.files:
        if rel.endswith(".py") or rel == "pyproject.toml":
            continue
        parent = str(Path(rel).parent).replace("\\", "/")
        dirs.setdefault(parent, []).append(rel)
    args = []
    for parent, files in sorted(dirs.items()):
        for rel in sorted(files):
            args.append(f"--add-data={st.path / rel}{os.pathsep}{parent}")
    return args


def pack_command(plan: Plan, st: Stage, work: Path) -> list[str]:
    meta = project()["tool"]["flet"]
    cmd = [flet_exe(), "pack", str(st.path / plan.app.entry), "--name", plan.app.name, "--distpath", str(work / "dist"),
           "--yes", "--product-name", plan.app.product, "--product-version", plan.version,
           "--file-version", ".".join((plan.version.split(".") + ["0"] * 4)[:4]),
           "--file-description", plan.app.product, "--company-name", meta["company"], "--copyright", meta["copyright"],
           "--bundle-id", plan.app.bundle_id]
    if plan.target != "macos" and not plan.onefile:
        cmd.append("--onedir")                          # macOS always produces a .app bundle
    if plan.icon:
        cmd += ["--icon", str(plan.icon)]
    if plan.target == "macos" and plan.codesign_identity:
        cmd += ["--codesign-identity", plan.codesign_identity]      # a keychain identity name, not a secret
    cmd += data_args(st)
    cmd += ["--hidden-import", "sqlcipher3", "--hidden-import", "sqlcipher3.dbapi2"]
    # Sub-packages are imported lazily by the views; collect all of them. tzdata backs zoneinfo on
    # Windows (no system tz database). --clean: never reuse another build's cache.
    if plan.app.key == "console":
        # the built-in admin service: uvicorn picks its protocol/loop modules at runtime, argon2's
        # binding is a native extension, and boto3 needs its service models for CDN uploads. The AI
        # model stack is never needed in the console.
        for extra in ("--collect-submodules=iflow_ai", "--collect-submodules=uvicorn", "--collect-all=argon2",
                      "--collect-all=_argon2_cffi_bindings", "--collect-data=botocore", "--collect-data=boto3",
                      "--exclude-module=torch", "--exclude-module=transformers", "--exclude-module=accelerate",
                      "--exclude-module=huggingface_hub", "--exclude-module=pytesseract",
                      "--exclude-module=pillow_heif", "--exclude-module=pypdfium2"):
            cmd.append(f"--pyinstaller-build-args={extra}")
    for extra in ("--collect-submodules=core", "--collect-submodules=ui", "--collect-submodules=database",
                  "--collect-binaries=sqlcipher3", "--collect-data=tzdata",
                  f"--workpath={work / 'pyi'}", f"--specpath={work}", "--clean",
                  "--exclude-module=pytest", "--exclude-module=tests", "--exclude-module=services"):
        cmd.append(f"--pyinstaller-build-args={extra}")
    return cmd


# botocore ships models for ~440 AWS services (plus API examples that contain AWS's documentation
# placeholder keys). The console only uploads statement formats: keep what that needs.
BOTOCORE_SERVICES = {"s3", "cloudfront", "sts", "sso", "sso-oidc"}


def prune_botocore(dist: Path) -> int:
    removed = 0
    for data in dist.rglob("botocore/data"):
        for service in data.iterdir():
            if service.is_dir() and service.name not in BOTOCORE_SERVICES:
                shutil.rmtree(service)
                removed += 1
        for examples in data.rglob("examples-1.json"):
            examples.unlink()
    return removed


def run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: int = 3600) -> None:
    log("$ " + " ".join(c if " " not in c else repr(c) for c in cmd[:6]) + (" …" if len(cmd) > 6 else ""))
    result = subprocess.run(cmd, cwd=cwd, env=env or build_env(), timeout=timeout)
    if result.returncode != 0:
        raise BuildError(f"{Path(cmd[0]).name} {cmd[1] if len(cmd) > 1 else ''} failed (exit {result.returncode})")


def check_provenance(work: Path, st: Stage) -> int:
    """Every project module PyInstaller analysed must come from the stage (not the checkout)."""
    tocs = list((work / "pyi").rglob("Analysis-*.toc")) or list((work / "pyi").rglob("PYZ-*.toc"))
    if not tocs:
        raise BuildError("PyInstaller analysis TOC not found; can't verify module provenance")
    text = "\n".join(t.read_text(encoding="utf-8", errors="replace") for t in tocs)
    root = str(ROOT)
    allowed = (str(st.path), str(work), sys.prefix, sys.base_prefix)
    leaked, count = set(), 0
    stage_dir = str(st.path)
    for chunk in set(text.split("'")):
        if chunk.startswith(stage_dir) and chunk.endswith(".py"):
            count += 1                                              # a project module, from the stage
        elif chunk.startswith(root) and not chunk.startswith(allowed):
            leaked.add(os.path.relpath(chunk, root))
    if leaked:
        raise BuildError("modules bundled from the working tree instead of the stage:\n  "
                         + "\n  ".join(sorted(leaked)[:15]))
    return count


def frozen_binary(plan: Plan, dist: Path) -> Path:
    if plan.target == "macos":
        return dist / f"{plan.app.name}.app" / "Contents" / "MacOS" / plan.app.name
    exe = plan.app.name + (".exe" if plan.target == "windows" else "")
    return dist / exe if plan.onefile else dist / plan.app.name / exe


def run_selftest(binary: Path, work: Path) -> dict:
    if not binary.exists():
        raise BuildError(f"built binary not found at {binary}")
    out = work / "selftest.json"
    out.unlink(missing_ok=True)
    env = build_env()
    env.pop("FLET_VIEW_PATH", None)
    with tempfile.TemporaryDirectory(prefix="iflowcore-selftest-") as home:
        env.update({"HOME": home, "USERPROFILE": home, "XDG_DATA_HOME": home, "XDG_CONFIG_HOME": home,
                    "APPDATA": home, "LOCALAPPDATA": home})
        log(f"self-test inside the frozen app: {binary.name} --selftest")
        result = subprocess.run([str(binary), "--selftest", "--json", f"--out={out}"], env=env, timeout=600,
                                capture_output=True, text=True)
    if not out.exists():
        raise BuildError(f"frozen self-test produced no report (exit {result.returncode}):\n"
                         f"{result.stdout[-2000:]}{result.stderr[-2000:]}")
    report = json.loads(out.read_text(encoding="utf-8"))
    for c in report["checks"]:
        log(f"  [{c['status'].upper():4}] {c['name']:<15} {c['detail'][:110]}")
    if not report["ok"] or result.returncode != 0:
        failed = [c for c in report["checks"] if c["status"] == "fail"]
        raise BuildError("frozen self-test failed: " + "; ".join(f"{c['name']}: {c['detail'][:300]}" for c in failed))
    status = {c["name"]: c["status"] for c in report["checks"]}
    if not report.get("frozen"):
        raise BuildError("self-test didn't run from the frozen app")
    if "encryption" in status and status.get("encryption") != "pass":
        raise BuildError("SQLCipher isn't working inside the build (encryption check did not pass)")
    return report


# --- installers ----------------------------------------------------------------------------------


def desktop_entry(plan: Plan, exec_path: str, icon: str | None) -> str:
    cats = ";".join(project()["tool"]["flet"].get("linux", {}).get("categories", ["Office"])) + ";"
    lines = ["[Desktop Entry]", "Type=Application", f"Name={plan.app.product}",
             f"Comment={plan.app.comment}",
             f"Exec={exec_path}", f"Icon={icon or 'accessories-calculator'}", f"Categories={cats}",
             "Terminal=false", f"StartupWMClass={plan.app.bundle_id}"]
    return "\n".join(lines) + "\n"


def make_tar(src_dir: Path, out: Path) -> Path:
    with tarfile.open(out, "w:gz") as tar:
        tar.add(src_dir, arcname=src_dir.name)
    return out


def make_zip(src: Path, out: Path) -> Path:
    if sys.platform == "darwin" and shutil.which("ditto"):      # keeps .app symlinks and signatures
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(src), str(out)], check=True)
        return out
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        if src.is_file():
            zf.write(src, src.name)
        else:
            for f in sorted(src.rglob("*")):
                zf.write(f, f.relative_to(src.parent).as_posix())
    return out


def normalize_modes(root: Path) -> None:
    """0755 dirs/executables, 0644 everything else (dpkg rejects group/world-writable trees)."""
    for p in [root, *root.rglob("*")]:
        if p.is_symlink():
            continue
        if p.is_dir():
            p.chmod(0o755)
            continue
        with p.open("rb") as fh:          # mode bits from the source drive can't be trusted; sniff
            head = fh.read(4)
        p.chmod(0o755 if head == b"\x7fELF" or head[:2] == b"#!" else 0o644)


def make_deb(plan: Plan, app_dir: Path, work: Path) -> Path:
    # Assemble on the system temp filesystem: project drives mounted as exFAT/NTFS report every
    # file as 0777 and can't hold the modes a .deb needs.
    with tempfile.TemporaryDirectory(prefix="iflowcore-deb-") as tmp:
        return _make_deb(plan, app_dir, Path(tmp) / "root", work)


def _make_deb(plan: Plan, app_dir: Path, root: Path, work: Path) -> Path:
    opt = root / "opt" / plan.app.deb
    shutil.copytree(app_dir, opt, symlinks=True)
    (root / "usr" / "bin").mkdir(parents=True)
    os.symlink(f"/opt/{plan.app.deb}/{plan.app.name}", root / "usr" / "bin" / plan.app.deb)
    apps = root / "usr" / "share" / "applications"
    apps.mkdir(parents=True)
    icon_name = None
    if plan.icon and plan.icon.suffix == ".png":
        icons = root / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps"
        icons.mkdir(parents=True)
        shutil.copy2(plan.icon, icons / f"{plan.app.deb}.png")
        icon_name = plan.app.deb
    (apps / f"{plan.app.deb}.desktop").write_text(desktop_entry(plan, f"/usr/bin/{plan.app.deb}", icon_name), encoding="utf-8")
    size_kb = sum(f.stat().st_size for f in opt.rglob("*") if f.is_file() and not f.is_symlink()) // 1024
    control = root / "DEBIAN"
    control.mkdir()
    (control / "control").write_text("\n".join([
        f"Package: {plan.app.deb}", f"Version: {plan.version}", f"Architecture: {deb_arch(plan.arch)}",
        f"Maintainer: {project()['tool']['flet']['company']} <support@iflowcore.dev>",
        f"Installed-Size: {size_kb}", "Section: misc", "Priority: optional",
        "Depends: libgtk-3-0, libc6",
        *(["Replaces: rubysoho", "Conflicts: rubysoho"] if plan.app.key == "core" else []),   # pre-rebrand name
        f"Description: {plan.app.product}: {plan.app.comment[0].lower() + plan.app.comment[1:]}",
        (" Encrypted (SQLCipher AES-256) earnings, mileage and tax tracking that stays on your device."
         if plan.app.key == "core" else
         " Accounts, licenses, statement formats (Parser Studio) and crash reports, with a built-in service."),
    ]) + "\n", encoding="utf-8")
    normalize_modes(root)
    out = DIST_DIR / f"{plan.app.deb}_{plan.version}_{deb_arch(plan.arch)}.deb"
    run(["dpkg-deb", "--root-owner-group", "--build", str(root), str(out)], cwd=work)
    return out


def make_appimage(plan: Plan, app_dir: Path, work: Path) -> Path:
    with tempfile.TemporaryDirectory(prefix="iflowcore-appimage-") as tmp:
        return _make_appimage(plan, app_dir, Path(tmp) / f"{plan.app.name}.AppDir", work)


def _make_appimage(plan: Plan, app_dir: Path, appdir: Path, work: Path) -> Path:
    shutil.copytree(app_dir, appdir / "usr" / "lib" / plan.app.deb, symlinks=True)
    run_sh = appdir / "AppRun"
    run_sh.write_text('#!/bin/sh\nHERE="$(dirname "$(readlink -f "$0")")"\n'
                      f'exec "$HERE/usr/lib/{plan.app.deb}/{plan.app.name}" "$@"\n', encoding="utf-8")
    run_sh.chmod(0o755)
    icon = plan.app.deb
    (appdir / f"{plan.app.deb}.desktop").write_text(desktop_entry(plan, plan.app.name, icon), encoding="utf-8")
    if plan.icon and plan.icon.suffix == ".png":
        shutil.copy2(plan.icon, appdir / f"{icon}.png")
    else:     # appimagetool requires an icon file; a 1x1 transparent PNG placeholder
        (appdir / f"{icon}.png").write_bytes(bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
            "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"))
    out = DIST_DIR / f"{plan.app.name}-{plan.version}-{plan.arch}.AppImage"
    env = build_env()
    env["ARCH"] = plan.arch
    run(["appimagetool", "--no-appstream", str(appdir), str(out)], cwd=work, env=env)
    return out


def make_dmg(plan: Plan, app: Path, out: Path, work: Path) -> Path:
    run(["hdiutil", "create", "-volname", plan.app.product, "-srcfolder", str(app), "-ov", "-format", "UDZO", str(out)],
        cwd=work)
    return out


# --- flet build (Flutter shell) ------------------------------------------------------------------


def flet_build_command(plan: Plan, st: Stage, work: Path) -> list[str]:
    cmd = [flet_exe(), "build", plan.target, str(st.path), "--output", str(work / "flet"), "--yes",
           "--build-version", plan.version, "--no-rich-output"]
    return cmd


def collect_flet_build(plan: Plan, work: Path) -> list[Path]:
    out = work / "flet"
    stem = f"{plan.app.name}-{plan.version}-{plan.target}-{plan.arch}"
    if plan.target == "macos":
        apps = list(out.glob("*.app"))
        if not apps:
            raise BuildError(f"no .app in {out}")
        return [make_zip(apps[0], DIST_DIR / f"{stem}.zip")]
    return [make_zip(out, DIST_DIR / f"{stem}.zip") if plan.target == "windows"
            else make_tar(out, DIST_DIR / f"{stem}.tar.gz")]


# --- main ----------------------------------------------------------------------------------------


def build(plan: Plan, *, dry_run: bool, selftest: bool) -> list[Path]:
    preflight(plan, dry_run=dry_run)
    st = stage(plan.target, channel=plan.channel, backend=plan.backend, app=plan.app,
               out=BUILD_DIR / "stage" / f"{plan.target}-{plan.app.key}")
    require_clean(st.path)
    work = BUILD_DIR / plan.backend / (plan.target if plan.app.key == "core" else f"{plan.target}-{plan.app.key}")
    cmd = pack_command(plan, st, work) if plan.backend == "pyinstaller" else flet_build_command(plan, st, work)
    plan.steps.append(f"{len(st.files)} files staged from the allowlist, secret scan clean")
    plan.steps.append("build: " + " ".join(cmd[:2] + ["…"]) + f" ({len(cmd)} args)")
    plan.steps.append(f"outputs: {', '.join(plan.formats) or 'none'}")
    for w in plan.warnings:
        log(f"warning: {w}")
    if dry_run:
        log("dry run: nothing built")
        for s in plan.steps:
            log(f"  plan · {s}")
        log("  command: " + " ".join(cmd))
        return []

    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    DIST_DIR.mkdir(exist_ok=True)
    artifacts: list[Path] = []
    report = None
    stem = f"{plan.app.name}-{plan.version}-{plan.target}-{plan.arch}"
    if plan.backend == "flet":
        run(cmd, cwd=st.path)
        artifacts = collect_flet_build(plan, work)
        log("note: the frozen self-test runs only for PyInstaller builds; run `<app> --selftest` by hand")
    else:
        run(cmd, cwd=work)
        n = check_provenance(work, st)
        log(f"provenance OK: {n} project modules, all from the stage")
        dist = work / "dist"
        if plan.app.key == "console":
            log(f"pruned {prune_botocore(dist)} unused AWS service models (kept {', '.join(sorted(BOTOCORE_SERVICES))})")
        # Scan the uncompressed bundle (archives below are compressed, so scanning them proves little).
        require_clean(dist, binary=True)
        if selftest:
            report = run_selftest(frozen_binary(plan, dist), work)
        if plan.target == "linux":
            app_dir = dist / plan.app.name
            if "tar" in plan.formats:
                artifacts.append(make_tar(app_dir, DIST_DIR / f"{stem}.tar.gz"))
            if "deb" in plan.formats:
                artifacts.append(make_deb(plan, app_dir, work))
            if "appimage" in plan.formats:
                artifacts.append(make_appimage(plan, app_dir, work))
        elif plan.target == "macos":
            app = dist / f"{plan.app.name}.app"
            if "zip" in plan.formats:
                artifacts.append(make_zip(app, DIST_DIR / f"{stem}.zip"))
            if "dmg" in plan.formats:
                artifacts.append(make_dmg(plan, app, DIST_DIR / f"{stem}.dmg", work))
        else:
            built = dist / f"{plan.app.name}.exe" if plan.onefile else dist / plan.app.name
            if "exe" in plan.formats and plan.onefile:
                artifacts.append(Path(shutil.copy2(built, DIST_DIR / f"{stem}.exe")))
            if "zip" in plan.formats or not plan.onefile:
                artifacts.append(make_zip(built, DIST_DIR / f"{stem}.zip"))

    for a in artifacts:
        require_clean(a, binary=True)          # catches anything added while packaging
    manifest = write_manifest(artifacts, app=plan.app, target=plan.target, version=plan.version, channel=plan.channel,
                              backend=plan.backend, selftest=report, out=DIST_DIR)
    for a in artifacts:
        log(f"built {a.relative_to(ROOT)} ({a.stat().st_size / 1e6:.1f} MB)")
    log(f"manifest {manifest.relative_to(ROOT)}")
    return artifacts


def parse(argv: list[str] | None = None) -> tuple[Plan, argparse.Namespace]:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--app", choices=sorted(APPS), default="core",
                    help="core: the iFlow Core app · console: the Admin Console (operator app)")
    ap.add_argument("--target", choices=DESKTOP_TARGETS, default=host_target() if host_target() in DESKTOP_TARGETS
                    else "linux")
    ap.add_argument("--backend", choices=("pyinstaller", "flet"), default="pyinstaller",
                    help="pyinstaller: `flet pack` (default, no Flutter needed); flet: `flet build` (Flutter SDK)")
    ap.add_argument("--channel", choices=("production", "beta", "qa", "dev"), default="production",
                    help="production/beta default to encrypted production mode; qa/dev are for testing only")
    ap.add_argument("--formats", help="comma-separated: linux tar,deb,appimage · macos zip,dmg · windows exe,zip")
    ap.add_argument("--onedir", action="store_true", help="Windows: folder build instead of a single .exe")
    ap.add_argument("--icon", type=Path, help=".png (Linux), .icns (macOS) or .ico (Windows); "
                                              "default: assets/icons/iflowcore.*")
    ap.add_argument("--codesign-identity", help='macOS: keychain identity, e.g. "Developer ID Application: …"')
    ap.add_argument("--arch", default=machine_arch(), help=argparse.SUPPRESS)
    ap.add_argument("--dry-run", action="store_true", help="preflight + stage + scan, print the build plan")
    ap.add_argument("--skip-selftest", action="store_true", help="don't run the frozen app's self-test")
    ns = ap.parse_args(argv)
    if ns.formats:
        formats = [f.strip() for f in ns.formats.split(",") if f.strip()]
        bad = [f for f in formats if f not in FORMATS[ns.target]]
        if bad:
            ap.error(f"format(s) {', '.join(bad)} not available for {ns.target} (choose from "
                     f"{', '.join(FORMATS[ns.target])})")
    else:
        formats = list(DEFAULT_FORMATS[ns.target])
    if ns.icon and not ns.icon.is_file():
        ap.error(f"icon not found: {ns.icon}")
    if ns.icon is None:          # brand icons from scripts/make_brand_assets.py
        default = ROOT / "assets" / "icons" / f"iflowcore.{DEFAULT_ICON_EXT[ns.target]}"
        ns.icon = default if default.is_file() else None
    onefile = ns.target == "windows" and not ns.onedir
    if ns.target == "windows" and ns.onedir and "exe" in formats:
        formats = [f for f in formats if f != "exe"] or ["zip"]
    plan = Plan(target=ns.target, backend=ns.backend, channel=ns.channel, formats=formats,
                explicit_formats=bool(ns.formats), onefile=onefile,
                icon=ns.icon.resolve() if ns.icon else None, arch=ns.arch, version=app_version(),
                codesign_identity=ns.codesign_identity, app=APPS[ns.app])
    return plan, ns


def main(argv: list[str] | None = None) -> int:
    plan, ns = parse(argv)
    log(f"{plan.app.product} {plan.version} · {plan.target}/{plan.arch} · {plan.backend} · {plan.channel}")
    try:
        build(plan, dry_run=ns.dry_run, selftest=not ns.skip_selftest)
    except BuildError as exc:
        print(f"[build] FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
