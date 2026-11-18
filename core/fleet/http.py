"""
Minimal HTTP layer for external providers (vehicle data, demand feeds, the
self-hosted AI service).

Stdlib only (urllib) so it works in Flet mobile builds without extra native
dependencies. Providers depend on the `Transport` protocol, which lets tests
inject a fake and lets a later phase swap in an async client.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from core import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"iFlowCore/{__version__} (+local-first gig worker suite)"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S = 30.0


class ProviderError(RuntimeError):
    """A provider call failed (network, HTTP, or payload)."""


class ProviderAuthError(ProviderError):
    """Credentials rejected or missing."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError(f"Invalid JSON from provider: {exc}") from exc


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        form: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 15.0,
    ) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None                      # the 3xx comes back as the response


class UrllibTransport:
    """Blocking HTTPS transport using the platform's default TLS settings.

    follow_redirects=False returns a 3xx as-is. Use it whenever a request carries a credential:
    urllib forwards the Authorization header to wherever a redirect points."""

    def __init__(self, *, follow_redirects: bool = True) -> None:
        self._open = (urllib.request.urlopen if follow_redirects
                      else urllib.request.build_opener(_NoRedirect).open)

    def request(self, method, url, *, params=None, form=None, headers=None, body=None, timeout=15.0) -> HttpResponse:
        if form is not None and body is not None:
            raise ValueError("Pass form or body, not both")
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"
        data = urllib.parse.urlencode(form).encode("ascii") if form is not None else body
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept", "application/json")
        if form is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self._open(req, timeout=timeout) as resp:  # noqa: S310 - https URLs from config
                return HttpResponse(resp.status, resp.read(), dict(resp.headers))
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, exc.read() or b"", dict(exc.headers or {}))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"Network error calling {urllib.parse.urlsplit(url).netloc}: {exc}") from exc


class XhrTransport:
    """Browser (Pyodide) transport: synchronous XMLHttpRequest, which web workers allow.
    The web app's Python runs in a worker, where sockets (urllib) don't exist. Cross-origin
    servers must send CORS headers for the browser to hand the response back."""

    def __init__(self, xhr_factory: Callable[[], Any] | None = None) -> None:
        self._new = xhr_factory

    def _xhr(self) -> Any:
        if self._new is not None:
            return self._new()
        import js  # type: ignore[import-not-found]  # Pyodide only

        return js.XMLHttpRequest.new()

    def request(self, method, url, *, params=None, form=None, headers=None, body=None, timeout=15.0) -> HttpResponse:
        if form is not None and body is not None:
            raise ValueError("Pass form or body, not both")
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"
        payload: Any = None
        if form is not None:
            payload = urllib.parse.urlencode(form)
        elif body is not None:
            payload = body.decode("utf-8")      # JSON bodies; binary uploads aren't used from the browser
        xhr = self._xhr()
        try:
            xhr.open(method, url, False)
            xhr.setRequestHeader("Accept", "application/json")
            if form is not None:
                xhr.setRequestHeader("Content-Type", "application/x-www-form-urlencoded")
            for k, v in (headers or {}).items():
                xhr.setRequestHeader(k, v)
            xhr.send(payload)
        except Exception as exc:                # a JS NetworkError (offline, CORS refusal, DNS)
            raise ProviderError(f"Network error calling {urllib.parse.urlsplit(url).netloc}") from exc
        if int(xhr.status) == 0:
            raise ProviderError(f"Network error calling {urllib.parse.urlsplit(url).netloc} (blocked or offline)")
        resp_headers = {}
        for line in str(xhr.getAllResponseHeaders() or "").splitlines():
            key, _, value = line.partition(":")
            if key.strip():
                resp_headers[key.strip().lower()] = value.strip()
        return HttpResponse(int(xhr.status), str(xhr.responseText or "").encode("utf-8"), resp_headers)


def in_browser() -> bool:
    import sys

    return sys.platform == "emscripten"


def default_transport() -> Transport:
    """urllib on desktop/mobile; synchronous XHR in the browser build."""
    return XhrTransport() if in_browser() else UrllibTransport()


def send_with_retry(
    transport: Transport,
    method: str,
    url: str,
    *,
    retries: int = 2,
    backoff_s: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> HttpResponse:
    """Send, retrying transient failures (429/5xx/network) with exponential
    backoff and honoring a numeric Retry-After header (capped)."""
    attempt = 0
    while True:
        try:
            resp = transport.request(method, url, **kwargs)
        except ProviderError:
            if attempt >= retries:
                raise
            delay = backoff_s * (2 ** attempt)
        else:
            if resp.status not in RETRYABLE_STATUS or attempt >= retries:
                return resp
            retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            try:
                delay = min(float(retry_after), MAX_RETRY_AFTER_S) if retry_after else backoff_s * (2 ** attempt)
            except ValueError:
                delay = backoff_s * (2 ** attempt)
        attempt += 1
        log.info("Retrying %s %s in %.1fs (attempt %d)", method, urllib.parse.urlsplit(url).netloc, delay, attempt)
        sleep(delay)
