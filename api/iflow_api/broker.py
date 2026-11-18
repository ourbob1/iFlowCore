"""
OAuth token broker for platform integrations (Phase 11).

Some platforms (Uber) only issue *confidential* OAuth clients: the code → token exchange needs a
client secret, which can't ship inside an app. The app does the browser sign-in itself (PKCE,
loopback redirect) and sends the code here; this adds the secret, calls the platform's token
endpoint and passes back only the token fields. Nothing is stored or logged: tokens go straight
back to the device, which keeps them sealed.

    IFLOW_API_INTEGRATION_CLIENTS = {"uber-driver-api": {"token_url": "https://auth.uber.com/oauth/v2/token",
                                                         "client_id": "…", "client_secret": "…"}}

(In AWS this is a Secrets Manager value injected like the JWT key.) Unknown integration ids get 404.
Redirects must be loopback (127.0.0.1) so a stolen code can't be redeemed for another site.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping

TOKEN_FIELDS = ("access_token", "refresh_token", "expires_in", "token_type", "scope")
_LOOPBACK = re.compile(r"^http://127\.0\.0\.1:\d{2,5}/callback$")


class BrokerConfigError(ValueError):
    pass


@dataclass(frozen=True)
class BrokerClient:
    token_url: str
    client_id: str
    client_secret: str

    def __repr__(self) -> str:                      # never print the secret
        return f"BrokerClient({self.token_url!r}, client_id={self.client_id!r})"


def parse_clients(raw: str | None) -> dict[str, BrokerClient]:
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise BrokerConfigError("IFLOW_API_INTEGRATION_CLIENTS must be JSON") from None
    if not isinstance(data, dict):
        raise BrokerConfigError("IFLOW_API_INTEGRATION_CLIENTS must be a JSON object")
    out = {}
    for key, value in data.items():
        if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k]
                                                  for k in ("token_url", "client_id", "client_secret")):
            raise BrokerConfigError(f"IFLOW_API_INTEGRATION_CLIENTS[{key!r}] needs token_url, client_id, client_secret")
        if not value["token_url"].startswith("https://"):
            raise BrokerConfigError(f"IFLOW_API_INTEGRATION_CLIENTS[{key!r}].token_url must be https")
        out[str(key)] = BrokerClient(value["token_url"], value["client_id"], value["client_secret"])
    return out


class BrokerError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


Post = Callable[[str, Mapping[str, str]], tuple[int, bytes]]


def _post(url: str, form: Mapping[str, str]) -> tuple[int, bytes]:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a: Any, **k: Any) -> None:
            return None

    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "Accept": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as resp:   # noqa: S310 - https only
            return resp.status, resp.read(64 * 1024)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(64 * 1024) or b""
    except (urllib.error.URLError, TimeoutError, OSError):
        raise BrokerError(502, "Couldn't reach the platform's sign-in server") from None


def exchange(client: BrokerClient, body: Mapping[str, Any], post: Post = _post) -> dict[str, Any]:
    grant = body.get("grant_type")
    form = {"grant_type": str(grant), "client_id": client.client_id, "client_secret": client.client_secret}
    if grant == "authorization_code":
        code, verifier, redirect = (str(body.get(k) or "") for k in ("code", "code_verifier", "redirect_uri"))
        if not code or not verifier or not _LOOPBACK.match(redirect):
            raise BrokerError(422, "code, code_verifier and a loopback redirect_uri are required")
        form.update(code=code, code_verifier=verifier, redirect_uri=redirect)
    elif grant == "refresh_token":
        token = str(body.get("refresh_token") or "")
        if not token:
            raise BrokerError(422, "refresh_token is required")
        form["refresh_token"] = token
    else:
        raise BrokerError(422, "grant_type must be authorization_code or refresh_token")
    status, raw = post(client.token_url, form)
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = {}
    if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
        # the platform's own error code helps the app explain; nothing else is passed through
        code = data.get("error") if isinstance(data, dict) and isinstance(data.get("error"), str) else "token_error"
        raise BrokerError(400 if 400 <= status < 500 else 502, code[:40])
    return {k: data[k] for k in TOKEN_FIELDS if k in data}
