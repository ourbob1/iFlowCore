"""
License state machine and entitlement checks.

    no token ─────────────────────────────▶ FREE      (Solo Core)
    valid token, before expiry ───────────▶ ACTIVE    (licensed tier)
    after expiry, within grace days ──────▶ GRACE     (licensed tier, renewal reminder)
    after grace ──────────────────────────▶ EXPIRED   (Solo Core; data untouched)
    forged / damaged / wrong-issuer token ▶ INVALID   (Solo Core, with the reason)

Clock rollback guard: the last time the app saw is remembered (in the
encrypted database, at most once an hour), and expiry is judged against
max(now, last seen). Moving the device clock backwards never extends a
license; a clock that was wrong in the future only moves an expiry earlier,
never later.

Everything here is local: the token lives in `device_settings` (sealed with
the device key on mobile platform storage), and checking it performs no I/O
beyond that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Mapping
import uuid

from core.licensing.pricing import Plan, Quote, add_months, quote
from core.licensing.tiers import FEATURES, Feature, Tier
from core.licensing.tokens import LicenseClaims, LicenseError, sign, simulation_private_key, verify
from core.timeutil import parse_iso, to_iso_utc


TOUCH_EVERY = timedelta(hours=1)        # how often "last seen" is written (the guard works in days)


class Status(str, Enum):
    FREE = "free"
    ACTIVE = "active"
    GRACE = "grace"
    EXPIRED = "expired"
    INVALID = "invalid"


class FeatureLocked(PermissionError):
    def __init__(self, feature: Feature, current: Tier) -> None:
        self.feature = feature
        self.required = FEATURES[feature].tier
        self.current = current
        super().__init__(f"{FEATURES[feature].label} needs {self.required.label} (you have {current.label}).")


@dataclass(frozen=True)
class LicenseState:
    status: Status
    tier: Tier                          # effective tier right now
    claims: LicenseClaims | None = None
    message: str = ""
    as_of: datetime | None = None

    def allows(self, feature: Feature) -> bool:
        return self.tier.includes(FEATURES[feature].tier)

    def require(self, feature: Feature) -> None:
        if not self.allows(feature):
            raise FeatureLocked(feature, self.tier)

    @property
    def paid(self) -> bool:
        return self.status in (Status.ACTIVE, Status.GRACE)

    @property
    def grace_ends_at(self) -> datetime | None:
        return self.claims.expires_at + timedelta(days=self.claims.grace_days) if self.claims else None

    @property
    def days_left(self) -> int | None:
        """Whole days until expiry (ACTIVE) or until grace ends (GRACE)."""
        if self.as_of is None or self.claims is None:
            return None
        end = self.claims.expires_at if self.status is Status.ACTIVE else self.grace_ends_at
        if self.status not in (Status.ACTIVE, Status.GRACE) or end is None:
            return None
        return max(0, (end - self.as_of).days)


FREE_STATE = LicenseState(Status.FREE, Tier.SOLO, message="Solo Core: free forever.")


def evaluate(token: str | None, now: datetime, *, last_seen: datetime | None = None, allow_simulation: bool = False,
             env: Mapping[str, str] | None = None) -> LicenseState:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    effective = max(now, last_seen) if last_seen else now
    if not token:
        return LicenseState(Status.FREE, Tier.SOLO, message=FREE_STATE.message, as_of=effective)
    try:
        claims = verify(token, allow_simulation=allow_simulation, env=env)
    except LicenseError as exc:
        return LicenseState(Status.INVALID, Tier.SOLO, message=str(exc), as_of=effective)
    if effective < claims.expires_at:
        return LicenseState(Status.ACTIVE, claims.tier, claims, f"{claims.tier.label} is active.", effective)
    if effective < claims.expires_at + timedelta(days=claims.grace_days):
        return LicenseState(Status.GRACE, claims.tier, claims,
                            f"{claims.tier.label} expired; features stay on during the grace period.", effective)
    return LicenseState(Status.EXPIRED, Tier.SOLO, claims,
                        f"{claims.tier.label} expired. You're on Solo Core; all your data is still here.", effective)


class LicenseManager:
    """Reads and writes the license through a SettingsStore-like object
    (`load()` → settings with license_token / license_last_seen; `update(**fields)`)."""

    def __init__(self, store: Any, *, clock: Callable[[], datetime] | None = None, allow_simulation: bool = False,
                 env: Mapping[str, str] | None = None) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.allow_simulation = allow_simulation
        self.env = env

    def _last_seen(self, settings: Any) -> datetime | None:
        raw = getattr(settings, "license_last_seen", None)
        try:
            return parse_iso(raw) if raw else None
        except ValueError:
            return None

    def state(self, *, touch: bool = True) -> LicenseState:
        settings = self.store.load()
        now = self.clock()
        last = self._last_seen(settings)
        st = evaluate(settings.license_token, now, last_seen=last, allow_simulation=self.allow_simulation,
                      env=self.env)
        if touch and settings.license_token and (last is None or now - last >= TOUCH_EVERY):
            self.store.update(license_last_seen=to_iso_utc(now))
        return st

    def activate(self, token: str) -> LicenseState:
        token = "".join((token or "").split())
        st = evaluate(token, self.clock(), last_seen=self._last_seen(self.store.load()),
                      allow_simulation=self.allow_simulation, env=self.env)
        if st.status is Status.INVALID:
            raise LicenseError(st.message)
        if st.status is Status.EXPIRED:
            raise LicenseError("That license has already expired")
        self.store.update(license_token=token)
        return self.state()

    def remove(self) -> LicenseState:
        """Back to Solo Core on this device. Nothing else changes."""
        self.store.update(license_token=None)
        return self.state(touch=False)

    # -- simulated upgrades (development builds only) --------------------------------------

    def quote_for(self, target: Plan, today: date) -> Quote:
        st = self.state(touch=False)
        c = st.claims
        if st.status is Status.ACTIVE and c is not None and c.amount_minor and c.currency == target.currency:
            # credit what was actually paid for this term, even if the price book has changed since
            current = Plan(f"{c.tier.value}_{c.period.value}", c.tier, c.period, c.currency, c.amount_minor)
            start = c.issued_at.date()
            end = c.expires_at.date()
            if start < end and start <= today:
                return quote(target, today, current=current, cycle_start=start, cycle_end=end)
        return quote(target, today)

    def simulate_purchase(self, target: Plan, today: date) -> tuple[LicenseState, Quote]:
        """Issue and activate a *simulation* license for `target`. No payment happens; production
        builds refuse. The term starts now and ends after the plan's billing period."""
        if not self.allow_simulation:
            raise LicenseError("Simulated purchases are disabled in production builds")
        q = self.quote_for(target, today)
        now = self.clock()
        expires = datetime.combine(add_months(now.date(), target.period.months), now.timetz())
        claims = LicenseClaims(license_id=str(uuid.uuid4()), issuer="simulation", tier=target.tier,
                               period=target.period, issued_at=now, expires_at=expires,
                               currency=target.currency, amount_minor=target.amount_minor)
        return self.activate(sign(claims, simulation_private_key())), q
