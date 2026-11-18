"""
Brokerage & carrier integration (stubs + payload handling).

    BrokerAdapter           quote(request) → offers; enroll(payload) → receipt; status(ref) → receipt
      SandboxBroker         offline, deterministic "iFlow Guild" catalog for development and demos
      RestBroker            HTTPS JSON client for a digital-first brokerage / AHP carrier API

Rules every adapter follows
---------------------------
* Only *approved* brokerages: the sandbox, plus HTTPS hosts listed in
  `IFLOW_HEALTH_BROKERS` ("id|Name|https://api.example.com", comma-separated).
* Data minimization. A quote request carries state, age band, coverage tier
  and lines, nothing else: no name, birth date, earnings or platforms. An
  enrollment adds only what enrollment needs (legal name, contact email,
  birth date, association member id), is shown field by field on a consent
  screen first, and is never stored or logged. `compliance.check_payload`
  refuses platform, payroll and medical fields anywhere in a payload.
* Offers are validated on arrival: integer minor-unit premiums only (a float
  premium is rejected, not rounded), known lines, and co-employment rules
  (compliance.filter_offers).
* Broker tokens are secrets: held sealed in device settings, sent only as a
  bearer header to that broker's host, never logged. Error messages never
  echo payload values.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Mapping, Protocol

from core.health.compliance import check_payload, filter_offers
from core.health.models import (
    CoverageTier,
    EligibilityRules,
    EnrollmentReceipt,
    HealthError,
    Line,
    MemberProfile,
    PlanOffer,
)

ENV_BROKERS = "IFLOW_HEALTH_BROKERS"
SANDBOX_ID = "sandbox"
CONSENT_VERSION = 1


class BrokerError(HealthError):
    """A brokerage call failed. Messages never contain payload values."""


# --- registry -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BrokerInfo:
    id: str
    name: str
    kind: str                       # sandbox | rest
    base_url: str | None = None


def approved_brokers(env: Mapping[str, str] | None = None) -> dict[str, BrokerInfo]:
    env = os.environ if env is None else env
    out = {SANDBOX_ID: BrokerInfo(SANDBOX_ID, "iFlow Guild Sandbox (demo)", "sandbox")}
    for entry in (e.strip() for e in env.get(ENV_BROKERS, "").split(",") if e.strip()):
        parts = entry.split("|")
        if len(parts) != 3:
            continue
        bid, name, url = (p.strip() for p in parts)
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or not bid.isidentifier():
            continue                               # only clean HTTPS base URLs are ever approved
        out[bid] = BrokerInfo(bid, name, "rest", url.rstrip("/"))
    return out


# --- payloads -------------------------------------------------------------------------------------


def quote_payload(profile: MemberProfile, *, today: date, lines: Iterable[Line], currency: str = "USD",
                  coverage_tier: CoverageTier | None = None) -> dict:
    """The only data a quote needs. No identity, no earnings, no platforms."""
    payload = {
        "state": profile.state,
        "age_band": profile.age_band(today),
        "coverage_tier": (coverage_tier or profile.coverage_tier).value,
        "lines": sorted(line.value for line in lines),
        "currency": currency,
    }
    check_payload(payload)
    return payload


@dataclass(frozen=True)
class Applicant:
    """Entered on the enrollment screen, sent once, never stored."""

    legal_name: str
    email: str
    birth_date: date

    def __post_init__(self) -> None:
        if len(self.legal_name.strip()) < 2:
            raise HealthError("Enter your legal name")
        if "@" not in self.email or "." not in self.email.split("@")[-1]:
            raise HealthError("Enter a valid email")


def enrollment_payload(offer: PlanOffer, profile: MemberProfile, applicant: Applicant, *, coverage_start: date,
                       consented_at: str) -> dict:
    if applicant.birth_date.year != profile.birth_year:
        raise HealthError("Birth date doesn't match the birth year in your profile")
    payload = {
        "offer_id": offer.offer_id,
        "line": offer.line.value,
        "coverage_tier": offer.coverage_tier.value,
        "coverage_start": coverage_start.isoformat(),
        "applicant": {
            "legal_name": applicant.legal_name.strip(),
            "email": applicant.email.strip(),
            "birth_date": applicant.birth_date.isoformat(),
            "state": profile.state,
        },
        "association_member_id": profile.association_member_id,
        "consent": {"version": CONSENT_VERSION, "at": consented_at},
    }
    check_payload(payload)
    return payload


FIELD_LABELS = {
    "offer_id": "Plan", "line": "Coverage type", "coverage_tier": "Who's covered", "coverage_start": "Start date",
    "applicant.legal_name": "Legal name", "applicant.email": "Email", "applicant.birth_date": "Date of birth",
    "applicant.state": "State", "association_member_id": "Association member ID", "consent.version": "Consent version",
    "consent.at": "Consent time", "state": "State", "age_band": "Age band", "lines": "Coverage types",
    "currency": "Currency",
}
_MASKED = {"applicant.birth_date", "association_member_id", "applicant.email"}


def describe_payload(payload: Mapping, prefix: str = "") -> list[tuple[str, str]]:
    """(label, display value) for every field, for the consent screen. Sensitive values are partly masked
    on screen; the user sees exactly which fields go to the brokerage."""
    rows: list[tuple[str, str]] = []
    for k, v in payload.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, Mapping):
            rows += describe_payload(v, key)
            continue
        text = ", ".join(map(str, v)) if isinstance(v, (list, tuple)) else ("—" if v is None else str(v))
        if key.endswith("coverage_tier"):
            text = CoverageTier(text).label
        elif key == "line":
            text = Line(text).label
        if key in _MASKED and v:
            text = text[:2] + "•" * max(3, len(text) - 4) + text[-2:]
        rows.append((FIELD_LABELS.get(key, key), text))
    return rows


def redact(payload: Any) -> Any:
    """Shape-only copy for diagnostics: keys kept, every value replaced."""
    if isinstance(payload, Mapping):
        return {k: redact(v) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [redact(v) for v in payload]
    return "<redacted>"


# --- parsing --------------------------------------------------------------------------------------


def _int_money(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BrokerError(f"Brokerage sent a non-integer {what}; offer ignored")
    return value


def parse_offer(raw: Mapping, broker_id: str) -> PlanOffer:
    try:
        rules_raw = raw.get("eligibility") or {}
        rules = EligibilityRules(
            min_membership_days=rules_raw.get("min_membership_days"),
            states=tuple(rules_raw["states"]) if rules_raw.get("states") is not None else None,
            min_age=rules_raw.get("min_age"), max_age=rules_raw.get("max_age"),
            min_net_earnings_90d_minor=rules_raw.get("min_net_earnings_90d_minor"),
            min_hours_per_week=rules_raw.get("min_hours_per_week"), max_members=rules_raw.get("max_members"),
            requires_line=Line(rules_raw["requires_line"]) if rules_raw.get("requires_line") else None,
        )
        return PlanOffer(
            offer_id=str(raw["offer_id"]), broker_id=broker_id, association=str(raw["association"]),
            carrier=str(raw["carrier"]), line=Line(raw["line"]), name=str(raw["name"]),
            guild_tier=str(raw.get("guild_tier", "")),
            monthly_premium_minor=_int_money(raw["monthly_premium_minor"], "premium"),
            currency=str(raw["currency"]).upper(), coverage_tier=CoverageTier(raw["coverage_tier"]),
            sponsor_type=str(raw.get("sponsor_type", "association")),
            member_directed_payment=bool(raw.get("member_directed_payment", True)),
            platform_contribution_minor=_int_money(raw.get("platform_contribution_minor", 0), "platform contribution"),
            summary=tuple(str(x) for x in raw.get("summary", ()))[:6], rules=rules,
        )
    except BrokerError:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        raise BrokerError("Brokerage sent an offer in an unexpected format; offer ignored") from exc


@dataclass(frozen=True)
class QuoteResult:
    offers: tuple[PlanOffer, ...]
    refused: tuple[str, ...]          # compliance / format problems, shown to the user


def build_quote_result(raw_offers: Iterable[Mapping], broker_id: str) -> QuoteResult:
    parsed, problems = [], []
    for raw in raw_offers:
        try:
            parsed.append(parse_offer(raw, broker_id))
        except BrokerError as exc:
            problems.append(str(exc))
    ok, refused = filter_offers(parsed)
    return QuoteResult(tuple(ok), tuple(problems + refused))


# --- adapters -------------------------------------------------------------------------------------


class BrokerAdapter(Protocol):
    info: BrokerInfo

    def quote(self, payload: Mapping) -> QuoteResult: ...

    def enroll(self, payload: Mapping) -> EnrollmentReceipt: ...

    def status(self, enrollment_ref: str) -> EnrollmentReceipt: ...


def _div(n: int, d: int) -> int:
    return int((Decimal(n) / Decimal(d)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


AGE_FACTOR_BP = {"18-29": 8_500, "30-39": 10_000, "40-49": 12_600, "50-59": 17_800, "60-64": 24_000, "65+": 30_000}
TIER_FACTOR_BP = {"self": 10_000, "self_spouse": 20_000, "self_children": 18_500, "family": 28_500}
HEALTH_STATES = ("AZ", "CA", "FL", "GA", "IL", "NC", "NY", "OH", "PA", "TX")

# (offer key, line, guild tier, name, base premium for one 30–39 adult, age-rated?, rules, summary)
SANDBOX_CATALOG: tuple[tuple, ...] = (
    ("health-bronze", Line.HEALTH, "Bronze", "Guild Bronze PPO", 32_000, True,
     EligibilityRules(min_membership_days=30, states=HEALTH_STATES, min_age=18, max_age=64,
                      min_net_earnings_90d_minor=150_000, min_hours_per_week=10),
     ("$7,000 deductible", "Nationwide PPO network", "Preventive care at no cost")),
    ("health-silver", Line.HEALTH, "Silver", "Guild Silver PPO", 41_500, True,
     EligibilityRules(min_membership_days=30, states=HEALTH_STATES, min_age=18, max_age=64,
                      min_net_earnings_90d_minor=150_000, min_hours_per_week=10),
     ("$3,500 deductible", "Nationwide PPO network", "$25 primary-care visits")),
    ("health-gold", Line.HEALTH, "Gold", "Guild Gold PPO", 52_000, True,
     EligibilityRules(min_membership_days=30, states=HEALTH_STATES, min_age=18, max_age=64,
                      min_net_earnings_90d_minor=150_000, min_hours_per_week=10),
     ("$1,000 deductible", "Nationwide PPO network", "$15 primary-care visits")),
    ("dental-basic", Line.DENTAL, "Basic", "Guild Dental Basic", 2_400, False,
     EligibilityRules(min_membership_days=30), ("Two cleanings a year", "Fillings at 80%")),
    ("dental-plus", Line.DENTAL, "Plus", "Guild Dental Plus", 3_900, False,
     EligibilityRules(min_membership_days=30), ("Two cleanings a year", "Crowns at 50%", "Orthodontia for kids")),
    ("life-50k", Line.LIFE, "Term 50", "Guild Term Life $50,000", 1_100, True,
     EligibilityRules(min_age=18, max_age=64, max_members=1), ("$50,000 benefit", "No medical exam")),
    ("life-100k", Line.LIFE, "Term 100", "Guild Term Life $100,000", 1_900, True,
     EligibilityRules(min_age=18, max_age=64, max_members=1), ("$100,000 benefit", "No medical exam")),
)
# One deliberately non-compliant listing: shows the co-employment filter at work in demos and tests.
SANDBOX_PLATFORM_PLAN = {
    "offer_id": "sbx-platform-partner", "association": "RideCo Partner Benefits", "carrier": "Demo Mutual",
    "line": "health", "name": "RideCo Driver Health", "guild_tier": "Partner", "monthly_premium_minor": 19_900,
    "currency": "USD", "coverage_tier": "self", "sponsor_type": "platform", "platform_contribution_minor": 10_000,
    "member_directed_payment": False,
}


class SandboxBroker:
    """Offline brokerage with a deterministic catalog. Nothing leaves the device."""

    info = BrokerInfo(SANDBOX_ID, "iFlow Guild Sandbox (demo)", "sandbox")
    association = "iFlow Freelancers Guild"
    carrier = "Sandbox Mutual (demo)"

    def __init__(self) -> None:
        self._enrollments: dict[str, EnrollmentReceipt] = {}

    def raw_offers(self, payload: Mapping) -> list[dict]:
        tier = payload["coverage_tier"]
        lines = set(payload["lines"])
        out = []
        for key, line, gtier, name, base, age_rated, rules, summary in SANDBOX_CATALOG:
            if line.value not in lines:
                continue
            if line is Line.LIFE and tier != "self":
                continue                                           # term life covers the member only
            premium = base
            if age_rated:
                premium = _div(premium * AGE_FACTOR_BP[payload["age_band"]], 10_000)
            if line is not Line.LIFE:
                premium = _div(premium * TIER_FACTOR_BP[tier], 10_000)
            out.append({
                "offer_id": f"sbx-{key}-{tier}", "association": self.association, "carrier": self.carrier,
                "line": line.value, "name": name, "guild_tier": gtier, "monthly_premium_minor": premium,
                "currency": "USD", "coverage_tier": tier, "summary": list(summary),
                "eligibility": {
                    "min_membership_days": rules.min_membership_days, "states": list(rules.states) if rules.states else None,
                    "min_age": rules.min_age, "max_age": rules.max_age,
                    "min_net_earnings_90d_minor": rules.min_net_earnings_90d_minor,
                    "min_hours_per_week": rules.min_hours_per_week, "max_members": rules.max_members,
                },
            })
        if "health" in lines:
            out.append(dict(SANDBOX_PLATFORM_PLAN))
        return out

    def quote(self, payload: Mapping) -> QuoteResult:
        check_payload(payload)
        return build_quote_result(self.raw_offers(payload), SANDBOX_ID)

    def enroll(self, payload: Mapping) -> EnrollmentReceipt:
        check_payload(payload)
        if not str(payload.get("offer_id", "")).startswith("sbx-"):
            raise BrokerError("The sandbox doesn't know that plan")
        ref = "SBX-" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:10].upper()
        receipt = EnrollmentReceipt(SANDBOX_ID, payload["offer_id"], ref, "pending",
                                    date.fromisoformat(payload["coverage_start"]),
                                    "Sandbox enrollment received. No real coverage is created.")
        self._enrollments[ref] = receipt
        return receipt

    def status(self, enrollment_ref: str, *, today: date | None = None) -> EnrollmentReceipt:
        r = self._enrollments.get(enrollment_ref)
        if r is None:
            raise BrokerError("Unknown enrollment")
        if today and today >= r.coverage_start:
            return EnrollmentReceipt(r.broker_id, r.offer_id, r.enrollment_ref, "active", r.coverage_start, r.message)
        return r


class RestBroker:
    """HTTPS JSON adapter for an approved brokerage:

        POST {base}/v1/quotes                body: quote payload        → {"offers": [...]}
        POST {base}/v1/enrollments           body: enrollment payload   → {"enrollment_ref", "status", "coverage_start"}
        GET  {base}/v1/enrollments/{ref}                                 → same shape
    """

    def __init__(self, info: BrokerInfo, token: str, *, transport: Any = None) -> None:
        if info.kind != "rest" or not info.base_url or not info.base_url.startswith("https://"):
            raise BrokerError("Only approved HTTPS brokerages can be connected")
        if not token or not token.strip():
            raise BrokerError("This brokerage needs an access token")
        from core.fleet.http import default_transport

        self.info = info
        self._token = token.strip()
        self.transport = transport or default_transport()

    def __repr__(self) -> str:                       # never show the token
        return f"RestBroker({self.info.id!r})"

    def _call(self, method: str, path: str, payload: Mapping | None = None) -> Any:
        from core.fleet.http import ProviderError, send_with_retry

        body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
        headers = {"Authorization": f"Bearer {self._token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = send_with_retry(self.transport, method, self.info.base_url + path, headers=headers, body=body,
                                   retries=1 if method == "GET" else 0, timeout=30.0)
        except ProviderError as exc:
            raise BrokerError(f"Can't reach {self.info.name}") from exc
        if resp.status in (401, 403):
            raise BrokerError(f"{self.info.name} rejected the access token")
        if resp.status >= 400:
            raise BrokerError(f"{self.info.name} returned an error ({resp.status})")
        try:
            return resp.json()
        except Exception as exc:
            raise BrokerError(f"{self.info.name} sent an unreadable response") from exc

    def quote(self, payload: Mapping) -> QuoteResult:
        check_payload(payload)
        data = self._call("POST", "/v1/quotes", payload)
        offers = data.get("offers") if isinstance(data, dict) else None
        if not isinstance(offers, list):
            raise BrokerError(f"{self.info.name} sent no offers")
        return build_quote_result(offers, self.info.id)

    def _receipt(self, data: Any, offer_id: str) -> EnrollmentReceipt:
        try:
            status = str(data["status"])
            if status not in ("pending", "active"):
                raise ValueError(status)
            return EnrollmentReceipt(self.info.id, str(data.get("offer_id", offer_id)), str(data["enrollment_ref"]),
                                     status, date.fromisoformat(str(data["coverage_start"])),
                                     str(data.get("message", ""))[:200])
        except (KeyError, ValueError, TypeError) as exc:
            raise BrokerError(f"{self.info.name} sent an unexpected enrollment response") from exc

    def enroll(self, payload: Mapping) -> EnrollmentReceipt:
        check_payload(payload)
        return self._receipt(self._call("POST", "/v1/enrollments", payload), str(payload.get("offer_id", "")))

    def status(self, enrollment_ref: str) -> EnrollmentReceipt:
        ref = urllib.parse.quote(enrollment_ref, safe="")
        return self._receipt(self._call("GET", f"/v1/enrollments/{ref}"), "")


def adapter_for(broker_id: str, *, tokens: Mapping[str, str] | None = None, env: Mapping[str, str] | None = None,
                transport: Any = None, sandbox: SandboxBroker | None = None) -> BrokerAdapter:
    info = approved_brokers(env).get(broker_id)
    if info is None:
        raise BrokerError("That brokerage isn't on the approved list")
    if info.kind == "sandbox":
        return sandbox or SandboxBroker()
    return RestBroker(info, (tokens or {}).get(broker_id, ""), transport=transport)
