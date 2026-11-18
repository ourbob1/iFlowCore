"""
Client for the AI service's admin API (/admin/v1), used by the Admin Console (admin_console.py).

Deliberately separate from the user's data: it never opens the local database, and the admin
token lives only in this object (memory). It's never written to settings, logs or telemetry, and
it's dropped on sign-out. Console users sign in with a username and password (AdminClient.login);
service tokens still work for automation. HTTPS only (http://localhost for development).
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from core.account.client import AccountError, validate_api_url
from core.fleet.http import ProviderError, Transport, default_transport


class AdminError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.message, self.status = message, status


class AdminClient:
    def __init__(self, base_url: str, token: str, *, transport: Transport | None = None) -> None:
        try:
            self.base_url = validate_api_url(base_url)
        except AccountError as exc:
            raise AdminError(exc.message) from None
        token = (token or "").strip()
        if len(token) < 32:
            raise AdminError("Admin tokens are at least 32 characters")
        self._token = token
        self.transport = transport or default_transport()

    @classmethod
    def login(cls, base_url: str, username: str, password: str, *, transport: Transport | None = None
              ) -> tuple[AdminClient, dict[str, Any]]:
        """Console sign-in: returns a client holding the session token (memory only) and the user."""
        if not (username or "").strip() or not password:
            raise AdminError("Enter your username and password")
        try:
            url = validate_api_url(base_url)
        except AccountError as exc:
            raise AdminError(exc.message) from None
        transport = transport or default_transport()
        try:
            resp = transport.request("POST", url + "/admin/v1/auth/login", headers={"Content-Type": "application/json"},
                                     body=json.dumps({"username": username.strip(), "password": password}).encode(),
                                     timeout=30)
        except ProviderError as exc:
            raise AdminError("Can't reach the server. Check the address and your connection.") from exc
        try:
            data = json.loads(resp.body.decode("utf-8")) if resp.body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        if resp.status >= 400:
            detail = data.get("detail") if isinstance(data, dict) else None
            raise AdminError({404: "This server has no Admin Console enabled"}.get(resp.status)
                             or (detail if isinstance(detail, str) else f"Sign-in failed ({resp.status})"), resp.status)
        return cls(url, data["session_token"], transport=transport), data["user"]

    def __repr__(self) -> str:                              # never show the token
        return f"AdminClient({self.base_url!r})"

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        try:
            resp = self.transport.request(method, self.base_url + path, headers=headers,
                                          body=json.dumps(body).encode() if body is not None else None, timeout=30)
        except ProviderError as exc:
            raise AdminError("Can't reach the server. Check the address and your connection.") from exc
        try:
            data = json.loads(resp.body.decode("utf-8")) if resp.body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        if resp.status >= 400:
            detail = data.get("detail") if isinstance(data, dict) else None
            detail = detail if isinstance(detail, str) else None
            if resp.status == 403 and detail in (None, "Sign in required"):
                detail = "You're not signed in, or that token isn't valid"
            message = {401: "Your session has ended. Sign in again.",
                       404: detail if detail and detail != "Not found" else "This server has no Admin Console enabled",
                       429: "Too many failed attempts; wait a few minutes"}.get(resp.status)
            raise AdminError(message or detail or f"Server error ({resp.status})", resp.status)
        return data if isinstance(data, dict) else {}

    # --- endpoints ---

    def whoami(self) -> dict[str, Any]:
        return self._call("GET", "/admin/v1/whoami")

    def me(self) -> dict[str, Any]:
        return self._call("GET", "/admin/v1/auth/me")

    def change_password(self, current: str, new: str) -> None:
        self._call("POST", "/admin/v1/auth/password", {"current_password": current, "new_password": new})

    def logout(self) -> None:
        try:
            self._call("POST", "/admin/v1/auth/logout")
        except AdminError:
            pass                                          # signing out locally always succeeds

    # users
    def users(self) -> dict[str, Any]:
        return self._call("GET", "/admin/v1/users")

    def create_user(self, username: str, display_name: str, role: str) -> dict[str, Any]:
        return self._call("POST", "/admin/v1/users", {"username": username, "display_name": display_name, "role": role})

    def update_user(self, user_id: str, **changes: Any) -> dict[str, Any]:
        uid = urllib.parse.quote(user_id, safe="")
        return self._call("PATCH", f"/admin/v1/users/{uid}", {k: v for k, v in changes.items() if v is not None})["user"]

    def reset_password(self, user_id: str) -> str:
        uid = urllib.parse.quote(user_id, safe="")
        return self._call("POST", f"/admin/v1/users/{uid}/reset-password")["temporary_password"]

    def unlock_user(self, user_id: str) -> None:
        self._call("POST", f"/admin/v1/users/{urllib.parse.quote(user_id, safe='')}/unlock")

    # Parser Studio
    def parsers(self) -> dict[str, Any]:
        return self._call("GET", "/admin/v1/parsers")

    def parser(self, schema_id: str) -> dict[str, Any]:
        return self._call("GET", f"/admin/v1/parsers/{urllib.parse.quote(schema_id, safe='')}")

    def save_parser(self, schema: dict[str, Any]) -> dict[str, Any]:
        return self._call("PUT", f"/admin/v1/parsers/{urllib.parse.quote(schema['id'], safe='')}", {"schema": schema})

    def publish_parser(self, schema_id: str) -> dict[str, Any]:
        return self._call("POST", f"/admin/v1/parsers/{urllib.parse.quote(schema_id, safe='')}/publish")

    def parser_bundle(self) -> dict[str, Any]:
        return self._call("GET", "/admin/v1/parsers/bundle")

    # Integration Studio (platform API connections)
    def integrations(self) -> dict[str, Any]:
        return self._call("GET", "/admin/v1/integrations")

    def integration(self, schema_id: str) -> dict[str, Any]:
        return self._call("GET", f"/admin/v1/integrations/{urllib.parse.quote(schema_id, safe='')}")

    def save_integration(self, schema: dict[str, Any]) -> dict[str, Any]:
        return self._call("PUT", f"/admin/v1/integrations/{urllib.parse.quote(schema['id'], safe='')}",
                          {"schema": schema})

    def publish_integration(self, schema_id: str) -> dict[str, Any]:
        return self._call("POST", f"/admin/v1/integrations/{urllib.parse.quote(schema_id, safe='')}/publish")

    def distribute_parsers(self) -> dict[str, Any]:
        return self._call("POST", "/admin/v1/parsers/distribute")

    def overview(self) -> dict[str, Any]:
        return self._call("GET", "/admin/v1/overview")

    def plans(self) -> dict[str, Any]:
        return self._call("GET", "/admin/v1/licenses/plans")

    def issue_license(self, tier: str, period: str, *, terms: int = 1, complimentary: bool = False,
                      customer_ref: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/admin/v1/licenses", {"tier": tier, "period": period, "terms": terms,
                                                         "complimentary": complimentary,
                                                         "customer_ref": customer_ref or None})

    def verify_license(self, token: str) -> dict[str, Any]:
        return self._call("POST", "/admin/v1/licenses/verify", {"token": token})

    def licenses(self, limit: int = 25) -> list[dict[str, Any]]:
        return self._call("GET", f"/admin/v1/licenses?limit={int(limit)}")["licenses"]

    def revenue(self, months: int = 12) -> dict[str, Any]:
        return self._call("GET", f"/admin/v1/revenue?months={int(months)}")

    def issues(self, status: str | None = "open", limit: int = 50) -> dict[str, Any]:
        query = urllib.parse.urlencode({k: v for k, v in (("status", status), ("limit", limit)) if v is not None})
        return self._call("GET", f"/admin/v1/telemetry/issues?{query}")

    def issue_events(self, fingerprint: str, limit: int = 10) -> list[dict[str, Any]]:
        fp = urllib.parse.quote(fingerprint, safe="")
        return self._call("GET", f"/admin/v1/telemetry/issues/{fp}?limit={int(limit)}")["events"]

    def triage(self, fingerprint: str, resolve: bool) -> dict[str, Any]:
        fp = urllib.parse.quote(fingerprint, safe="")
        return self._call("POST", f"/admin/v1/telemetry/issues/{fp}/{'resolve' if resolve else 'reopen'}")

    def purge(self, older_than_days: int) -> int:
        return int(self._call("POST", "/admin/v1/telemetry/purge", {"older_than_days": older_than_days})
                   ["deleted_events"])

    def health(self) -> dict[str, Any]:
        return self._call("GET", "/admin/v1/health")

    def audit(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._call("GET", f"/admin/v1/audit?limit={int(limit)}")["entries"]
