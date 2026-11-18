"""
Client for the self-hosted iFlow Core AI service (services/ai).

One small HTTPS API on the user's own server:

    GET  /v1/status              model readiness (auth)
    POST /v1/receipts/extract    raw image/PDF body → extracted fields
    POST /v1/tax/ask             {question, context} → grounded answer
    POST /v1/telemetry/events    {events: [...]} → crash reports (opt-in)

Stdlib transport (core.fleet.http) so it works in Flet mobile builds. The
bearer token is a per-install secret held in the encrypted database (or
`IFLOW_AI_TOKEN`); it is never logged.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.parse
from typing import Any, Mapping

from core.fleet.http import HttpResponse, ProviderAuthError, ProviderError, Transport, default_transport, send_with_retry

ENV_AI_URL = "IFLOW_AI_URL"
ENV_AI_TOKEN = "IFLOW_AI_TOKEN"

MAX_QUESTION_CHARS = 500
RECEIPT_TIMEOUT_S = 180.0       # CPU inference on a small server can take a minute or two
TAX_TIMEOUT_S = 120.0
DEFAULT_TIMEOUT_S = 15.0


class AIServiceError(ProviderError):
    """The AI service is unreachable, misconfigured, or returned an error."""


class AIServiceBusy(AIServiceError):
    """The server is at capacity or still loading models; try again later."""


def _is_loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_service_url(url: str, *, allow_http: bool = False) -> str:
    """Normalize a base URL. HTTPS is required, except plain HTTP to loopback
    (always) or anywhere when `allow_http` (development builds only)."""
    raw = (url or "").strip().rstrip("/")
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise AIServiceError("Service URL must look like https://ai.example.com")
    if parts.username or parts.password:
        raise AIServiceError("Don't put credentials in the URL; use the access token field")
    if parts.query or parts.fragment:
        raise AIServiceError("Service URL can't have a query string")
    if parts.scheme == "http" and not (allow_http or _is_loopback(parts.hostname)):
        raise AIServiceError("Service URL must use https://")
    return raw


class AIServiceClient:
    def __init__(self, base_url: str, token: str, *, transport: Transport | None = None, allow_http: bool = False,
                 retries: int = 1, sleep: Any = None) -> None:
        if not token or not token.strip():
            raise AIServiceError("An access token is required")
        self.base_url = validate_service_url(base_url, allow_http=allow_http)
        self._token = token.strip()
        self.transport = transport or default_transport()
        self.retries = retries
        self._sleep_kw = {"sleep": sleep} if sleep else {}

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.base_url).hostname or ""

    def __repr__(self) -> str:                  # never show the token
        return f"AIServiceClient({self.base_url!r})"

    # -- plumbing ---------------------------------------------------------------------

    def _call(self, method: str, path: str, *, body: bytes | None = None, content_type: str | None = None,
              timeout: float = DEFAULT_TIMEOUT_S, extra_headers: Mapping[str, str] | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self._token}", **(extra_headers or {})}
        if content_type:
            headers["Content-Type"] = content_type
        try:
            resp = send_with_retry(self.transport, method, self.base_url + path, retries=self.retries,
                                   headers=headers, body=body, timeout=timeout, **self._sleep_kw)
        except ProviderError as exc:
            raise AIServiceError(f"Can't reach the AI service at {self.host}: {exc}") from exc
        return self._parse(resp)

    def _parse(self, resp: HttpResponse) -> Any:
        if resp.status in (401, 403):
            raise ProviderAuthError("The AI service rejected the access token")
        detail = ""
        if resp.status >= 400:
            try:
                payload = resp.json()
                detail = str(payload.get("detail", "")) if isinstance(payload, dict) else ""
            except ProviderError:
                detail = ""
            if resp.status in (429, 503):
                raise AIServiceBusy(detail or "The AI service is busy; try again in a minute")
            if resp.status == 413:
                raise AIServiceError(detail or "File is too large for the AI service")
            raise AIServiceError(f"AI service error {resp.status}" + (f": {detail}" if detail else ""))
        try:
            return resp.json()
        except ProviderError as exc:
            raise AIServiceError(str(exc)) from exc

    # -- endpoints --------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        data = self._call("GET", "/v1/status")
        if not isinstance(data, dict):
            raise AIServiceError("Unexpected status payload")
        return data

    def extract_receipt(self, content: bytes, mime_type: str, *, currency_hint: str | None = None,
                        locale: str | None = None) -> dict[str, Any]:
        if not content:
            raise AIServiceError("Receipt file is empty")
        extra = {}
        if currency_hint:
            extra["X-Currency-Hint"] = currency_hint
        if locale:
            extra["X-Locale"] = locale
        data = self._call("POST", "/v1/receipts/extract", body=content, content_type=mime_type,
                          timeout=RECEIPT_TIMEOUT_S, extra_headers=extra)
        if not isinstance(data, dict):
            raise AIServiceError("Unexpected receipt payload")
        return data

    def ask_tax(self, question: str, context: Mapping[str, Any]) -> dict[str, Any]:
        q = " ".join((question or "").split())
        if not q:
            raise AIServiceError("Type a question first")
        if len(q) > MAX_QUESTION_CHARS:
            raise AIServiceError(f"Keep questions under {MAX_QUESTION_CHARS} characters")
        body = json.dumps({"question": q, "context": dict(context)}).encode("utf-8")
        data = self._call("POST", "/v1/tax/ask", body=body, content_type="application/json", timeout=TAX_TIMEOUT_S)
        if not isinstance(data, dict) or not isinstance(data.get("answer"), str):
            raise AIServiceError("Unexpected answer payload")
        return data

    def send_events(self, events: list[dict[str, Any]]) -> int:
        """Telemetry sender for ErrorReporter.flush."""
        body = json.dumps({"events": events}, separators=(",", ":")).encode("utf-8")
        data = self._call("POST", "/v1/telemetry/events", body=body, content_type="application/json")
        accepted = data.get("accepted") if isinstance(data, dict) else None
        return int(accepted) if isinstance(accepted, int) else len(events)
