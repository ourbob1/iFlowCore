"""
HTTP API (FastAPI). All routes under /v1; JSON in and out; errors are {"error": code, "message": text}.

    POST   /v1/auth/signup              {email, password}                      → 202 (code emailed)
    POST   /v1/auth/verify/resend       {email}                                → 202
    POST   /v1/auth/verify              {email, code, device}                  → tokens
    POST   /v1/auth/login               {email, password, device}              → tokens
    POST   /v1/auth/google              {id_token, device}                     → tokens
    POST   /v1/auth/refresh             {refresh_token}                        → tokens (rotated)
    POST   /v1/auth/password/forgot     {email}                                → 202
    POST   /v1/auth/password/reset      {email, code, new_password}            → 204 (all devices signed out)
    POST   /v1/auth/logout              (bearer)                               → 204
    GET    /v1/account                  (bearer)                               → account
    POST   /v1/account/password         (bearer) {current_password, new_password} → 204
    GET    /v1/account/devices          (bearer)                               → devices
    DELETE /v1/account/devices/{id}     (bearer)                               → 204
    POST   /v1/account/logout-all       (bearer)                               → {"revoked": n}
    DELETE /v1/account                  (bearer, signed in within 10 min)      → 204
    GET    /v1/sync/keys                (bearer)                               → {envelope, key_version}
    PUT    /v1/sync/keys                (bearer) {envelope, expected_version}  → {key_version} (409 if stale)
    POST   /v1/sync/push                (bearer) {items: [...]}                → {results, cursor}
    GET    /v1/sync/pull?after=&limit=  (bearer)                               → {items, cursor, more}
    GET    /v1/sync/status              (bearer)                               → counts
    DELETE /v1/sync                     (bearer, signed in within 10 min)      → 204 (erase synced data)
    GET    /healthz          liveness (load balancer health check; no database)
    GET    /readyz           readiness: the database answers (smoke tests, alarms)

    uvicorn iflow_api.app:create_app --factory
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from . import __version__
from .db import Base, make_engine, make_sessionmaker, utcnow
from .google import GoogleVerifier
from .mailer import Mailer, make_mailer
from .ratelimit import RateLimiter
from .security import AccessClaims
from .service import AccountService, AuthError, DeviceInfo, SessionTokens
from .sync import SyncService
from .settings import Settings

log = logging.getLogger("iflow_api")
MAX_BODY = 16 * 1024
MAX_SYNC_BODY = 8 * 1024 * 1024


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=512)


class DeviceIn(_In):
    name: str = Field(min_length=1, max_length=64)
    platform: str = Field(max_length=16)

    def info(self) -> DeviceInfo:
        return DeviceInfo(self.name, self.platform)


class SignupIn(_In):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class EmailIn(_In):
    email: str = Field(max_length=254)


class VerifyIn(_In):
    email: str = Field(max_length=254)
    code: str = Field(max_length=16)
    device: DeviceIn


class LoginIn(_In):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)
    device: DeviceIn


class GoogleIn(_In):
    id_token: str = Field(max_length=4096)
    device: DeviceIn


class RefreshIn(_In):
    refresh_token: str = Field(max_length=200)


class ResetIn(_In):
    email: str = Field(max_length=254)
    code: str = Field(max_length=16)
    new_password: str = Field(max_length=256)


class KeysIn(_In):
    model_config = ConfigDict(extra="forbid")
    envelope: dict[str, Any]
    expected_version: int | None = None


class PushIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[dict[str, Any]] = Field(max_length=200)


class ChangePasswordIn(_In):
    current_password: str = Field(default="", max_length=256)
    new_password: str = Field(max_length=256)


def _tokens(t: SessionTokens) -> dict[str, Any]:
    return {"access_token": t.access_token, "token_type": "Bearer", "expires_in": t.expires_in,
            "refresh_token": t.refresh_token, "refresh_expires_in": t.refresh_expires_in,
            "device_id": t.device_id, "account": t.user}


def create_app(settings: Settings | None = None, *, mailer: Mailer | None = None,
               google: GoogleVerifier | None = None, clock: Callable[[], datetime] = utcnow,
               limiter: RateLimiter | None = None, create_schema: bool | None = None,
               broker_post: Callable[..., Any] | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = make_engine(settings.database_url)
    if create_schema if create_schema is not None else not settings.is_production:
        Base.metadata.create_all(engine)              # production schema comes from Alembic migrations
    service = AccountService(settings, make_sessionmaker(engine),
                             mailer or make_mailer(settings.mail_backend, settings.mail_from, settings.ses_region),
                             google or GoogleVerifier(settings.google_client_ids, clock=None), clock=clock)
    limiter = limiter or RateLimiter()
    sync = SyncService(make_sessionmaker(engine), clock, settings.fresh_auth_s)

    app = FastAPI(title="iFlow Core API", version=__version__, docs_url=None if settings.is_production else "/docs",
                  redoc_url=None, openapi_url=None if settings.is_production else "/openapi.json")
    app.state.service, app.state.settings, app.state.engine, app.state.sync = service, settings, engine, sync
    if settings.cors_origins:               # the web app (iflowcore.com) calls the API from the browser
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
                           allow_methods=["GET", "POST", "PUT", "DELETE"],
                           allow_headers=["Authorization", "Content-Type"], max_age=600)

    def client_ip(request: Request) -> str:
        if settings.trust_proxy:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def limit(bucket: str, key: str) -> None:
        wait = limiter.hit(bucket, key)
        if wait:
            raise AuthError("rate_limited", "Too many requests. Try again shortly.", 429, retry_after=wait)

    def bearer(request: Request) -> AccessClaims:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthError("unauthorized", "Sign in first", 401)
        return service.authenticate(token.strip())

    @app.middleware("http")
    async def guard(request: Request, call_next):
        length = request.headers.get("content-length")
        cap = MAX_SYNC_BODY if request.url.path.startswith("/v1/sync/") else MAX_BODY
        if length and length.isdigit() and int(length) > cap:
            return JSONResponse({"error": "too_large", "message": "Request too large"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(AuthError)
    async def auth_error(_: Request, exc: AuthError):
        headers = {"Retry-After": str(int(exc.retry_after + 0.999))} if exc.retry_after else None
        if exc.status == 401:
            headers = {**(headers or {}), "WWW-Authenticate": "Bearer"}
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, exc: RequestValidationError):
        fields = sorted({".".join(str(p) for p in e.get("loc", ())[1:]) for e in exc.errors()})
        return JSONResponse({"error": "invalid_request", "message": "Check the highlighted fields",
                             "fields": fields}, status_code=422)            # never echoes submitted values

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:                        # the reason stays in the log, not the response
            log.warning("Readiness check failed: %s", type(exc).__name__)
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ready", "version": __version__})

    @app.post("/v1/auth/signup", status_code=202)
    def signup(body: SignupIn, request: Request) -> dict[str, str]:
        limit("signup:ip", client_ip(request))
        limit("code:email", body.email.strip().lower())
        service.signup(body.email, body.password)
        return {"status": "verification_sent"}

    @app.post("/v1/auth/verify/resend", status_code=202)
    def resend(body: EmailIn, request: Request) -> dict[str, str]:
        limit("verify:ip", client_ip(request))
        limit("code:email", body.email.strip().lower())
        service.resend_verification(body.email)
        return {"status": "verification_sent"}

    @app.post("/v1/auth/verify")
    def verify(body: VerifyIn, request: Request) -> dict[str, Any]:
        limit("verify:ip", client_ip(request))
        return _tokens(service.verify_email(body.email, body.code, body.device.info()))

    @app.post("/v1/auth/login")
    def login(body: LoginIn, request: Request) -> dict[str, Any]:
        limit("login:ip", client_ip(request))
        limit("login:email", body.email.strip().lower())
        return _tokens(service.login(body.email, body.password, body.device.info()))

    @app.post("/v1/auth/google")
    def google_login(body: GoogleIn, request: Request) -> dict[str, Any]:
        limit("google:ip", client_ip(request))
        return _tokens(service.google_login(body.id_token, body.device.info()))

    @app.post("/v1/auth/refresh")
    def refresh(body: RefreshIn, request: Request) -> dict[str, Any]:
        limit("refresh:ip", client_ip(request))
        return _tokens(service.refresh(body.refresh_token))

    @app.post("/v1/auth/password/forgot", status_code=202)
    def forgot(body: EmailIn, request: Request) -> dict[str, str]:
        limit("verify:ip", client_ip(request))
        limit("code:email", body.email.strip().lower())
        service.forgot_password(body.email)
        return {"status": "reset_sent"}

    @app.post("/v1/auth/password/reset", status_code=204)
    def reset(body: ResetIn, request: Request) -> Response:
        limit("verify:ip", client_ip(request))
        service.reset_password(body.email, body.code, body.new_password)
        return Response(status_code=204)

    @app.post("/v1/auth/logout", status_code=204)
    def logout(claims: AccessClaims = Depends(bearer)) -> Response:
        service.logout(claims)
        return Response(status_code=204)

    @app.get("/v1/account")
    def account(claims: AccessClaims = Depends(bearer)) -> dict[str, Any]:
        return service.account(claims)

    @app.post("/v1/account/password", status_code=204)
    def change_password(body: ChangePasswordIn, claims: AccessClaims = Depends(bearer)) -> Response:
        service.change_password(claims, body.current_password, body.new_password)
        return Response(status_code=204)

    @app.get("/v1/account/devices")
    def devices(claims: AccessClaims = Depends(bearer)) -> dict[str, Any]:
        return {"devices": service.devices(claims)}

    @app.delete("/v1/account/devices/{device_id}", status_code=204)
    def revoke(device_id: str, claims: AccessClaims = Depends(bearer)) -> Response:
        service.revoke_device(claims, device_id)
        return Response(status_code=204)

    @app.post("/v1/account/logout-all")
    def logout_all(claims: AccessClaims = Depends(bearer)) -> dict[str, int]:
        return {"revoked": service.logout_all(claims)}

    @app.delete("/v1/account", status_code=204)
    def delete_account(claims: AccessClaims = Depends(bearer)) -> Response:
        service.delete_account(claims)
        return Response(status_code=204)

    @app.get("/v1/sync/keys")
    def get_keys(claims: AccessClaims = Depends(bearer)) -> dict[str, Any]:
        return sync.get_keys(claims)

    @app.put("/v1/sync/keys")
    def put_keys(body: KeysIn, claims: AccessClaims = Depends(bearer)) -> dict[str, int]:
        return {"key_version": sync.put_keys(claims, body.envelope, body.expected_version)}

    @app.post("/v1/sync/push")
    def push(body: PushIn, claims: AccessClaims = Depends(bearer)) -> dict[str, Any]:
        return sync.push(claims, body.items)

    @app.get("/v1/sync/pull")
    def pull(after: int = 0, limit: int = 500, claims: AccessClaims = Depends(bearer)) -> dict[str, Any]:
        return sync.pull(claims, max(0, after), limit)

    @app.get("/v1/sync/status")
    def sync_status(claims: AccessClaims = Depends(bearer)) -> dict[str, Any]:
        return sync.status(claims)

    @app.delete("/v1/sync", status_code=204)
    def sync_reset(claims: AccessClaims = Depends(bearer)) -> Response:
        sync.reset(claims)
        return Response(status_code=204)

    @app.post("/v1/integrations/{integration_id}/token")
    def integration_token(integration_id: str, body: dict[str, Any], request: Request) -> JSONResponse:
        """OAuth token broker: adds the platform's client secret (see broker.py). Stores and logs nothing."""
        from . import broker

        limit("integration:ip", client_ip(request))
        client = settings.integration_clients.get(integration_id)
        if client is None:
            return JSONResponse({"error": "unknown_integration"}, status_code=404)
        try:
            tokens = broker.exchange(client, body, **({"post": broker_post} if broker_post else {}))
        except broker.BrokerError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)
        return JSONResponse(tokens, headers={"Cache-Control": "no-store"})

    return app
