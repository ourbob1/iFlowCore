"""Phase 7 licensing: tiers & feature flags, integer pricing, offline signed tokens, license state machine."""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.licensing import (
    FEATURES,
    Feature,
    FeatureLocked,
    LicenseClaims,
    LicenseError,
    LicenseManager,
    Period,
    Plan,
    PricingError,
    Status,
    Tier,
    evaluate,
    plan,
    plans,
)
from core.licensing.pricing import (
    add_months,
    annual_savings_bp,
    annual_savings_minor,
    format_price,
    plan_by_id,
    quote,
    unused_credit_minor,
)
from core.licensing.state import TOUCH_EVERY
from core.licensing.tiers import features_for, new_in
from core.licensing.tokens import ENV_PUBKEYS, public_key_hex, sign, simulation_private_key, verify

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
VENDOR = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
VENDOR_ENV = {ENV_PUBKEYS: public_key_hex(VENDOR.public_key())}


def claims(tier=Tier.PRO, *, issuer="vendor", days=30, issued=T0, grace=7, amount=699, period=Period.MONTHLY):
    import uuid

    return LicenseClaims(str(uuid.uuid4()), issuer, tier, period, issued, issued + timedelta(days=days), grace, "USD",
                         amount)


def token(c=None, key=VENDOR) -> str:
    return sign(c or claims(), key)


# =============================================================================================
# tiers & feature flags
# =============================================================================================


def test_tier_order_and_inclusion():
    assert Tier.GUILD.includes(Tier.PRO) and Tier.PRO.includes(Tier.SOLO) and Tier.GUILD.includes(Tier.GUILD)
    assert not Tier.SOLO.includes(Tier.PRO) and not Tier.PRO.includes(Tier.GUILD)
    assert [t.rank for t in (Tier.SOLO, Tier.PRO, Tier.GUILD)] == [0, 1, 2]


@pytest.mark.parametrize("feature,tier", [
    (Feature.MANUAL_TRACKING, Tier.SOLO), (Feature.CSV_IMPORT, Tier.SOLO), (Feature.MILEAGE_LOG, Tier.SOLO),
    (Feature.LOCAL_STORAGE, Tier.SOLO), (Feature.BASIC_EXPORT, Tier.SOLO), (Feature.INSIGHTS, Tier.SOLO),
    (Feature.MOTION_SENTRY, Tier.PRO), (Feature.VIN_DECODE, Tier.PRO), (Feature.FLEET, Tier.PRO),
    (Feature.ADVANCED_EXPORTS, Tier.PRO), (Feature.RECEIPT_OCR, Tier.PRO),
    (Feature.AHP_POOL, Tier.GUILD), (Feature.PREMIUM_WITHHOLDING, Tier.GUILD),
])
def test_feature_tiers_match_the_spec(feature, tier):
    assert FEATURES[feature].tier is tier


def test_higher_tiers_include_everything_below():
    solo, pro, guild = (set(features_for(t)) for t in (Tier.SOLO, Tier.PRO, Tier.GUILD))
    assert solo < pro < guild and guild == set(Feature)
    assert set(new_in(Tier.PRO)) == pro - solo and set(new_in(Tier.GUILD)) == guild - pro


# =============================================================================================
# pricing (integer minor units)
# =============================================================================================


def test_price_book_is_integer_cents():
    assert (plan(Tier.PRO, Period.MONTHLY).amount_minor, plan(Tier.PRO, Period.ANNUAL).amount_minor,
            plan(Tier.GUILD, Period.MONTHLY).amount_minor) == (699, 6000, 1499)
    for p in plans():
        assert isinstance(p.amount_minor, int) and not isinstance(p.amount_minor, bool)
    assert plan(Tier.PRO, Period.ANNUAL).monthly_equivalent_minor == 500


def test_annual_savings():
    assert annual_savings_minor(Tier.PRO) == 12 * 699 - 6000 == 2388
    assert annual_savings_bp(Tier.PRO) == 2847                      # 2388 / 8388 = 28.47%
    assert annual_savings_minor(Tier.GUILD) is None                 # no annual Guild plan


def test_unavailable_plans_and_currencies():
    with pytest.raises(PricingError):
        plan(Tier.GUILD, Period.ANNUAL)
    with pytest.raises(PricingError):
        plan(Tier.PRO, Period.MONTHLY, "EUR")                       # never FX-converted
    assert {p.currency for p in plans("EUR")} == {"USD"}            # falls back to the USD price list
    assert plan_by_id("pro_annual").amount_minor == 6000
    with pytest.raises(PricingError):
        plan_by_id("platinum_monthly")


@pytest.mark.parametrize("amount", [6.99, -1, True, "699"])
def test_plan_rejects_non_integer_prices(amount):
    with pytest.raises(PricingError):
        Plan("x", Tier.PRO, Period.MONTHLY, "USD", amount)


def test_unused_credit_rounds_down():
    pro = plan(Tier.PRO, Period.MONTHLY)
    start, end = date(2026, 9, 1), date(2026, 10, 1)              # 30 days
    assert unused_credit_minor(pro, start, end, date(2026, 9, 16)) == 349          # 699 × 15/30 = 349.5 → 349
    assert unused_credit_minor(pro, start, end, start) == 699
    assert unused_credit_minor(pro, start, end, end) == 0
    assert unused_credit_minor(pro, start, end, date(2026, 12, 1)) == 0
    with pytest.raises(PricingError):
        unused_credit_minor(pro, end, start, start)
    with pytest.raises(PricingError):
        unused_credit_minor(pro, start, end, date(2026, 8, 1))


def test_quote_credit_is_capped_and_itemized():
    annual = plan(Tier.PRO, Period.ANNUAL)
    guild = plan(Tier.GUILD, Period.MONTHLY)
    q = quote(guild, date(2026, 9, 27), current=annual, cycle_start=date(2026, 9, 1), cycle_end=date(2027, 9, 1))
    assert q.credit_minor == guild.amount_minor and q.due_today_minor == 0      # never more credit than the price
    q2 = quote(guild, date(2026, 9, 16), current=plan(Tier.PRO, Period.MONTHLY), cycle_start=date(2026, 9, 1),
               cycle_end=date(2026, 10, 1))
    assert (q2.credit_minor, q2.due_today_minor, q2.renews_on, q2.renewal_minor) == (349, 1150, date(2026, 10, 16),
                                                                                    1499)
    assert quote(annual, date(2026, 9, 27)).renews_on == date(2027, 9, 27)


def test_add_months_clamps_to_month_end():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
    assert add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
    assert add_months(date(2028, 2, 29), 12) == date(2029, 2, 28)


def test_format_price():
    assert (format_price(699, "USD"), format_price(6000, "USD"), format_price(700, "JPY"),
            format_price(1500, "KWD")) == ("6.99", "60", "700", "1.500")


# =============================================================================================
# tokens
# =============================================================================================


def test_sign_verify_roundtrip():
    c = claims(Tier.GUILD, amount=1499)
    assert verify(token(c), allow_simulation=False, env=VENDOR_ENV) == c


def test_tampered_payload_fails():
    tok = token()
    prefix, payload, sig = tok.split(".")
    data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    data["tier"] = "guild"
    forged = base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    with pytest.raises(LicenseError, match="signature"):
        verify(f"{prefix}.{forged}.{sig}", allow_simulation=False, env=VENDOR_ENV)


def test_wrong_key_and_unknown_issuer_fail(monkeypatch):
    other = Ed25519PrivateKey.generate()
    with pytest.raises(LicenseError, match="signature"):
        verify(token(key=other), allow_simulation=False, env=VENDOR_ENV)
    with pytest.raises(LicenseError, match="signature"):
        verify(token(), allow_simulation=False, env={})                 # only the pinned vendor key is trusted
    monkeypatch.setattr("core.licensing.tokens.VENDOR_PUBLIC_KEYS", ())
    with pytest.raises(LicenseError, match="can't verify"):
        verify(token(), allow_simulation=False, env={})                 # no vendor key configured
    with pytest.raises(LicenseError):
        verify(token(claims(issuer="someone")), allow_simulation=True, env=VENDOR_ENV)


def test_simulation_tokens_only_in_development():
    tok = token(claims(issuer="simulation"), key=simulation_private_key())
    assert verify(tok, allow_simulation=True).issuer == "simulation"
    with pytest.raises(LicenseError, match="development builds"):
        verify(tok, allow_simulation=False)
    # a simulation-signed token claiming to be a vendor license is not trusted either
    with pytest.raises(LicenseError):
        verify(token(claims(issuer="vendor"), key=simulation_private_key()), allow_simulation=True, env=VENDOR_ENV)


@pytest.mark.parametrize("bad", ["", "   ", "hello", "RSL1.abc", "RSL2.a.b", "RSL1.!!!.???", "RSL1." + "a" * 3000 + ".b"])
def test_malformed_tokens(bad):
    with pytest.raises(LicenseError):
        verify(bad, allow_simulation=True, env=VENDOR_ENV)


def test_pasted_whitespace_is_tolerated():
    tok = token()
    wrapped = "\n".join(tok[i:i + 40] for i in range(0, len(tok), 40)) + "  "
    assert verify(wrapped, allow_simulation=False, env=VENDOR_ENV).tier is Tier.PRO


@pytest.mark.parametrize("change,msg", [
    ({"tier": "solo"}, "Solo Core"), ({"v": 2}, "version"), ({"grace": 90}, "grace"), ({"amt": -5}, "amount"),
    ({"lid": "not-a-uuid"}, "incomplete"),
])
def test_invalid_claims(change, msg):
    p = {**claims().to_payload(), **change}
    payload = base64.urlsafe_b64encode(json.dumps(p).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(VENDOR.sign(f"RSL1.{payload}".encode())).rstrip(b"=").decode()
    with pytest.raises(LicenseError, match=msg):
        verify(f"RSL1.{payload}.{sig}", allow_simulation=False, env=VENDOR_ENV)


def test_expiry_before_issue_rejected():
    c = claims()
    bad = LicenseClaims(c.license_id, c.issuer, c.tier, c.period, c.issued_at, c.issued_at, 7)
    with pytest.raises(LicenseError, match="expires before"):
        verify(sign(bad, VENDOR), allow_simulation=False, env=VENDOR_ENV)


# =============================================================================================
# state machine
# =============================================================================================


def test_state_transitions_at_exact_boundaries():
    c = claims(days=30, grace=7)
    tok = token(c)
    at = lambda dt: evaluate(tok, dt, env=VENDOR_ENV)  # noqa: E731
    assert evaluate(None, T0).status is Status.FREE and evaluate(None, T0).tier is Tier.SOLO
    assert (at(T0).status, at(T0).tier, at(T0).days_left) == (Status.ACTIVE, Tier.PRO, 30)
    just_before = c.expires_at - timedelta(seconds=1)
    assert at(just_before).status is Status.ACTIVE
    assert (at(c.expires_at).status, at(c.expires_at).tier, at(c.expires_at).days_left) == (Status.GRACE, Tier.PRO, 7)
    grace_end = c.expires_at + timedelta(days=7)
    assert at(grace_end - timedelta(seconds=1)).status is Status.GRACE
    expired = at(grace_end)
    assert (expired.status, expired.tier, expired.days_left) == (Status.EXPIRED, Tier.SOLO, None)
    assert "still here" in expired.message


def test_invalid_token_falls_back_to_solo():
    st = evaluate("RSL1.garbage.sig", T0)
    assert st.status is Status.INVALID and st.tier is Tier.SOLO and st.message


def test_clock_rollback_does_not_extend_a_license():
    c = claims(days=30, grace=0)
    tok = token(c)
    after = c.expires_at + timedelta(days=1)
    assert evaluate(tok, T0, last_seen=after, env=VENDOR_ENV).status is Status.EXPIRED
    assert evaluate(tok, T0, last_seen=T0 - timedelta(days=5), env=VENDOR_ENV).status is Status.ACTIVE


def test_naive_now_rejected():
    with pytest.raises(ValueError):
        evaluate(None, datetime(2026, 9, 27))


def test_allows_and_require():
    pro = evaluate(token(), T0, env=VENDOR_ENV)
    assert pro.allows(Feature.MOTION_SENTRY) and pro.allows(Feature.CSV_IMPORT) and not pro.allows(Feature.AHP_POOL)
    with pytest.raises(FeatureLocked) as exc:
        pro.require(Feature.PREMIUM_WITHHOLDING)
    assert exc.value.required is Tier.GUILD and exc.value.current is Tier.PRO and "Health & Guild" in str(exc.value)
    solo = evaluate(None, T0)
    for f in features_for(Tier.SOLO):
        solo.require(f)                                                  # never raises
    assert not solo.allows(Feature.RECEIPT_OCR)


class Store:
    def __init__(self):
        self.s = SimpleNamespace(license_token=None, license_last_seen=None)
        self.writes = 0

    def load(self):
        return self.s

    def update(self, **k):
        self.writes += 1
        self.s.__dict__.update(k)
        return self.s


def manager(now, *, simulation=True, env=None):
    return LicenseManager(Store(), clock=lambda: now[0], allow_simulation=simulation, env=env)


def test_activate_remove_and_last_seen_throttle():
    now = [T0]
    m = manager(now, simulation=False, env=VENDOR_ENV)
    assert m.state().status is Status.FREE and m.store.writes == 0       # no writes without a license
    st = m.activate("  " + token() + "\n")
    assert st.status is Status.ACTIVE and m.store.s.license_last_seen is not None
    writes = m.store.writes
    now[0] = T0 + TOUCH_EVERY / 2
    m.state()
    assert m.store.writes == writes                                       # throttled
    now[0] = T0 + TOUCH_EVERY * 2
    m.state()
    assert m.store.writes == writes + 1
    assert m.remove().status is Status.FREE and m.store.s.license_token is None


def test_activate_rejects_bad_and_expired_tokens():
    now = [T0 + timedelta(days=60)]
    m = manager(now, simulation=False, env=VENDOR_ENV)
    with pytest.raises(LicenseError, match="expired"):
        m.activate(token(claims(days=30)))
    with pytest.raises(LicenseError):
        m.activate("RSL1.x.y")
    assert m.store.s.license_token is None                               # nothing stored on failure


def test_simulated_purchase_flow_with_proration():
    now = [T0]
    m = manager(now)
    st, q = m.simulate_purchase(plan(Tier.PRO, Period.MONTHLY), T0.date())
    assert (st.status, st.tier, q.due_today_minor, q.credit_minor) == (Status.ACTIVE, Tier.PRO, 699, 0)
    assert st.claims.issuer == "simulation" and st.claims.amount_minor == 699
    now[0] = T0 + timedelta(days=15)                                     # halfway through a 30-day month
    st, q = m.simulate_purchase(plan(Tier.GUILD, Period.MONTHLY), now[0].date())
    assert st.tier is Tier.GUILD and (q.credit_minor, q.due_today_minor) == (349, 1150)
    now[0] = T0 + timedelta(days=20)                                     # downgrade Guild → Pro: credit capped at price
    st, q = m.simulate_purchase(plan(Tier.PRO, Period.MONTHLY), now[0].date())
    assert st.tier is Tier.PRO and q.credit_minor == 699 and q.due_today_minor == 0


def test_simulation_is_refused_in_production():
    m = manager([T0], simulation=False)
    with pytest.raises(LicenseError, match="production"):
        m.simulate_purchase(plan(Tier.PRO, Period.MONTHLY), T0.date())


def test_quote_without_active_license_has_no_credit():
    m = manager([T0])
    q = m.quote_for(plan(Tier.PRO, Period.ANNUAL), T0.date())
    assert (q.credit_minor, q.due_today_minor) == (0, 6000)


# =============================================================================================
# storage: the token is a sealed secret on mobile platform storage
# =============================================================================================


def test_token_is_sealed_on_platform_storage(db):
    from core.crypto import FieldSealer
    from core.device_settings import SettingsStore

    store = SettingsStore(db, sealer=FieldSealer("cd" * 32), require_sealing=True)
    m = LicenseManager(store, clock=lambda: T0, allow_simulation=True)
    st, _ = m.simulate_purchase(plan(Tier.PRO, Period.MONTHLY), T0.date())
    with db.connection() as conn:
        raw = conn.execute("SELECT value_json FROM device_settings WHERE key = 'license_token'").fetchone()[0]
    assert "RSL1" not in raw and "sealed" in raw
    assert m.state().status is Status.ACTIVE
    assert LicenseManager(SettingsStore(db), clock=lambda: T0, allow_simulation=True).state().status is Status.FREE


def test_licensing_package_imports_no_network_code():
    import ast
    from pathlib import Path

    forbidden = ("urllib", "http", "socket", "requests", "httpx", "ssl", "core.ai", "core.fleet.http", "core.telemetry")
    pkg = Path(__file__).resolve().parents[1] / "core" / "licensing"
    offenders = []
    for path in pkg.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
            offenders += [f"{path.name}:{n}" for n in names if any(n == f or n.startswith(f + ".") for f in forbidden)]
    assert offenders == []
