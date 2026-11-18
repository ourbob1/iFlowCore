"""
App-side client for the iFlow Core account API (api/iflow_api).

- Talks HTTPS only (plain http is allowed for localhost during development).
- Keeps the refresh token in a SessionStore (device settings: SQLCipher on desktop, sealed with the
  device key on mobile) and the short-lived access token in memory only.
- Refreshes the access token shortly before it expires and retries once on 401. If the server ends
  the session (sign-out elsewhere, refresh-token reuse, account deleted) the stored session is
  cleared and AccountError("signed_out") is raised, so the UI can return to the sign-in screen.
- Never logs tokens, passwords, codes or emails.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from dataclasses import asdict, dataclass
from typing import Any, Callable, Protocol

from core.fleet.http import ProviderError, Transport, default_transport

log = logging.getLogger(__name__)

ENV_API_URL = "IFLOW_ACCOUNT_API_URL"
REFRESH_MARGIN_S = 60
PLATFORM_LABELS = {"linux": "Linux desktop", "macos": "Mac", "windows": "Windows PC", "android": "Android phone",
                   "ios": "iPhone", "web": "Web browser"}


class AccountError(Exception):
    def __init__(self, code: str, message: str, status: int = 0, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.status, self.retry_after = code, message, status, retry_after


@dataclass(frozen=True)
class AccountSession:
    api_url: str
    user_id: str
    email: str
    device_id: str
    refresh_token: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> AccountSession | None:
        try:
            return cls(**{k: str(raw[k]) for k in ("api_url", "user_id", "email", "device_id", "refresh_token")})
        except (KeyError, TypeError):
            return None


class SessionStore(Protocol):
    def load(self) -> AccountSession | None: ...
    def save(self, session: AccountSession | None) -> None: ...


class MemorySessionStore:
    def __init__(self, session: AccountSession | None = None) -> None:
        self.session = session

    def load(self) -> AccountSession | None:
        return self.session

    def save(self, session: AccountSession | None) -> None:
        self.session = session


class SettingsSessionStore:
    """Persists the session in device settings (`account_session`, a sealed field)."""

    def __init__(self, settings_store: Any) -> None:
        self._store = settings_store

    def load(self) -> AccountSession | None:
        return AccountSession.from_dict(self._store.load().account_session)

    def save(self, session: AccountSession | None) -> None:
        self._store.update(account_session=session.to_dict() if session else None)


def validate_api_url(url: str) -> str:
    parts = urllib.parse.urlsplit((url or "").strip())
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise AccountError("insecure_url", "The account server must use HTTPS")
    if not parts.hostname:
        raise AccountError("invalid_url", "Enter the account server address")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


class AccountClient:
    def __init__(self, api_url: str, *, store: SessionStore | None = None, transport: Transport | None = None,
                 platform: str = "linux", device_name: str | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.api_url = validate_api_url(api_url)
        self.store = store or MemorySessionStore()
        self.transport = transport or default_transport()
        self.platform = platform
        self.device_name = device_name or PLATFORM_LABELS.get(platform, "Device")
        self._clock = clock
        self._access: tuple[str, float] | None = None       # (token, expires at on the monotonic clock)

    # --- state ------------------------------------------------------------------------------------

    @property
    def session(self) -> AccountSession | None:
        s = self.store.load()
        return s if s is not None and s.api_url == self.api_url else None

    @property
    def signed_in(self) -> bool:
        return self.session is not None

    def _device(self) -> dict[str, str]:
        return {"name": self.device_name, "platform": self.platform}

    # --- sign-up / sign-in ------------------------------------------------------------------------

    def signup(self, email: str, password: str) -> None:
        self._call("POST", "/v1/auth/signup", {"email": email, "password": password})

    def resend_verification(self, email: str) -> None:
        self._call("POST", "/v1/auth/verify/resend", {"email": email})

    def verify_email(self, email: str, code: str) -> dict[str, Any]:
        return self._start(self._call("POST", "/v1/auth/verify", {"email": email, "code": code,
                                                                  "device": self._device()}))

    def login(self, email: str, password: str) -> dict[str, Any]:
        return self._start(self._call("POST", "/v1/auth/login", {"email": email, "password": password,
                                                                 "device": self._device()}))

    def google(self, id_token: str) -> dict[str, Any]:
        return self._start(self._call("POST", "/v1/auth/google", {"id_token": id_token, "device": self._device()}))

    def forgot_password(self, email: str) -> None:
        self._call("POST", "/v1/auth/password/forgot", {"email": email})

    def reset_password(self, email: str, code: str, new_password: str) -> None:
        self._call("POST", "/v1/auth/password/reset", {"email": email, "code": code, "new_password": new_password})

    # --- signed-in calls --------------------------------------------------------------------------

    def account(self) -> dict[str, Any]:
        return self._authed("GET", "/v1/account")

    def devices(self) -> list[dict[str, Any]]:
        return self._authed("GET", "/v1/account/devices")["devices"]

    def revoke_device(self, device_id: str) -> None:
        self._authed("DELETE", f"/v1/account/devices/{urllib.parse.quote(device_id, safe='')}")

    def change_password(self, current: str, new_password: str) -> None:
        self._authed("POST", "/v1/account/password", {"current_password": current, "new_password": new_password})

    def api(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Signed-in call to another API route (used by the sync engine)."""
        return self._authed(method, path, body)

    def logout(self) -> None:
        """Sign this device out. Local session is cleared even if the server can't be reached."""
        try:
            if self.signed_in:
                self._authed("POST", "/v1/auth/logout")
        except AccountError as exc:
            log.info("Server sign-out failed (%s); clearing the local session anyway", exc.code)
        finally:
            self._clear()

    def logout_all(self) -> int:
        revoked = int(self._authed("POST", "/v1/account/logout-all").get("revoked", 0))
        self._clear()
        return revoked

    def delete_account(self) -> None:
        self._authed("DELETE", "/v1/account")
        self._clear()

    # --- internals --------------------------------------------------------------------------------

    def _start(self, data: dict[str, Any]) -> dict[str, Any]:
        account = data["account"]
        self.store.save(AccountSession(self.api_url, account["id"], account["email"], data["device_id"],
                                       data["refresh_token"]))
        self._access = (data["access_token"], self._clock() + float(data["expires_in"]))
        return account

    def _clear(self) -> None:
        self._access = None
        self.store.save(None)

    def _refresh(self) -> None:
        session = self.session
        if session is None:
            raise AccountError("signed_out", "Sign in to your account")
        try:
            data = self._call("POST", "/v1/auth/refresh", {"refresh_token": session.refresh_token})
        except AccountError as exc:
            if exc.status == 401:
                self._clear()
                raise AccountError("signed_out", exc.message or "Sign in again", 401) from None
            raise
        self._start(data)

    def _access_token(self) -> str:
        if self._access is None or self._access[1] - REFRESH_MARGIN_S <= self._clock():
            self._refresh()
        assert self._access is not None
        return self._access[0]

    def _authed(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in (1, 2):
            try:
                return self._call(method, path, body, token=self._access_token())
            except AccountError as exc:
                if exc.status == 401 and attempt == 1 and exc.code != "signed_out":
                    self._access = None                 # stale/revoked access token: refresh once and retry
                    continue
                if exc.status == 401:
                    self._clear()
                    raise AccountError("signed_out", "Sign in again", 401) from None
                raise
        raise AccountError("signed_out", "Sign in again", 401)

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None, *,
              token: str | None = None) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = self.transport.request(method, self.api_url + path, headers=headers,
                                          body=json.dumps(body).encode() if body is not None else None, timeout=20)
        except ProviderError as exc:
            raise AccountError("offline", "Can't reach the account server. Check your connection.") from exc
        if resp.status == 204 or not resp.body:
            if resp.status >= 400:
                raise AccountError("server_error", f"Account server error ({resp.status})", resp.status)
            return {}
        try:
            data = json.loads(resp.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AccountError("server_error", f"Unexpected response from the account server ({resp.status})",
                               resp.status) from None
        if resp.status >= 400:
            retry = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            raise AccountError(str(data.get("error", "error")), str(data.get("message", "Something went wrong")),
                               resp.status, float(retry) if retry and retry.isdigit() else None)
        return data
