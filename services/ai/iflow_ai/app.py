"""
iFlow Core AI service (FastAPI).

    uvicorn iflow_ai.app:create_app --factory --host 0.0.0.0 --port 8080

Endpoints
---------
GET  /healthz                 liveness for Docker (no auth, no details)
GET  /v1/status               model readiness                       [client token]
POST /v1/receipts/extract     raw image/PDF body → receipt fields   [client token]
POST /v1/tax/ask              {question, context} → answer          [client token]
POST /v1/telemetry/events     {events: [...]} → stored crash reports [client token]
GET  /v1/telemetry/issues     grouped crash reports                 [admin token]
GET  /v1/telemetry/issues/{fingerprint}                              [admin token]

Privacy: receipt bodies are decoded in memory and dropped after the
response; nothing but sizes and timings is logged. Tax requests carry only
aggregate figures. Crash reports arrive scrubbed by the app.

Capacity: CPU inference is serialized by a semaphore (RS_AI_MAX_CONCURRENCY);
requests that can't get a slot within RS_AI_QUEUE_TIMEOUT_S get 503 with
Retry-After, and each token has a per-minute rate limit (429).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from iflow_ai import __version__
from iflow_ai.admin import AdminGate, AdminStore, RequestStats, build_router
from iflow_ai.admin_users import AdminUsers
from iflow_ai import format_publisher
from iflow_ai.parsers import ParserStore, validate_integration
from iflow_ai.imaging import ALLOWED_TYPES, DocumentError, load_document, prepare_for_ocr, prepare_for_vlm
from iflow_ai.models import HFTextGenerator, HFVisionReader, ModelRegistry, TesseractOCR
from iflow_ai.receipts import EXTRACTION_SYSTEM, extraction_prompt, heuristics, merge, parse_json_object
from iflow_ai.settings import Settings
from iflow_ai.tax import (
    DISCLAIMER,
    OFF_TOPIC_REPLY,
    SYSTEM_PROMPT,
    QuestionError,
    clean_question,
    fallback_answer,
    is_on_topic,
    looks_like_injection,
    quick_answer,
    tidy_answer,
    ungrounded_numbers,
    user_prompt,
)
from iflow_ai.telemetry import TelemetryStore

log = logging.getLogger("iflow_ai")
MAX_JSON_BYTES = 512 * 1024


# -- request models --------------------------------------------------------------------


class TaxQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    context: dict[str, Any] = Field(default_factory=dict)


class Frame(BaseModel):
    model_config = ConfigDict(extra="ignore")
    file: str = Field(max_length=300)
    line: int | None = None
    function: str = Field(max_length=200)
    in_app: bool = False


_FP = re.compile(r"^[0-9a-f]{16,64}$")


class Event(BaseModel):
    model_config = ConfigDict(extra="ignore")
    schema_: int = Field(alias="schema", ge=1, le=10)
    id: str = Field(min_length=36, max_length=36)
    timestamp: str = Field(max_length=40)
    first_seen: str = Field(max_length=40)
    last_seen: str = Field(max_length=40)
    count: int = Field(ge=1, le=1_000_000)
    level: Literal["fatal", "critical", "error", "warning", "info"]
    kind: Literal["exception", "message"]
    type: str = Field(max_length=200)
    message: str = Field(max_length=1000)
    frames: list[Frame] = Field(default_factory=list, max_length=50)
    fingerprint: str
    handled: bool = True
    source: str = Field(default="", max_length=100)
    context: dict[str, str] = Field(default_factory=dict, max_length=10)
    release: str = Field(max_length=40)
    environment: str = Field(max_length=20)
    platform: str = Field(max_length=20)
    runtime: str = Field(default="", max_length=40)
    install_id: str = Field(min_length=36, max_length=36)
    cause: str | None = Field(default=None, max_length=400)

    @field_validator("fingerprint")
    @classmethod
    def _fp(cls, v: str) -> str:
        if not _FP.match(v):
            raise ValueError("bad fingerprint")
        return v


class EventBatch(BaseModel):
    events: list[Event] = Field(max_length=100)


# -- auth & limits -----------------------------------------------------------------------


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


class RateLimiter:
    """Sliding one-minute window per (token, bucket)."""

    def __init__(self, clock: Any = time.monotonic) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._clock = clock

    def check(self, who: str, bucket: str, per_minute: int) -> None:
        now = self._clock()
        q = self._hits[(who, bucket)]
        while q and now - q[0] >= 60:
            q.popleft()
        if len(q) >= per_minute:
            retry = max(1, int(60 - (now - q[0])))
            raise HTTPException(429, "Too many requests; slow down", headers={"Retry-After": str(retry)})
        q.append(now)


class Services:
    """Everything a request handler needs; built once per app."""

    def __init__(self, settings: Settings, registry: ModelRegistry, store: TelemetryStore,
                 gate: AdminGate | None = None, stats: RequestStats | None = None) -> None:
        self.settings = settings
        self.registry = registry
        self.store = store
        self.limiter = RateLimiter()
        self.slots = asyncio.Semaphore(settings.max_concurrency)
        self._clients = [_digest(t) for t in settings.tokens]
        self.gate = gate or AdminGate(settings.admin_tokens, failures_allowed=settings.admin_failures_per_10min)
        self.stats = stats or RequestStats()

    def client_id(self, token: str) -> str | None:
        d = _digest(token)
        for i, known in enumerate(self._clients):
            if hmac.compare_digest(d, known):
                return f"client-{i}"
        return None

    def is_admin(self, token: str) -> bool:
        return self.gate.match(token) is not None

    async def run_model(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        self.stats.waiting += 1
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=self.settings.queue_timeout_s)
        except asyncio.TimeoutError:
            raise HTTPException(503, "Server is busy; try again shortly", headers={"Retry-After": "30"}) from None
        finally:
            self.stats.waiting -= 1
        self.stats.in_flight += 1
        try:
            return await run_in_threadpool(fn, *args, **kwargs)
        finally:
            self.stats.in_flight -= 1
            self.slots.release()


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    return token.strip()


def client(request: Request) -> str:
    svc: Services = request.app.state.services
    who = svc.client_id(_bearer(request))
    if who is None:
        raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": "Bearer"})
    return who


def admin(request: Request) -> None:
    svc: Services = request.app.state.services
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    svc.gate.check(token.strip() if scheme.lower() == "bearer" and token.strip() else None,
                   request.client.host if request.client else "unknown")


async def read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"File is larger than {limit // (1024 * 1024)} MB")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, f"File is larger than {limit // (1024 * 1024)} MB")
        chunks.append(chunk)
    return b"".join(chunks)


# -- receipt pipeline -------------------------------------------------------------------


def extract_receipt(svc: Services, content: bytes, mime: str, *, currency_hint: str | None,
                    locale: str | None) -> dict[str, Any]:
    started = time.perf_counter()
    backend = svc.settings.receipt_backend
    doc = load_document(content, mime)
    warnings: list[str] = []

    text = doc.text or ""
    ocr = svc.registry.get("ocr")
    if not text and doc.image is not None and ocr is not None:
        text = svc.registry.ensure("ocr").image_to_text(prepare_for_ocr(doc.image))
    h = heuristics(text, locale=locale)

    model_out: dict[str, Any] | None = None
    model_name = "heuristics"
    if backend in ("ocr_llm", "fake"):
        if text.strip():
            llm = svc.registry.ensure("llm")
            raw = llm.generate(EXTRACTION_SYSTEM, extraction_prompt(text, currency_hint=currency_hint),
                               max_new_tokens=svc.settings.max_new_tokens)
            model_out, model_name = parse_json_object(raw), llm.name
        else:
            warnings.append("No readable text found; try a sharper, flatter photo in good light")
    elif backend == "vlm" and doc.image is not None:
        vlm = svc.registry.ensure("vlm")
        hint = f" The user's usual currency is {currency_hint}." if currency_hint else ""
        raw = vlm.read(prepare_for_vlm(doc.image), EXTRACTION_SYSTEM, "Extract this receipt." + hint,
                       max_new_tokens=svc.settings.max_new_tokens)
        model_out, model_name = parse_json_object(raw), vlm.name
    if model_out is None and backend != "off" and (text.strip() or backend == "vlm"):
        warnings.append("The model's answer couldn't be read; fields come from text rules only")

    result = merge(model_out, h, has_text=bool(text.strip()))
    result["warnings"] = warnings + result["warnings"]
    result["model"] = model_name
    result["source"] = "pdf_text" if doc.text else ("ocr" if text else "none")
    result["pages"] = doc.pages
    result["duration_ms"] = int((time.perf_counter() - started) * 1000)
    return result


# -- app ------------------------------------------------------------------------------------


def build_registry(settings: Settings) -> ModelRegistry:
    reg = ModelRegistry()
    uses_llm = settings.receipt_backend == "ocr_llm" or settings.tax_backend == "llm"
    reg.register("llm", HFTextGenerator(settings.llm_model, settings.llm_revision) if uses_llm else None)
    reg.register("vlm", HFVisionReader(settings.vlm_model, settings.vlm_revision)
                 if settings.receipt_backend == "vlm" else None)
    reg.register("ocr", TesseractOCR(settings.ocr_languages) if settings.receipt_backend in ("ocr_llm", "vlm") else None)
    return reg


def create_app(settings: Settings | None = None, *, registry: ModelRegistry | None = None,
               store: TelemetryStore | None = None, publisher: Any = "env") -> FastAPI:
    """publisher: "env" reads RS_AI_FORMATS_* (the deployed service); None means no CDN target."""
    settings = settings or Settings.from_env()
    registry = registry or build_registry(settings)
    store = store or TelemetryStore(settings.data_dir / "telemetry.db", retention_days=settings.telemetry_retention_days)
    admin_db = ":memory:" if str(getattr(store, "path", "")) == ":memory:" else settings.data_dir / "admin.db"
    ledger = AdminStore(admin_db)
    users = AdminUsers(admin_db)
    parsers = ParserStore(admin_db)
    integrations = ParserStore(admin_db, table="integration_schemas", validate=validate_integration)
    gate = AdminGate(settings.admin_tokens, failures_allowed=settings.admin_failures_per_10min)
    stats = RequestStats()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = Services(settings, registry, store, gate=gate, stats=stats)
        store.prune()
        if settings.preload:
            registry.warm()
        yield
        store.close()
        ledger.close()
        users.close()
        parsers.close()
        integrations.close()

    docs = "/docs" if settings.docs_enabled else None
    app = FastAPI(title="iFlow Core AI", version=__version__, lifespan=lifespan, docs_url=docs, redoc_url=None,
                  openapi_url="/openapi.json" if settings.docs_enabled else None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        started = time.perf_counter()
        declared = request.headers.get("content-length", "")
        if (request.url.path.startswith(("/v1/tax", "/v1/telemetry")) and declared.isdigit()
                and int(declared) > MAX_JSON_BYTES):
            return JSONResponse({"detail": "Request body too large"}, status_code=413)
        response = await call_next(request)
        stats.record(request.url.path, response.status_code, int((time.perf_counter() - started) * 1000))
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        log.info("%s %s %s %dms", request.method, request.url.path, response.status_code,
                 int((time.perf_counter() - started) * 1000))
        return response

    app.include_router(build_router(lambda: app.state.services, settings, gate, ledger, stats, users, parsers,
                                    format_publisher.from_env() if publisher == "env" else publisher,
                                    integrations))
    app.state.admin_ledger, app.state.admin_gate, app.state.admin_users = ledger, gate, users
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/status")
    async def status(request: Request, who: str = Depends(client)) -> dict[str, Any]:
        s = settings
        return {"version": __version__, "receipts": s.receipt_backend, "tax": s.tax_backend,
                "models": registry.status(), "max_upload_mb": s.max_upload_bytes // (1024 * 1024)}

    @app.post("/v1/receipts/extract")
    async def receipts(request: Request, who: str = Depends(client)) -> dict[str, Any]:
        svc: Services = request.app.state.services
        if settings.receipt_backend == "off":
            raise HTTPException(503, "Receipt extraction is turned off on this server")
        svc.limiter.check(who, "receipts", settings.rate_receipts_per_min)
        mime = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if mime not in ALLOWED_TYPES:
            raise HTTPException(415, f"Unsupported type '{mime}'. Send a JPEG, PNG, WebP, HEIC, TIFF or PDF body.")
        content = await read_body(request, settings.max_upload_bytes)
        if not content:
            raise HTTPException(400, "Empty body")
        currency = (request.headers.get("x-currency-hint") or "").strip().upper()[:3] or None
        locale = (request.headers.get("x-locale") or "").strip()[:10] or None
        try:
            result = await svc.run_model(extract_receipt, svc, content, mime, currency_hint=currency, locale=locale)
        except DocumentError as exc:
            raise HTTPException(422, str(exc)) from exc
        except (LookupError, RuntimeError) as exc:
            raise HTTPException(503, str(exc), headers={"Retry-After": "60"}) from exc
        finally:
            del content
        log.info("receipt %s → %s in %dms", mime, result["source"], result["duration_ms"])
        return result

    @app.post("/v1/tax/ask")
    async def tax(body: TaxQuestion, request: Request, who: str = Depends(client)) -> dict[str, Any]:
        svc: Services = request.app.state.services
        if settings.tax_backend == "off":
            raise HTTPException(503, "Tax questions are turned off on this server")
        svc.limiter.check(who, "tax", settings.rate_tax_per_min)
        try:
            question = clean_question(body.question)
            prompt = user_prompt(question, body.context)
        except QuestionError as exc:
            raise HTTPException(422, str(exc)) from exc
        if looks_like_injection(question) or not is_on_topic(question):
            return {"answer": OFF_TOPIC_REPLY, "model": "guard", "grounded": False, "disclaimer": DISCLAIMER}
        quick = quick_answer(question, body.context)
        if quick is not None:
            return {"answer": quick, "model": "rules", "grounded": True, "disclaimer": DISCLAIMER}

        def answer() -> tuple[str, str]:
            llm = svc.registry.ensure("llm")          # may load the model: keep it off the event loop
            return llm.generate(SYSTEM_PROMPT, prompt, max_new_tokens=settings.max_new_tokens), llm.name

        try:
            raw, model_name = await svc.run_model(answer)
        except (LookupError, RuntimeError) as exc:
            raise HTTPException(503, str(exc), headers={"Retry-After": "60"}) from exc
        answer = tidy_answer(raw)
        invented = ungrounded_numbers(answer, body.context)
        if invented:
            log.info("tax answer replaced: %d number(s) not in facts", len(invented))
            return {"answer": fallback_answer(body.context), "model": model_name, "grounded": False,
                    "disclaimer": DISCLAIMER, "replaced": True}
        return {"answer": answer, "model": model_name, "grounded": bool(body.context), "disclaimer": DISCLAIMER}

    @app.post("/v1/telemetry/events")
    async def events(batch: EventBatch, request: Request, who: str = Depends(client)) -> dict[str, int]:
        svc: Services = request.app.state.services
        svc.limiter.check(who, "events", settings.rate_events_per_min)
        payload = [e.model_dump(by_alias=True) for e in batch.events]
        new = await run_in_threadpool(svc.store.add, payload)
        return {"accepted": len(payload), "new": new}

    @app.get("/v1/telemetry/issues", dependencies=[Depends(admin)])
    async def issues(limit: int = 50) -> dict[str, Any]:
        return {"issues": await run_in_threadpool(store.issues, max(1, min(limit, 500)))}

    @app.get("/v1/telemetry/issues/{fingerprint}", dependencies=[Depends(admin)])
    async def issue_events(fingerprint: str, limit: int = 20) -> dict[str, Any]:
        if not _FP.match(fingerprint):
            raise HTTPException(404, "Unknown issue")
        events = await run_in_threadpool(store.events_for, fingerprint, max(1, min(limit, 200)))
        return {"events": [{k: v for k, v in e.items() if k != "install_id"} for e in events]}

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error on %s", request.url.path)
        return JSONResponse({"detail": "Internal error"}, status_code=500)

    return app
