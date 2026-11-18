#!/usr/bin/env python3
"""
Post-deploy smoke test for the sync API (no AWS credentials needed; it only makes HTTP requests).

    python scripts/smoke_test.py [--url https://api.iflowcore.com]

Checks: valid TLS; /healthz and /readyz (the database answers); plain HTTP redirects to HTTPS;
protected routes refuse anonymous calls; CORS allows the web app's origin and nobody else; the API
docs are off in production. It creates no accounts and sends no email.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Callable

from _common import load_config


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(url: str, method: str = "GET", headers: dict[str, str] | None = None, *, follow: bool = True):
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "iflow-smoke-test", **(headers or {})})
    try:
        with opener.open(req, timeout=15) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def checks(base: str, web_origin: str) -> list[tuple[str, Callable[[], str | None]]]:
    host = base.split("://", 1)[1]

    def health():
        status, _, body = request(base + "/healthz")
        return None if status == 200 and json.loads(body).get("status") == "ok" else f"HTTP {status}"

    def ready():
        status, _, body = request(base + "/readyz")
        return None if status == 200 else f"HTTP {status} {body[:80]!r} (database unreachable?)"

    def redirect():
        status, headers, _ = request(f"http://{host}/healthz", follow=False)
        location = headers.get("Location") or headers.get("location") or ""
        return None if status in (301, 302, 308) and location.startswith("https://") else f"HTTP {status} → {location!r}"

    def protected():
        status, _, _ = request(base + "/v1/account")
        return None if status == 401 else f"expected 401, got {status}"

    def cors_allowed():
        status, headers, _ = request(base + "/v1/auth/login", "OPTIONS", {
            "Origin": web_origin, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
        allowed = {k.lower(): v for k, v in headers.items()}.get("access-control-allow-origin")
        return None if status == 200 and allowed == web_origin else f"HTTP {status}, allow-origin={allowed!r}"

    def cors_denied():
        _, headers, _ = request(base + "/v1/auth/login", "OPTIONS", {
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        allowed = {k.lower(): v for k, v in headers.items()}.get("access-control-allow-origin")
        return None if not allowed else f"a foreign origin was allowed ({allowed!r})"

    def docs_off():
        status, _, _ = request(base + "/docs")
        return None if status == 404 else f"/docs answered {status} (should be off in production)"

    return [("TLS + /healthz", health), ("/readyz (database)", ready), ("HTTP → HTTPS", redirect),
            ("anonymous calls refused", protected), (f"CORS allows {web_origin}", cors_allowed),
            ("CORS refuses other origins", cors_denied), ("API docs off", docs_off)]


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=cfg.api_url)
    args = parser.parse_args(argv)
    base = args.url.rstrip("/")
    failed = 0
    for name, check in checks(base, cfg.web_origins[0]):
        try:
            problem = check()
        except Exception as exc:                        # DNS not ready, TLS error, timeout…
            problem = f"{type(exc).__name__}: {exc}"
        print(("✓ " if problem is None else "✗ ") + name + ("" if problem is None else f": {problem}"))
        failed += problem is not None
    print(f"\n{'All checks passed' if not failed else f'{failed} check(s) failed'} for {base}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
