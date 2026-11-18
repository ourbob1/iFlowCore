"""
Google sign-in on desktop: OAuth 2.0 authorization code + PKCE with a loopback redirect
(RFC 8252, "OAuth for native apps"; what Google requires for installed apps).

1. Start a one-shot HTTP listener on 127.0.0.1 (random port).
2. Open the system browser at Google's consent page with a PKCE S256 challenge and a random state.
3. Google redirects to http://127.0.0.1:<port>/callback?code=…&state=…; the state must match.
4. Exchange the code (+ PKCE verifier) at Google's token endpoint for an ID token.
5. The app sends only that ID token to the iFlow Core API, which verifies it (api/iflow_api/google.py).

The Google access token is discarded; the app never calls Google APIs on the user's behalf.
Configuration (public values, from Google Cloud "Desktop app" OAuth client):
  IFLOW_GOOGLE_DESKTOP_CLIENT_ID, IFLOW_GOOGLE_DESKTOP_CLIENT_SECRET (Google issues desktop clients a
  "secret" that isn't confidential by design; PKCE is what protects the flow).
Android/iOS use native Google Sign-In instead (wired in with the account screens, 10.5).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Callable, Mapping

from core.account.client import AccountError
from core.fleet.http import ProviderError, Transport, default_transport

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ENV_CLIENT_ID = "IFLOW_GOOGLE_DESKTOP_CLIENT_ID"
ENV_CLIENT_SECRET = "IFLOW_GOOGLE_DESKTOP_CLIENT_SECRET"
_DONE_PAGE = (b"<!doctype html><meta charset=utf-8><title>iFlow Core</title>"
              b"<body style='font-family:system-ui;text-align:center;padding:3em'>"
              b"<h2>You're signed in with Google</h2><p>You can close this tab and return to iFlow Core.</p>")


@dataclass(frozen=True)
class GoogleClientConfig:
    client_id: str
    client_secret: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GoogleClientConfig | None:
        env = os.environ if env is None else env
        cid = (env.get(ENV_CLIENT_ID) or "").strip()
        return cls(cid, (env.get(ENV_CLIENT_SECRET) or "").strip() or None) if cid else None


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class _Catcher(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] | None = None

    def do_GET(self) -> None:                           # noqa: N802 (http.server API)
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
        self.wfile.write(_DONE_PAGE)

    def log_message(self, *args: object) -> None:          # keep codes out of logs
        pass


def sign_in(config: GoogleClientConfig, *, open_url: Callable[[str], object] = webbrowser.open,
            transport: Transport | None = None, timeout_s: float = 180.0) -> str:
    """Run the browser flow and return Google's ID token. Raises AccountError on cancel/timeout/error."""
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    handler = type("Catcher", (_Catcher,), {"result": None})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    server.timeout = 1.0
    redirect = f"http://127.0.0.1:{server.server_address[1]}/callback"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": config.client_id, "redirect_uri": redirect, "response_type": "code", "scope": "openid email",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "prompt": "select_account"})
    done = threading.Event()

    def serve() -> None:
        deadline = timeout_s
        while handler.result is None and deadline > 0 and not done.is_set():
            server.handle_request()
            deadline -= server.timeout
        done.set()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        open_url(url)
        done.wait(timeout_s + 2)
    finally:
        done.set()
        thread.join(3)
        server.server_close()
    result = handler.result
    if result is None:
        raise AccountError("google_timeout", "Google sign-in timed out. Try again.")
    if result.get("error"):
        raise AccountError("google_cancelled", "Google sign-in was cancelled")
    if not secrets.compare_digest(result.get("state", ""), state) or not result.get("code"):
        raise AccountError("google_failed", "Google sign-in failed. Try again.")
    form = {"code": result["code"], "client_id": config.client_id, "code_verifier": verifier,
            "grant_type": "authorization_code", "redirect_uri": redirect}
    if config.client_secret:
        form["client_secret"] = config.client_secret
    try:
        resp = (transport or default_transport()).request("POST", TOKEN_URL, form=form, timeout=20)
    except ProviderError as exc:
        raise AccountError("offline", "Can't reach Google. Check your connection.") from exc
    try:
        data = json.loads(resp.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = {}
    if resp.status != 200 or not data.get("id_token"):
        raise AccountError("google_failed", "Google sign-in failed. Try again.")
    return str(data["id_token"])
