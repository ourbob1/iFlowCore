"""
Admin portal API (Phase 11), mounted at /admin/v1.

Who can call it
---------------
Operators holding an admin bearer token (RS_AI_ADMIN_TOKENS). Only SHA-256 digests of the tokens
are kept in memory, and a presented token is compared against *every* digest with
hmac.compare_digest (no early exit), so timing doesn't reveal which or how many tokens exist.
Client tokens (RS_AI_TOKENS, given to app installs) never grant admin access. Failed admin
attempts are limited per client IP (default 10 per 10 minutes), then refused with 429.
Admin routes answer 404 when no admin token is configured, so they aren't advertised.

What it touches
---------------
- licenses:  signs RSL1 tokens with RS_AI_LICENSE_SIGNING_KEY (Ed25519) and records each issue in a
             ledger (admin.db): license id, tier, period, terms, integer cents, dates, an optional
             opaque customer reference. No names, emails or payment details, ever.
- telemetry: the crash-report store, which only ever held scrubbed events. Issues show install
             *counts*; install ids are removed from event bodies before they leave this API.
- health:    uptime, container, model/OCR state, queue, request counters, disk.
User data never reaches this server: app data is end-to-end encrypted or stays on devices.

Every admin action (not reads) is written to an audit log: time, which admin slot, action, target.
Tokens themselves are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import platform
import re
import shutil
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .admin_users import ROLE_LABELS, ROLE_PERMISSIONS, AccountError, AdminUsers, Principal
from .licensing import (
    PRICE_BOOK,
    TIERS,
    LicenseIssueError,
    inspect,
    issue,
    load_signing_key,
    public_key_hex,
)

from .parsers import ParserSchemaError, ParserStore
from .format_publisher import PublishError
from .parsers import now_iso
from .parsers import sign_bundle as sign_parser_bundle

log = logging.getLogger("iflow_ai.admin")
_FP = re.compile(r"^[0-9a-f]{16,64}$")
_REF = re.compile(r"^[A-Za-z0-9._:#-]{1,64}$")
_EMAILISH = re.compile(r"@|\s")


# --- auth ----------------------------------------------------------------------------------------


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


class AdminGate:
    def __init__(self, tokens: tuple[str, ...], *, failures_allowed: int = 10, window_s: float = 600,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._digests = [_digest(t) for t in tokens]
        self.failures_allowed, self.window_s, self._clock = failures_allowed, window_s, clock
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._digests)

    def match(self, token: str) -> int | None:
        """Index of the matching admin token, or None. Checks every digest (constant work)."""
        presented = _digest(token)
        found = None
        for i, known in enumerate(self._digests):
            if hmac.compare_digest(presented, known) and found is None:
                found = i
        return found

    def _recent(self, ip: str) -> deque[float]:
        q = self._failures.setdefault(ip, deque())
        now = self._clock()
        while q and now - q[0] >= self.window_s:
            q.popleft()
        return q

    def guard(self, ip: str) -> None:
        """429 while this IP is locked out after too many failed admin attempts."""
        with self._lock:
            q = self._recent(ip)
            if len(q) >= self.failures_allowed:
                retry = max(1, int(self.window_s - (self._clock() - q[0])))
                raise HTTPException(429, "Too many failed admin sign-ins; try again later",
                                    headers={"Retry-After": str(retry)})

    def fail(self, ip: str) -> None:
        with self._lock:
            self._recent(ip).append(self._clock())
        log.warning("admin authentication failed from %s", ip)

    def resolve(self, token: str | None, ip: str, users: Any = None) -> Principal:
        """Principal for a console session token ("ias_…") or a service token; raises 401/403/404/429."""
        if not self.enabled and not (users is not None and users.count()):
            raise HTTPException(404, "Not found")
        self.guard(ip)
        principal = None
        if token and token.startswith("ias_") and users is not None:
            principal = users.session(token)
        elif token:
            index = self.match(token)
            principal = Principal(f"token-{index}", "owner") if index is not None else None
        if principal is None:
            self.fail(ip)
            if token is not None and token.startswith("ias_"):      # expired, revoked or signed out
                raise HTTPException(401, "Your session has ended. Sign in again.")
            raise HTTPException(401 if token is None else 403, "Sign in required",
                                headers={"WWW-Authenticate": "Bearer"} if token is None else None)
        return principal

    def check(self, token: str | None, ip: str) -> str:
        """Legacy /v1/telemetry admin routes: service tokens only; returns the actor label."""
        if not self.enabled:
            raise HTTPException(404, "Not found")
        return self.resolve(token, ip).actor


# --- ledger & audit --------------------------------------------------------------------------------

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS licenses (
    license_id    TEXT PRIMARY KEY,
    tier          TEXT NOT NULL CHECK (tier IN ('pro', 'guild')),
    period        TEXT NOT NULL CHECK (period IN ('monthly', 'annual')),
    terms         INTEGER NOT NULL CHECK (terms BETWEEN 1 AND 36),
    currency      TEXT NOT NULL,
    amount_minor  INTEGER NOT NULL CHECK (amount_minor >= 0),
    complimentary INTEGER NOT NULL DEFAULT 0,
    issued_at     TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    customer_ref  TEXT,
    issued_by     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_licenses_issued ON licenses (issued_at);
CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AdminStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript("PRAGMA journal_mode=WAL;" + LEDGER_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def audit(self, actor: str, action: str, target: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO audit (at, actor, action, target) VALUES (?, ?, ?, ?)",
                               (_now_iso(), actor, action, target))

    def record_license(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO licenses (license_id, tier, period, terms, currency, amount_minor, complimentary, "
                "issued_at, expires_at, customer_ref, issued_by) VALUES (:license_id, :tier, :period, :terms, "
                ":currency, :amount_minor, :complimentary, :issued_at, :expires_at, :customer_ref, :issued_by)", row)

    def licenses(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM licenses ORDER BY issued_at DESC, rowid DESC LIMIT ?",
                                      (limit,)).fetchall()
        return [{**dict(r), "complimentary": bool(r["complimentary"])} for r in rows]

    def license(self, license_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM licenses WHERE license_id = ?", (license_id,)).fetchone()
        return dict(row) if row else None

    def revenue(self, months: int = 12) -> dict[str, Any]:
        """Integer-cent totals by issue month and tier (what was charged; complimentary = 0)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT substr(issued_at, 1, 7) AS month, tier, currency, count(*) AS licenses, "
                "sum(amount_minor) AS amount_minor FROM licenses GROUP BY month, tier, currency "
                "ORDER BY month DESC, tier").fetchall()
        by_month: dict[str, dict[str, Any]] = {}
        for r in rows:
            m = by_month.setdefault(r["month"], {"month": r["month"], "total_minor": 0, "licenses": 0, "tiers": {}})
            m["tiers"][r["tier"]] = {"licenses": r["licenses"], "amount_minor": int(r["amount_minor"])}
            m["total_minor"] += int(r["amount_minor"])
            m["licenses"] += r["licenses"]
        recent = sorted(by_month.values(), key=lambda m: m["month"], reverse=True)[:months]
        return {"currency": "USD", "months": recent,
                "total_minor": sum(m["total_minor"] for m in recent),
                "licenses": sum(m["licenses"] for m in recent)}

    def audit_log(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT at, actor, action, target FROM audit ORDER BY id DESC LIMIT ?",
                                      (limit,)).fetchall()
        return [dict(r) for r in rows]


# --- request statistics ------------------------------------------------------------------------------


@dataclass
class RequestStats:
    """Rolling counters for the health monitor (no paths with ids, no clients, no bodies)."""

    window: int = 2000
    started: float = field(default_factory=time.time)
    total: int = 0
    by_class: dict[str, int] = field(default_factory=dict)
    by_area: dict[str, int] = field(default_factory=dict)
    rate_limited: int = 0
    in_flight: int = 0
    waiting: int = 0
    _latencies: deque = field(default_factory=lambda: deque(maxlen=2000))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, path: str, status: int, ms: int) -> None:
        area = "/".join(path.split("/")[:3]) or "/"          # e.g. /v1/receipts (never ids or queries)
        with self._lock:
            self.total += 1
            key = f"{status // 100}xx"
            self.by_class[key] = self.by_class.get(key, 0) + 1
            self.by_area[area] = self.by_area.get(area, 0) + 1
            if status == 429:
                self.rate_limited += 1
            self._latencies.append(ms)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            lat = sorted(self._latencies)
            pct = (lambda q: lat[min(len(lat) - 1, int(q * len(lat)))] if lat else 0)
            return {"total": self.total, "by_class": dict(self.by_class), "by_area": dict(self.by_area),
                    "rate_limited": self.rate_limited, "in_flight": self.in_flight, "waiting": self.waiting,
                    "latency_ms": {"p50": pct(0.5), "p95": pct(0.95), "samples": len(lat)}}


def _in_container() -> bool:
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    try:
        return any(k in Path("/proc/1/cgroup").read_text() for k in ("docker", "containerd", "kubepods"))
    except OSError:
        return False


def _tesseract() -> dict[str, Any]:
    try:
        import pytesseract

        return {"available": True, "version": str(pytesseract.get_tesseract_version())}
    except Exception as exc:                                    # missing binary or package
        return {"available": False, "error": type(exc).__name__}


# --- routes --------------------------------------------------------------------------------------------


class IssueIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier: str = Field(pattern="^(pro|guild)$")
    period: str = Field(pattern="^(monthly|annual)$")
    terms: int = Field(default=1, ge=1, le=36)
    currency: str = Field(default="USD", pattern="^[A-Z]{3}$")
    complimentary: bool = False
    customer_ref: str | None = Field(default=None, max_length=64)


class TokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(max_length=2048)


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class PasswordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class UserIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(max_length=64)
    display_name: str = Field(default="", max_length=80)
    role: str = Field(pattern="^(owner|admin|developer|support)$")


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, max_length=80)
    role: str | None = Field(default=None, pattern="^(owner|admin|developer|support)$")
    active: bool | None = None


class SchemaIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    schema_: dict[str, Any] = Field(alias="schema")


class PurgeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    older_than_days: int = Field(ge=1, le=3650)


def build_router(services: Callable[[], Any], settings: Any, gate: AdminGate, ledger: AdminStore,
                 stats: RequestStats, users: AdminUsers, parsers: ParserStore, publisher: Any = None,
                 integrations: ParserStore | None = None) -> APIRouter:
    """`services()` returns the app's Services container (created at startup)."""
    signing_key = load_signing_key(settings.license_signing_key) if settings.license_signing_key else None
    public_hex = public_key_hex(signing_key) if signing_key else None
    router = APIRouter(prefix="/admin/v1")

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def any_session(request: Request) -> Principal:
        """Who is calling: a signed-in console user (session) or a service token (owner). Allowed while a
        password change is pending, for the auth routes only."""
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        token = token.strip() if scheme.lower() == "bearer" and token.strip() else None
        return gate.resolve(token, client_ip(request), users)

    def signed_in(p: Principal = Depends(any_session)) -> Principal:
        if p.must_change_password:
            raise HTTPException(403, "Choose a new password first", headers={"X-Password-Change": "required"})
        return p

    def need(permission: str) -> Callable[..., Principal]:
        def check(p: Principal = Depends(signed_in)) -> Principal:
            if not p.can(permission):
                raise HTTPException(403, "Your role doesn't allow this")
            return p
        return check

    def account_error(exc: AccountError) -> HTTPException:
        return HTTPException(exc.status, exc.message)

    # --- console sign-in ---

    @router.post("/auth/login")
    async def login(body: LoginIn, request: Request) -> dict[str, Any]:
        ip = client_ip(request)
        gate.guard(ip)
        try:
            token, p, user = await run_in_threadpool(users.login, body.username, body.password)
        except AccountError as exc:
            if exc.code == "invalid_credentials":
                gate.fail(ip)
            raise account_error(exc) from None
        await run_in_threadpool(ledger.audit, p.actor, "auth.login", None)
        return {"session_token": token, "idle_minutes": 30, "user": user}

    @router.post("/auth/logout")
    async def logout(p: Principal = Depends(any_session)) -> dict[str, bool]:
        await run_in_threadpool(users.logout, p)
        return {"signed_out": True}

    @router.get("/auth/me")
    async def me(p: Principal = Depends(any_session)) -> dict[str, Any]:
        user = await run_in_threadpool(users.get, p.user_id) if p.user_id else None
        return {"actor": p.actor, "role": p.role, "permissions": sorted(p.permissions),
                "must_change_password": p.must_change_password, "user": user,
                "license_signing": signing_key is not None}

    @router.post("/auth/password")
    async def change_password(body: PasswordIn, p: Principal = Depends(any_session)) -> dict[str, bool]:
        try:
            await run_in_threadpool(users.change_password, p, body.current_password, body.new_password)
        except AccountError as exc:
            raise account_error(exc) from None
        await run_in_threadpool(ledger.audit, p.actor, "auth.password_changed", None)
        return {"changed": True}

    # --- user administration ---

    @router.get("/users")
    async def list_users(p: Principal = Depends(need("users.manage"))) -> dict[str, Any]:
        return {"users": await run_in_threadpool(users.list),
                "roles": [{"id": r, "label": ROLE_LABELS[r], "permissions": sorted(ROLE_PERMISSIONS[r])}
                          for r in ROLE_PERMISSIONS]}

    @router.post("/users")
    async def create_user(body: UserIn, p: Principal = Depends(need("users.manage"))) -> dict[str, Any]:
        if body.role == "owner" and p.role != "owner":
            raise HTTPException(403, "Only owners can create owner accounts")
        try:
            user, temp = await run_in_threadpool(lambda: users.create(body.username, body.display_name, body.role,
                                                                      created_by=p.actor))
        except AccountError as exc:
            raise account_error(exc) from None
        await run_in_threadpool(ledger.audit, p.actor, "user.create", f"{user['username']} ({user['role']})")
        return {"user": user, "temporary_password": temp}

    @router.patch("/users/{user_id}")
    async def update_user(user_id: str, body: UserPatch, p: Principal = Depends(need("users.manage"))) -> dict:
        try:
            user = await run_in_threadpool(lambda: users.update(p, user_id, display_name=body.display_name,
                                                                role=body.role, active=body.active))
        except AccountError as exc:
            raise account_error(exc) from None
        changes = ", ".join(f"{k}={v}" for k, v in body.model_dump(exclude_none=True).items())
        await run_in_threadpool(ledger.audit, p.actor, "user.update", f"{user['username']}: {changes}")
        return {"user": user}

    @router.post("/users/{user_id}/reset-password")
    async def reset_password(user_id: str, p: Principal = Depends(need("users.manage"))) -> dict[str, Any]:
        try:
            temp = await run_in_threadpool(users.reset_password, p, user_id)
        except AccountError as exc:
            raise account_error(exc) from None
        user = await run_in_threadpool(users.get, user_id)
        await run_in_threadpool(ledger.audit, p.actor, "user.reset_password", user["username"])
        return {"temporary_password": temp}

    @router.post("/users/{user_id}/unlock")
    async def unlock_user(user_id: str, p: Principal = Depends(need("users.manage"))) -> dict[str, bool]:
        try:
            await run_in_threadpool(users.unlock, p, user_id)
        except AccountError as exc:
            raise account_error(exc) from None
        user = await run_in_threadpool(users.get, user_id)
        await run_in_threadpool(ledger.audit, p.actor, "user.unlock", user["username"])
        return {"unlocked": True}

    # --- Parser Studio ---

    def distribution_info() -> dict[str, Any]:
        return {"configured": publisher is not None, "target": getattr(publisher, "target", None),
                "public_url": getattr(publisher, "public_url", None), "last": parsers.last_distribution()}

    def distribute(actor: str) -> dict[str, Any]:
        """Sign every published format into one bundle and write it to the CDN target."""
        if publisher is None:
            raise HTTPException(503, "No CDN target: set RS_AI_FORMATS_BUCKET (or RS_AI_FORMATS_DIR) on the service")
        if signing_key is None:
            raise HTTPException(503, "Publishing needs RS_AI_LICENSE_SIGNING_KEY on the server")
        schemas = parsers.published()
        apis = integrations.published() if integrations is not None else []
        issued_at = now_iso()
        bundle = sign_parser_bundle(schemas, signing_key, issued_at=issued_at, integrations=apis)
        try:
            out = publisher.publish(bundle, issued_at, len(schemas)).to_dict()
        except PublishError as exc:
            raise HTTPException(502, str(exc)) from None
        parsers.record_distribution(out, actor)
        out["integrations"] = len(apis)
        ledger.audit(actor, "parser.distribute", f"{len(schemas)} format(s), {len(apis)} integration(s) "
                                                 f"sha256 {out['sha256'][:12]}")
        return out

    @router.get("/parsers")
    async def list_parsers(p: Principal = Depends(need("parsers.edit"))) -> dict[str, Any]:
        return {"schemas": await run_in_threadpool(parsers.list), "signing": signing_key is not None,
                "distribution": await run_in_threadpool(distribution_info)}

    @router.get("/parsers/distribution")
    async def distribution(p: Principal = Depends(need("parsers.edit"))) -> dict[str, Any]:
        return await run_in_threadpool(distribution_info)

    @router.post("/parsers/distribute")
    async def distribute_now(p: Principal = Depends(need("parsers.publish"))) -> dict[str, Any]:
        return await run_in_threadpool(distribute, p.actor)

    @router.get("/parsers/bundle")
    async def export_bundle(p: Principal = Depends(need("parsers.edit"))) -> dict[str, Any]:
        if signing_key is None:
            raise HTTPException(503, "Publishing needs RS_AI_LICENSE_SIGNING_KEY on the server")
        schemas = await run_in_threadpool(parsers.published)
        return {"bundle": sign_parser_bundle(schemas, signing_key), "schemas": len(schemas), "public_key": public_hex}

    @router.get("/parsers/{schema_id}")
    async def get_parser(schema_id: str, p: Principal = Depends(need("parsers.edit"))) -> dict[str, Any]:
        try:
            return await run_in_threadpool(parsers.get, schema_id)
        except KeyError:
            raise HTTPException(404, "No such statement format") from None

    @router.put("/parsers/{schema_id}")
    async def save_parser(schema_id: str, body: SchemaIn, p: Principal = Depends(need("parsers.edit"))) -> dict:
        if body.schema_.get("id") != schema_id:
            raise HTTPException(422, "Schema id doesn't match the URL")
        try:
            out = await run_in_threadpool(parsers.save_draft, body.schema_, p.actor)
        except ParserSchemaError as exc:
            raise HTTPException(422, str(exc)) from None
        await run_in_threadpool(ledger.audit, p.actor, "parser.save_draft", f"{schema_id} v{out['version']}")
        return out

    @router.post("/parsers/{schema_id}/publish")
    async def publish_parser(schema_id: str, p: Principal = Depends(need("parsers.publish"))) -> dict[str, Any]:
        if signing_key is None:
            raise HTTPException(503, "Publishing needs RS_AI_LICENSE_SIGNING_KEY on the server")
        try:
            out = await run_in_threadpool(parsers.publish, schema_id, p.actor)
        except KeyError:
            raise HTTPException(404, "There's no draft to publish") from None
        except ParserSchemaError as exc:
            raise HTTPException(422, str(exc)) from None
        await run_in_threadpool(ledger.audit, p.actor, "parser.publish", f"{schema_id} v{out['version']}")
        if publisher is not None:                  # ship it: apps pick it up from the CDN within a day
            try:
                out["distribution"] = await run_in_threadpool(distribute, p.actor)
            except HTTPException as exc:
                out["distribution_error"] = exc.detail   # published here; retry with /parsers/distribute
        return out

    # --- Integration Studio (platform API connections) ---

    def _integrations() -> ParserStore:
        if integrations is None:
            raise HTTPException(404, "Integrations aren't enabled on this server")
        return integrations

    @router.get("/integrations")
    async def list_integrations(p: Principal = Depends(need("parsers.edit"))) -> dict[str, Any]:
        return {"integrations": await run_in_threadpool(_integrations().list), "signing": signing_key is not None}

    @router.get("/integrations/{schema_id}")
    async def get_integration(schema_id: str, p: Principal = Depends(need("parsers.edit"))) -> dict[str, Any]:
        try:
            return await run_in_threadpool(_integrations().get, schema_id)
        except KeyError:
            raise HTTPException(404, "No such integration") from None

    @router.put("/integrations/{schema_id}")
    async def save_integration(schema_id: str, body: SchemaIn, p: Principal = Depends(need("parsers.edit"))) -> dict:
        if body.schema_.get("id") != schema_id:
            raise HTTPException(422, "Schema id doesn't match the URL")
        try:
            out = await run_in_threadpool(_integrations().save_draft, body.schema_, p.actor)
        except ParserSchemaError as exc:
            raise HTTPException(422, str(exc)) from None
        await run_in_threadpool(ledger.audit, p.actor, "integration.save_draft", f"{schema_id} v{out['version']}")
        return out

    @router.post("/integrations/{schema_id}/publish")
    async def publish_integration(schema_id: str, p: Principal = Depends(need("parsers.publish"))) -> dict[str, Any]:
        if signing_key is None:
            raise HTTPException(503, "Publishing needs RS_AI_LICENSE_SIGNING_KEY on the server")
        try:
            out = await run_in_threadpool(_integrations().publish, schema_id, p.actor)
        except KeyError:
            raise HTTPException(404, "There's no draft to publish") from None
        except ParserSchemaError as exc:
            raise HTTPException(422, str(exc)) from None
        await run_in_threadpool(ledger.audit, p.actor, "integration.publish", f"{schema_id} v{out['version']}")
        if publisher is not None:
            try:
                out["distribution"] = await run_in_threadpool(distribute, p.actor)
            except HTTPException as exc:
                out["distribution_error"] = exc.detail
        return out

    @router.get("/whoami")
    async def whoami(p: Principal = Depends(any_session)) -> dict[str, Any]:
        return {"actor": p.actor, "role": p.role, "permissions": sorted(p.permissions), "must_change_password": p.must_change_password, "version": __version__, "license_signing": signing_key is not None}

    @router.get("/overview")
    async def overview(p: Principal = Depends(signed_in)) -> dict[str, Any]:
        counts = await run_in_threadpool(services().store.counts)
        revenue = await run_in_threadpool(ledger.revenue, 1)
        this_month = datetime.now(timezone.utc).strftime("%Y-%m")
        month = next((m for m in revenue["months"] if m["month"] == this_month), None)
        sees_money = p.can("licenses.read")
        return {"telemetry": counts, "licenses_this_month": (month["licenses"] if month else 0) if sees_money else None,
                "revenue_this_month_minor": (month["total_minor"] if month else 0) if sees_money else None,
                "currency": "USD",
                "uptime_s": int(time.time() - stats.started), "models": services().registry.status(),
                "license_signing": signing_key is not None}

    # --- licenses ---

    @router.get("/licenses/plans")
    async def plans(p: Principal = Depends(need('licenses.read'))) -> dict[str, Any]:
        return {"plans": [{"tier": t, "tier_name": TIERS[t], "period": p, "currency": c, "amount_minor": a}
                          for (t, p, c), a in PRICE_BOOK.items()],
                "public_key": public_hex}

    @router.post("/licenses")
    async def issue_license(body: IssueIn, p: Principal = Depends(need('licenses.issue'))) -> dict[str, Any]:
        if signing_key is None:
            raise HTTPException(503, "License signing isn't configured on this server (RS_AI_LICENSE_SIGNING_KEY)")
        ref = (body.customer_ref or "").strip() or None
        if ref is not None and (_EMAILISH.search(ref) or not _REF.match(ref)):
            # an opaque order/customer id only: this ledger must never hold emails or names
            raise HTTPException(422, "Customer reference must be an opaque id (letters, digits, . _ : # -), "
                                     "not an email or name")
        try:
            out = issue(signing_key, tier=body.tier, period=body.period, terms=body.terms, currency=body.currency,
                        complimentary=body.complimentary)
        except LicenseIssueError as exc:
            raise HTTPException(422, str(exc)) from None
        row = {"license_id": out.license_id, "tier": out.tier, "period": out.period, "terms": out.terms,
               "currency": out.currency, "amount_minor": out.amount_minor, "complimentary": int(body.complimentary),
               "issued_at": out.issued_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "expires_at": out.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "customer_ref": ref, "issued_by": p.actor}
        await run_in_threadpool(ledger.record_license, row)
        await run_in_threadpool(ledger.audit, p.actor, "license.issue", out.license_id)
        return {**row, "complimentary": body.complimentary, "token": out.token, "public_key": public_hex}

    @router.post("/licenses/verify")
    async def verify_license(body: TokenIn, p: Principal = Depends(need('licenses.read'))) -> dict[str, Any]:
        if public_hex is None:
            raise HTTPException(503, "License signing isn't configured on this server")
        try:
            info = inspect(body.token, public_hex)
        except LicenseIssueError as exc:
            return {"signature_valid": False, "error": str(exc)}
        record = await run_in_threadpool(ledger.license, str(info.get("license_id") or ""))
        expires = info.get("expires_at")
        info["expired"] = bool(expires and expires < datetime.now(timezone.utc).isoformat())
        info["in_ledger"] = record is not None
        info["matches_ledger"] = bool(record and record["tier"] == info["tier"]
                                      and record["amount_minor"] == info["amount_minor"])
        return info

    @router.get("/licenses")
    async def list_licenses(limit: int = 50, p: Principal = Depends(need('licenses.read'))) -> dict[str, Any]:
        return {"licenses": await run_in_threadpool(ledger.licenses, max(1, min(limit, 500)))}

    @router.get("/revenue")
    async def revenue(months: int = 12, p: Principal = Depends(need('licenses.read'))) -> dict[str, Any]:
        return await run_in_threadpool(ledger.revenue, max(1, min(months, 60)))

    # --- telemetry ---

    @router.get("/telemetry/issues")
    async def issues(status: str | None = None, limit: int = 50, p: Principal = Depends(need('telemetry.read'))) -> dict:
        if status not in (None, "open", "resolved"):
            raise HTTPException(422, "status must be open or resolved")
        rows = await run_in_threadpool(services().store.issues, max(1, min(limit, 500)), status)
        return {"issues": rows, "counts": await run_in_threadpool(services().store.counts)}

    @router.get("/telemetry/issues/{fingerprint}")
    async def issue_events(fingerprint: str, limit: int = 20, p: Principal = Depends(need('telemetry.read'))) -> dict:
        if not _FP.match(fingerprint):
            raise HTTPException(404, "Unknown issue")
        events = await run_in_threadpool(services().store.events_for, fingerprint, max(1, min(limit, 200)))
        return {"events": [{k: v for k, v in e.items() if k != "install_id"} for e in events]}

    @router.post("/telemetry/issues/{fingerprint}/{action}")
    async def triage(fingerprint: str, action: str, p: Principal = Depends(need('telemetry.triage'))) -> dict[str, Any]:
        if not _FP.match(fingerprint) or action not in ("resolve", "reopen"):
            raise HTTPException(404, "Unknown issue")
        ok = await run_in_threadpool(services().store.set_status, fingerprint, "resolved" if action == "resolve" else "open")
        if not ok:
            raise HTTPException(404, "Unknown issue")
        await run_in_threadpool(ledger.audit, p.actor, f"telemetry.{action}", fingerprint)
        return {"fingerprint": fingerprint, "status": "resolved" if action == "resolve" else "open"}

    @router.post("/telemetry/purge")
    async def purge(body: PurgeIn, p: Principal = Depends(need('telemetry.triage'))) -> dict[str, int]:
        deleted = await run_in_threadpool(services().store.purge, body.older_than_days)
        await run_in_threadpool(ledger.audit, p.actor, "telemetry.purge", f"older_than_days={body.older_than_days}")
        return {"deleted_events": deleted}

    # --- health ---

    @router.get("/health")
    async def health(p: Principal = Depends(need('health.read'))) -> dict[str, Any]:
        data_dir = Path(settings.data_dir)
        try:
            disk = shutil.disk_usage(data_dir)
            disk_info = {"free_bytes": disk.free, "total_bytes": disk.total}
        except OSError:
            disk_info = {}
        db_bytes = sum(p.stat().st_size for p in data_dir.glob("*.db*") if p.is_file()) if data_dir.exists() else 0
        slots = services().slots
        return {
            "version": __version__, "python": platform.python_version(), "container": _in_container(),
            "uptime_s": int(time.time() - stats.started),
            "started_at": datetime.fromtimestamp(stats.started, timezone.utc).isoformat(),
            "models": services().registry.status(), "ocr": await run_in_threadpool(_tesseract),
            "backends": {"receipts": settings.receipt_backend, "tax": settings.tax_backend},
            "queue": {"max_concurrency": settings.max_concurrency, "slots_free": getattr(slots, "_value", None),
                      "waiting": stats.waiting, "timeout_s": settings.queue_timeout_s},
            "rate_limits_per_min": {"receipts": settings.rate_receipts_per_min, "tax": settings.rate_tax_per_min,
                                    "events": settings.rate_events_per_min},
            "requests": stats.snapshot(), "disk": {**disk_info, "databases_bytes": db_bytes},
            "load_avg": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        }

    @router.get("/audit")
    async def audit(limit: int = 50, p: Principal = Depends(need('audit.read'))) -> dict[str, Any]:
        return {"entries": await run_in_threadpool(ledger.audit_log, max(1, min(limit, 500)))}

    return router
