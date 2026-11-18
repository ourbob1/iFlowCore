#!/usr/bin/env python3
"""
Build the iFlow Core web app: static files that run the whole app in the visitor's browser.

    python scripts/package_web.py                  # → build/web/site/ + dist/iflowcore-web-<v>.tar.gz
    python scripts/package_web.py --serve 8570     # build, then serve locally for a browser test

How it works
------------
`flet publish` packages the app for Pyodide (Python compiled to WebAssembly). Python runs in a web
worker in the visitor's browser; the web server only hands out static files. No Python, database or
PHP is needed on the server for the app itself.

Data stays in the browser: SQLite in memory while unlocked, saved after every change as an
AES-256-GCM snapshot (key = Argon2id of the user's passphrase) in the browser's private origin
storage (core/web/vault.py). The server never receives user data; sync goes end-to-end encrypted
through the account API once that's deployed.

Pipeline: stage (allowlist, same as desktop/mobile) → web requirements pinned to the Flet version
whose browser client is bundled → flet publish (hash routes: no server rewrite rules needed) →
Apache .htaccess (MIME types for .wasm/.mjs, caching, security headers) → secret scan of the site
and of the app archive inside it → tarball + SHA-256 manifest.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_common import (  # noqa: E402
    BUILD_DIR,
    DIST_DIR,
    PRODUCT,
    ROOT,
    BuildError,
    app_version,
    log,
    project,
    require_clean,
    scan_tree,
    stage,
    write_manifest,
)

WEB_DIR = BUILD_DIR / "web"
# Pure-Python/runtime packages the browser installs with micropip (cryptography comes from Pyodide).
EXCLUDE_ON_WEB = ("flet-secure-storage", "sqlcipher", "flet-desktop")

HTACCESS = """# iFlow Core web app (static; everything runs in the browser)
DirectoryIndex index.html index.php
Options -Indexes

<IfModule mod_mime.c>
    AddType application/wasm .wasm
    AddType text/javascript .mjs .js
    AddType application/json .json
</IfModule>

<IfModule mod_headers.c>
    Header always set X-Content-Type-Options "nosniff"
    Header always set Referrer-Policy "no-referrer"
    Header always set Permissions-Policy "camera=(), microphone=(), geolocation=(self)"
    Header always set X-Frame-Options "DENY"
    # The app shell must be revalidated so updates reach users; hashed runtime files can be cached.
    <FilesMatch "^(index\\.html|flutter_bootstrap\\.js|flutter_service_worker\\.js|python-worker\\.js|python\\.js|app\\.tar\\.gz|manifest\\.json|version\\.json)$">
        Header set Cache-Control "no-cache"
    </FilesMatch>
    <FilesMatch "\\.(wasm|mjs|png|ico|otf|ttf|woff2?)$">
        Header set Cache-Control "public, max-age=604800"
    </FilesMatch>
</IfModule>

<IfModule mod_deflate.c>
    AddOutputFilterByType DEFLATE text/html text/javascript application/javascript application/json application/wasm
</IfModule>

# Never serve backups or server-side leftovers from this folder.
<FilesMatch "(\\.bak|\\.orig|~|\\.php\\.old|\\.env)$">
    Require all denied
</FilesMatch>
"""


def web_requirements(data: dict) -> list[str]:
    """[project].dependencies adapted for Pyodide: Flet packages pinned to the bundled client,
    tzdata unconditional (the browser has no system timezone database), native-only packages dropped."""
    import importlib.metadata as md

    pins = {}
    for dist in ("flet", "flet-charts", "flet-geolocator"):
        try:
            pins[dist] = md.version(dist)
        except md.PackageNotFoundError:
            pass
    out = []
    for dep in data["project"]["dependencies"]:
        name = re.split(r"[<>=!~;\[ ]", dep, maxsplit=1)[0].strip().lower()
        if any(x in name for x in EXCLUDE_ON_WEB):
            continue
        if name in pins:
            out.append(f"{name}=={pins[name]}")
        elif name == "tzdata":
            out.append("tzdata")
        else:
            out.append(dep)
    if not any(r.startswith("tzdata") for r in out):
        out.append("tzdata")
    return out


def _rewrite_dependencies(pyproject: Path, deps: list[str]) -> None:
    text = pyproject.read_text(encoding="utf-8")
    block = "dependencies = [\n" + "".join(f'    "{d}",\n' for d in deps) + "]"
    new, n = re.subn(r"(?ms)^dependencies\s*=\s*\[.*?^\]", block, text, count=1)
    if n != 1:
        raise BuildError("couldn't find [project].dependencies in the staged pyproject.toml")
    pyproject.write_text(new, encoding="utf-8")


def flet_exe() -> str:
    local = Path(sys.executable).with_name("flet")
    found = str(local) if local.exists() else shutil.which("flet")
    if not found:
        raise BuildError("the `flet` CLI isn't installed (pip install -r requirements-build.txt)")
    return found


def scan_app_archive(archive: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="iflowcore-webscan-") as tmp:
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter="data")
        findings = scan_tree(Path(tmp))
    if findings:
        raise BuildError("secret scan failed inside app.tar.gz:\n  " +
                         "\n  ".join(f"{f.path}: {f.what}" for f in findings[:20]))
    log("secret scan clean: app.tar.gz")


def build(channel: str = "production") -> tuple[Path, Path]:
    version = app_version()
    st = stage("web", channel=channel, backend="flet-web")
    deps = web_requirements(project())
    _rewrite_dependencies(st.path / "pyproject.toml", deps)
    require_clean(st.path)
    log(f"web requirements: {', '.join(deps)}")
    site = WEB_DIR / "site"
    if site.exists():
        shutil.rmtree(site)
    cmd = [flet_exe(), "publish", str(st.path / "main.py"), "--distpath", str(site), "--app-name", PRODUCT,
           "--app-short-name", PRODUCT, "--app-description", "Command center for gig workers",
           "--base-url", "/", "--route-url-strategy", "hash", "--pwa-background-color", "#FFFFFF",
           "--pwa-theme-color", "#1B3A6B"]
    log("$ flet publish … (app runs in the browser via Pyodide)")
    result = subprocess.run(cmd, cwd=st.path, capture_output=True, text=True)
    if result.returncode != 0:
        raise BuildError(f"flet publish failed:\n{result.stdout[-2000:]}{result.stderr[-2000:]}")
    icon = ROOT / "assets" / "icons" / "iflowcore.png"
    if icon.exists():
        shutil.copy2(icon, site / "favicon.png")
    (site / ".htaccess").write_text(HTACCESS, encoding="utf-8")
    require_clean(site)
    scan_app_archive(site / "app.tar.gz")
    DIST_DIR.mkdir(exist_ok=True)
    bundle = DIST_DIR / f"iflowcore-web-{version}.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        for f in sorted(site.rglob("*")):
            tar.add(f, arcname=f.relative_to(site).as_posix(), recursive=False)
    manifest = write_manifest([bundle], target="web", version=version, channel=channel, backend="flet-web",
                              selftest=None, out=DIST_DIR)
    size = sum(f.stat().st_size for f in site.rglob("*") if f.is_file()) / 1e6
    log(f"site: {site.relative_to(ROOT)} ({size:.1f} MB) · bundle {bundle.relative_to(ROOT)} · "
        f"manifest {manifest.relative_to(ROOT)}")
    return site, bundle


def serve(site: Path, port: int) -> None:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    handler.extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                              ".wasm": "application/wasm", ".mjs": "text/javascript"}
    log(f"serving {site} on http://127.0.0.1:{port}")
    http.server.ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--channel", choices=("production", "beta", "qa", "dev"), default="production")
    ap.add_argument("--serve", type=int, metavar="PORT", help="serve the built site locally afterwards")
    ns = ap.parse_args(argv)
    try:
        site, _ = build(ns.channel)
    except BuildError as exc:
        print(f"[build] FAILED: {exc}", file=sys.stderr)
        return 1
    if ns.serve:
        serve(site, ns.serve)
    return 0


if __name__ == "__main__":
    sys.exit(main())
