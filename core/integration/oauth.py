"""
OAuth 2.0 authorization code + PKCE for integrations (desktop, loopback redirect per RFC 8252).

    creds = connect(schema, broker_url=..., open_url=webbrowser.open)   # browser → consent → tokens
    creds = refresh(schema, creds, broker_url=...)

token_exchange:
  direct  the app posts the code + PKCE verifier to the provider's token_url (public clients)
  broker  the app posts them to iFlow's API (POST /v1/integrations/<id>/token), which adds the
          client secret the provider requires and passes the provider's answer back. The secret
          never ships in the app; the API doesn't store or log the tokens.

Tokens are kept by the caller in device settings (`integration_credentials`, a sealed field).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from typing import Any, Callable

from core.fleet.http import ProviderError
from core.integration.engine import AuthExpired, Credentials, IntegrationError
from core.integration.schema import IntegrationSchema

_DONE = (b"<!doctype html><meta charset=utf-8><title>iFlow Core</title>"
         b"<body style='font-family:system-ui;text-align:center;padding:3em'>"
         b"<h2>Connected</h2><p>You can close this tab and return to iFlow Core.</p>")


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(schema: IntegrationSchema, *, redirect: str, state: str, challenge: str) -> str:
    a = schema.auth
    return a.authorize_url + "?" + urllib.parse.urlencode({
        "client_id": a.client_id, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(a.scopes),
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})


def _token_call(schema: IntegrationSchema, form: dict[str, str], *, transport: Any, broker_url: str | None
                ) -> Credentials:
    try:
        if schema.auth.token_exchange == "broker":
            if not broker_url:
                raise IntegrationError("This connection needs the iFlow server, which isn't configured", code="config")
            url = f"{broker_url.rstrip('/')}/v1/integrations/{urllib.parse.quote(schema.id, safe='')}/token"
            resp = transport.request("POST", url, headers={"Content-Type": "application/json"},
                                     body=json.dumps(form).encode(), timeout=30)
        else:
            resp = transport.request("POST", schema.auth.token_url,
                                     form={**form, "client_id": schema.auth.client_id}, timeout=30)
    except ProviderError:
        raise IntegrationError("Can't reach the sign-in server. Check your connection.", code="offline") from None
    try:
        data = json.loads((resp.body or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = {}
    if resp.status in (400, 401) and form.get("grant_type") == "refresh_token":
        raise AuthExpired()
    if resp.status != 200 or not isinstance(data, dict) or not data.get("access_token"):
        raise IntegrationError(f"{schema.platform} didn't accept the connection ({resp.status})", code="auth")
    expires_in = data.get("expires_in")
    return Credentials(str(data["access_token"]), str(data.get("refresh_token") or form.get("refresh_token") or ""),
                       time.time() + float(expires_in) if isinstance(expires_in, (int, float)) else 0.0)


def exchange(schema: IntegrationSchema, code: str, verifier: str, redirect: str, *, transport: Any,
             broker_url: str | None = None) -> Credentials:
    return _token_call(schema, {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                                "redirect_uri": redirect}, transport=transport, broker_url=broker_url)


def refresh(schema: IntegrationSchema, creds: Credentials, *, transport: Any, broker_url: str | None = None
            ) -> Credentials:
    if not creds.refresh_token:
        raise AuthExpired()
    return _token_call(schema, {"grant_type": "refresh_token", "refresh_token": creds.refresh_token},
                       transport=transport, broker_url=broker_url)


class _Catcher(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] | None = None

    def do_GET(self) -> None:                                   # noqa: N802 (http.server API)
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        type(self).result = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(_DONE)

    def log_message(self, *args: object) -> None:              # keep codes out of logs
        pass


def connect(schema: IntegrationSchema, *, transport: Any, broker_url: str | None = None,
            open_url: Callable[[str], object] = webbrowser.open, timeout_s: float = 180.0) -> Credentials:
    """Desktop browser flow. Raises IntegrationError on cancel / timeout / refusal."""
    if schema.auth.type != "oauth2_pkce":
        raise IntegrationError("This integration doesn't use a browser sign-in", code="config")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    handler = type("Catcher", (_Catcher,), {"result": None})
    try:
        server = http.server.HTTPServer(("127.0.0.1", schema.auth.redirect_port), handler)
    except OSError:
        raise IntegrationError(f"Port {schema.auth.redirect_port} on this computer is busy; close the other "
                               "program using it and try again", code="port") from None
    server.timeout = 1.0
    redirect = f"http://127.0.0.1:{server.server_address[1]}/callback"
    done = threading.Event()

    def serve() -> None:
        remaining = timeout_s
        while handler.result is None and remaining > 0 and not done.is_set():
            server.handle_request()
            remaining -= server.timeout
        done.set()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        open_url(authorize_url(schema, redirect=redirect, state=state, challenge=challenge))
        done.wait(timeout_s + 2)
    finally:
        done.set()
        thread.join(3)
        server.server_close()
    result = handler.result
    if result is None:
        raise IntegrationError("The sign-in timed out. Try again.", code="timeout")
    if result.get("error"):
        raise IntegrationError(f"{schema.platform} sign-in was cancelled", code="cancelled")
    if not secrets.compare_digest(result.get("state", ""), state) or not result.get("code"):
        raise IntegrationError(f"{schema.platform} sign-in failed. Try again.", code="state")
    return exchange(schema, result["code"], verifier, redirect, transport=transport, broker_url=broker_url)
