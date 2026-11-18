"""Phase 8 iFlow Health engine: withholding from the earnings stream, eligibility, compliance, brokerages."""

from __future__ import annotations

import ast
import json
import logging
from datetime import date, timedelta
from pathlib import Path

import pytest

from core.fleet.http import HttpResponse
from core.guild import active_plans, record_entry, save_plan
from core.health.brokers import (
    Applicant,
    BrokerError,
    RestBroker,
    SandboxBroker,
    approved_brokers,
    describe_payload,
    enrollment_payload,
    parse_offer,
    quote_payload,
    redact,
)
from core.health.compliance import ComplianceError, check_offer, check_payload, forbidden_keys
from core.health.eligibility import WorkEvidence, evaluate
from core.health.enrollment import browse, enroll, first_of_next_month, refresh_statuses
from core.health.models import (
    CoverageTier,
    EligibilityRules,
    HealthError,
    Line,
    MemberProfile,
    PlanOffer,
    age_band,
)
from core.health.withholding import (
    Decision,
    Payout,
    WithholdingError,
    WithholdingPolicy,
    decide,
    platform_split,
    reserve_status,
    split_across_plans,
    sync_withholdings,
    weekly_need_minor,
)

from .test_intelligence import _platform, _txn

TODAY = date(2026, 9, 27)
NY = "America/New_York"
PROFILE = MemberProfile(state="FL", birth_year=1990, association_member_since=date(2026, 6, 1),
                        association_member_id="GLD-448812")


def payout(net, platform="uber", tid="t1"):
    return Payout(tid, TODAY, platform, net)


# =============================================================================================
# decide(): one payout
# =============================================================================================


def test_percent_mode_is_cents_per_dollar_rounded_up():
    p = WithholdingPolicy(mode="percent", rate_bp=700)                    # 7¢ per $1
    assert decide(payout(10_000), p, need_minor=0, expected_minor=0) == Decision(700, 700, "withheld")
    assert decide(payout(1_234), p, need_minor=0, expected_minor=0).amount_minor == 87   # 86.38 → 87
    assert decide(payout(1), p, need_minor=0, expected_minor=0).amount_minor == 1


def test_per_payout_mode_and_cap():
    p = WithholdingPolicy(mode="per_payout", per_payout_minor=200)
    assert decide(payout(10_000), p, need_minor=0, expected_minor=0) == Decision(200, 200, "withheld")
    capped = decide(payout(400), p, need_minor=0, expected_minor=0)       # $2 of $4 is 50% > 25% cap
    assert (capped.amount_minor, capped.rate_bp, capped.reason) == (100, 2500, "capped")


def test_auto_mode_targets_the_need():
    p = WithholdingPolicy()
    d = decide(payout(10_000), p, need_minor=45_000, expected_minor=300_000)
    assert (d.rate_bp, d.amount_minor) == (1500, 1500)
    assert decide(payout(10_000), p, need_minor=300, expected_minor=300_000).amount_minor == 10   # 10 bp of $100
    last = decide(payout(10_000), p, need_minor=700, expected_minor=10_000)
    assert last.amount_minor == 700                                          # never more than the need
    assert decide(payout(10_000), p, need_minor=0, expected_minor=0).reason == "covered"
    thin = decide(payout(10_000), p, need_minor=45_000, expected_minor=20_000)
    assert (thin.rate_bp, thin.amount_minor, thin.reason) == (2500, 2500, "withheld")   # cap holds


@pytest.mark.parametrize("net,reason", [(0, "not_a_payout"), (-500, "not_a_payout")])
def test_refunds_are_never_clawed_back(net, reason):
    assert decide(payout(net), WithholdingPolicy(mode="percent", rate_bp=700), need_minor=1, expected_minor=1) \
        == Decision(0, 0, reason)


def test_excluded_platform_and_off():
    p = WithholdingPolicy(mode="percent", rate_bp=700, excluded_platforms=("dash",))
    assert decide(payout(10_000, "dash"), p, need_minor=0, expected_minor=0).reason == "excluded"
    assert decide(payout(10_000), WithholdingPolicy(mode="off"), need_minor=9, expected_minor=9).reason == "off"


@pytest.mark.parametrize("kwargs", [dict(mode="weekly"), dict(mode="percent"), dict(mode="per_payout"),
                                    dict(cap_bp=0), dict(cap_bp=10_001), dict(rate_bp=3000),
                                    dict(mode="percent", rate_bp=7.5), dict(per_payout_minor=-1)])
def test_policy_validation(kwargs):
    with pytest.raises(WithholdingError):
        WithholdingPolicy(**kwargs)


def test_policy_roundtrip():
    p = WithholdingPolicy(mode="percent", rate_bp=650, cap_bp=2000, excluded_platforms=("a",), start=TODAY)
    assert WithholdingPolicy.from_dict(p.to_dict()) == p and WithholdingPolicy.from_dict(None) == WithholdingPolicy()


def test_split_across_plans_is_lossless():
    health = _plan_stub("h", 45_000)
    dental = _plan_stub("d", 3_900)
    assert split_across_plans(1_000, [health, dental]) == [("h", 920), ("d", 80)]
    for amount in (1, 2, 7, 999, 12_345):
        assert sum(p for _, p in split_across_plans(amount, [health, dental])) == amount
    assert split_across_plans(1, [health, dental]) == [("h", 1)]            # zero shares dropped
    assert split_across_plans(0, [health]) == []


def _plan_stub(pid, premium):
    from core.guild.store import HealthPlan

    return HealthPlan(pid, "A", "P", premium, "USD", 1, 1, date(2026, 9, 1), None, None)


def test_weekly_need_rounds_up():
    assert weekly_need_minor([_plan_stub("h", 45_000)]) == 10_385             # 450 × 12 / 52 = 103.846…
    assert weekly_need_minor([_plan_stub("h", 45_000), _plan_stub("d", 3_900)]) == 11_285


# =============================================================================================
# sync from the earnings stream (multi-platform)
# =============================================================================================


def _setup(db, *, premium=45_000, due_day=1, start=date(2026, 9, 1), dental=False):
    ids = [save_plan(db, association="Guild", plan_name="Silver", monthly_premium_minor=premium, due_day=due_day,
                     coverage_start=start)]
    if dental:
        ids.append(save_plan(db, association="Guild", plan_name="Dental", monthly_premium_minor=3_900,
                             due_day=due_day, coverage_start=start, line="dental"))
    return ids


def _at(d: date, hour=22) -> str:
    return f"{d.isoformat()}T{hour:02d}:00:00.000Z"


def _entries(db):
    with db.connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM health_premium_entries WHERE deleted_at IS NULL AND source = 'auto'")]


def test_percent_withholding_across_platforms(db):
    uber, dash = _platform(db, "Uber"), _platform(db, "DoorDash")
    _setup(db, dental=True)
    _txn(db, uber, _at(date(2026, 9, 20)), gross=2_500, fee=500)          # net 2000
    _txn(db, dash, _at(date(2026, 9, 21)), gross=1_234, fee=0)            # net 1234
    _txn(db, uber, _at(date(2026, 9, 22)), gross=900, fee=0, kind="reimbursement")      # not a payout
    _txn(db, uber, _at(date(2026, 9, 23)), gross=-800, fee=0, kind="adjustment")        # never clawed back
    policy = WithholdingPolicy(mode="percent", rate_bp=700, start=date(2026, 9, 1))
    rep = sync_withholdings(db, policy=policy, today=TODAY, tz=NY, currency="USD")
    assert rep.withheld_minor == 140 + 87 and rep.by_platform == {uber: 140, dash: 87}
    assert rep.reasons == {"withheld": 2, "not_a_payout": 1}
    rows = _entries(db)
    assert len(rows) == 4 and all(isinstance(r["amount_minor"], int) for r in rows)   # 2 payouts × 2 plans
    assert sum(r["amount_minor"] for r in rows) == 227 and {r["rate_bp"] for r in rows} == {700}
    shares = {s.name: s for s in platform_split(db, start=date(2026, 9, 1), end=TODAY, tz=NY)}
    assert shares["Uber"].withheld_minor == 140 and shares["Uber"].net_minor == 2_000
    assert shares["Uber"].effective_bp == 700 and shares["DoorDash"].withheld_minor == 87


def test_sync_is_idempotent_and_picks_up_new_payouts(db):
    uber = _platform(db)
    _setup(db)
    policy = WithholdingPolicy(mode="percent", rate_bp=500, start=date(2026, 9, 1))
    _txn(db, uber, _at(date(2026, 9, 20)), gross=2_000, fee=0)
    assert sync_withholdings(db, policy=policy, today=TODAY, tz=NY, currency="USD").processed == 1
    again = sync_withholdings(db, policy=policy, today=TODAY, tz=NY, currency="USD")
    assert (again.processed, again.withheld_minor) == (0, 0) and len(_entries(db)) == 1
    _txn(db, uber, _at(date(2026, 9, 21)), gross=4_000, fee=0)
    assert sync_withholdings(db, policy=policy, today=TODAY, tz=NY, currency="USD").withheld_minor == 200


def test_deleted_payout_reverses_its_withholding(db):
    uber = _platform(db)
    _setup(db)
    _txn(db, uber, _at(date(2026, 9, 20)), gross=2_000, fee=0)
    policy = WithholdingPolicy(mode="percent", rate_bp=500, start=date(2026, 9, 1))
    sync_withholdings(db, policy=policy, today=TODAY, tz=NY, currency="USD")
    with db.transaction() as conn:
        conn.execute("UPDATE transactions SET deleted_at = '2026-09-26T00:00:00Z'")
    rep = sync_withholdings(db, policy=policy, today=TODAY, tz=NY, currency="USD")
    assert rep.reversed == 1 and _entries(db) == []
    assert reserve_status(db, today=TODAY, currency="USD").balance_minor == 0


def test_auto_mode_spreads_the_need_and_lands_exactly_on_the_due_date(db):
    uber = _platform(db)
    _setup(db, premium=45_000, due_day=1)
    for i in range(24):                                                     # Sep 8 → Oct 1, $150 net a day
        _txn(db, uber, _at(date(2026, 9, 8) + timedelta(days=i)), gross=15_000, fee=0)
    policy = WithholdingPolicy(start=date(2026, 9, 8))
    # a week before the due date the reserve is on track but not yet full: the need is spread, not front-loaded
    early = sync_withholdings(db, policy=policy, today=date(2026, 9, 24), tz=NY, currency="USD")
    st = reserve_status(db, today=date(2026, 9, 24), currency="USD")
    assert 0 < st.shortfall_minor < 15_000 and early.withheld_minor == st.balance_minor
    rates = [r["rate_bp"] for r in sorted(_entries(db), key=lambda r: r["occurred_on"])]
    assert rates[0] == 2500                          # first-ever payout: no history, so the cap applies
    steady = rates[2:]
    assert max(steady) <= 1300 and max(steady) - min(steady) <= 300         # then a steady small slice
    # by the due date's payout it is funded to the cent, with no overshoot
    sync_withholdings(db, policy=policy, today=date(2026, 10, 1), tz=NY, currency="USD")
    assert sum(r["amount_minor"] for r in _entries(db)) == 45_000


def test_platform_split_counts_payouts_that_needed_no_withholding(db):
    uber, dash = _platform(db, "Uber"), _platform(db, "DoorDash")
    (pid,) = _setup(db, premium=45_000)
    record_entry(db, pid, kind="reserve", amount_minor=44_000, on=date(2026, 9, 2))      # nearly covered already
    _txn(db, uber, _at(date(2026, 9, 20)), gross=10_000, fee=0)                           # withholds the last $10
    _txn(db, dash, _at(date(2026, 9, 21)), gross=8_000, fee=0)                            # covered: nothing withheld
    sync_withholdings(db, policy=WithholdingPolicy(start=date(2026, 9, 1)), today=TODAY, tz=NY, currency="USD")
    shares = {s.name: s for s in platform_split(db, start=date(2026, 9, 1), end=TODAY, tz=NY)}
    assert (shares["Uber"].net_minor, shares["Uber"].withheld_minor) == (10_000, 1_000)
    assert (shares["DoorDash"].net_minor, shares["DoorDash"].withheld_minor, shares["DoorDash"].effective_bp) == \
        (8_000, 0, 0)


def test_auto_mode_stops_once_covered(db):
    uber = _platform(db)
    (pid,) = _setup(db, premium=45_000)
    record_entry(db, pid, kind="reserve", amount_minor=45_000, on=date(2026, 9, 5))
    _txn(db, uber, _at(date(2026, 9, 20)), gross=100_000, fee=0)
    rep = sync_withholdings(db, policy=WithholdingPolicy(start=date(2026, 9, 1)), today=TODAY, tz=NY, currency="USD")
    assert rep.withheld_minor == 0 and rep.reasons == {"covered": 1}


def test_auto_mode_counts_existing_reserve_and_payments(db):
    uber = _platform(db)
    (pid,) = _setup(db, premium=45_000)
    record_entry(db, pid, kind="reserve", amount_minor=40_000, on=date(2026, 9, 5))          # carried surplus
    _txn(db, uber, _at(date(2026, 9, 20)), gross=100_000, fee=0)
    rep = sync_withholdings(db, policy=WithholdingPolicy(start=date(2026, 9, 1)), today=TODAY, tz=NY, currency="USD")
    assert rep.withheld_minor == 5_000                                      # only the missing $50
    (pid2,) = [p.id for p in active_plans(db)]
    record_entry(db, pid2, kind="payment", amount_minor=45_000, on=TODAY, covers_month="2026-10")
    st = reserve_status(db, today=TODAY, currency="USD")
    assert st.shortfall_minor == 0 and st.balance_minor == 0


def test_foreign_currency_payouts_and_plans(db):
    uber = _platform(db)
    _setup(db)
    save_plan(db, association="EU", plan_name="Euro dental", monthly_premium_minor=2_000, currency="EUR",
              coverage_start=date(2026, 9, 1), line="dental")
    _txn(db, uber, _at(date(2026, 9, 20)), gross=1_000, fee=0, currency="EUR")               # no base amount
    _txn(db, uber, _at(date(2026, 9, 21)), gross=1_000, fee=100, currency="EUR", base=1_100)  # → 1100 − 110
    rep = sync_withholdings(db, policy=WithholdingPolicy(mode="percent", rate_bp=1000, start=date(2026, 9, 1)),
                            today=TODAY, tz=NY, currency="USD")
    assert rep.plans_other_currency == 1 and rep.processed == 1 and rep.withheld_minor == 99   # 990 × 10%


def test_policy_start_and_off(db):
    uber = _platform(db)
    _setup(db)
    _txn(db, uber, _at(date(2026, 9, 10)), gross=10_000, fee=0)
    _txn(db, uber, _at(date(2026, 9, 25)), gross=10_000, fee=0)
    rep = sync_withholdings(db, policy=WithholdingPolicy(mode="percent", rate_bp=500, start=date(2026, 9, 20)),
                            today=TODAY, tz=NY, currency="USD")
    assert rep.processed == 1 and rep.withheld_minor == 500
    assert sync_withholdings(db, policy=WithholdingPolicy(mode="off"), today=TODAY, tz=NY, currency="USD").processed == 0


def test_payout_day_uses_local_time(db):
    uber = _platform(db)
    _setup(db)
    _txn(db, uber, "2026-09-20T02:00:00.000Z", gross=10_000, fee=0)       # Sep 19, 10 PM in New York
    sync_withholdings(db, policy=WithholdingPolicy(mode="percent", rate_bp=500, start=date(2026, 9, 1)),
                      today=TODAY, tz=NY, currency="USD")
    assert _entries(db)[0]["occurred_on"] == "2026-09-19"


def test_reserve_status_week(db):
    uber = _platform(db)
    _setup(db, premium=45_000)
    _txn(db, uber, _at(date(2026, 9, 18)), gross=10_000, fee=0)            # previous week
    _txn(db, uber, _at(date(2026, 9, 22)), gross=10_000, fee=0)            # this week (Mon Sep 21)
    sync_withholdings(db, policy=WithholdingPolicy(mode="percent", rate_bp=1000, start=date(2026, 9, 1)),
                      today=TODAY, tz=NY, currency="USD")
    st = reserve_status(db, today=TODAY, currency="USD")
    assert (st.balance_minor, st.this_week_minor, st.weekly_need_minor, st.due_on) == (2_000, 1_000, 10_385,
                                                                                         date(2026, 10, 1))
    assert (st.premium_due_minor, st.covered_minor, st.shortfall_minor) == (45_000, 2_000, 43_000)


# =============================================================================================
# eligibility
# =============================================================================================


RULES = EligibilityRules(min_membership_days=30, states=("FL", "GA"), min_age=18, max_age=64,
                         min_net_earnings_90d_minor=150_000, min_hours_per_week=10, max_members=4)
GOOD = WorkEvidence(net_earnings_minor=900_000, worked_seconds=20 * 3600 * 13)        # ~20 h/week over 90 days


def test_likely_eligible():
    r = evaluate(RULES, PROFILE, today=TODAY, evidence=GOOD)
    assert r.status == "likely_eligible" and all(c.outcome == "pass" for c in r.checks)


@pytest.mark.parametrize("change,rule", [
    (dict(state="WA"), "service_area"), (dict(birth_year=1950), "age"),
    (dict(association_member_since=date(2026, 9, 10)), "membership"),
])
def test_each_rule_can_fail(change, rule):
    p = MemberProfile(**{**PROFILE.__dict__, **change})
    r = evaluate(RULES, p, today=TODAY, evidence=GOOD)
    assert r.status == "not_eligible" and [c.rule for c in r.failed] == [rule]


def test_work_evidence_rules():
    thin = WorkEvidence(net_earnings_minor=100_000, worked_seconds=5 * 3600 * 13)
    r = evaluate(RULES, PROFILE, today=TODAY, evidence=thin)
    assert {c.rule for c in r.failed} == {"self_employment", "hours"}
    assert GOOD.hours_per_week == 20 and WorkEvidence(0, 0).hours_per_week == 0
    unknown = evaluate(RULES, PROFILE, today=TODAY, evidence=None)
    assert unknown.status == "check_with_association"


def test_age_edge_is_unknown_not_guessed():
    edge = MemberProfile(state="FL", birth_year=TODAY.year - 65)           # 64 or 65 depending on birthday
    r = evaluate(EligibilityRules(max_age=64), edge, today=TODAY)
    assert r.status == "check_with_association" and r.checks[0].outcome == "unknown"


def test_members_and_companion_line():
    r = evaluate(EligibilityRules(max_members=1, requires_line=Line.HEALTH), PROFILE, today=TODAY,
                 coverage_tier=CoverageTier.FAMILY)
    assert {c.rule for c in r.failed} == {"members", "companion_line"}
    ok = evaluate(EligibilityRules(requires_line=Line.HEALTH), PROFILE, today=TODAY, active_lines=[Line.HEALTH])
    assert ok.status == "likely_eligible"


def test_missing_profile():
    assert evaluate(RULES, None, today=TODAY, evidence=GOOD).status == "check_with_association"


# =============================================================================================
# models & compliance
# =============================================================================================


def test_models():
    assert [age_band(a) for a in (18, 29, 30, 64, 65, 90)] == ["18-29", "18-29", "30-39", "60-64", "65+", "65+"]
    with pytest.raises(HealthError):
        age_band(17)
    for bad in (dict(state="Florida"), dict(state="F1"), dict(birth_year=1800)):
        with pytest.raises(HealthError):
            MemberProfile(**{"state": "FL", "birth_year": 1990, **bad})
    assert MemberProfile.from_dict(PROFILE.to_dict()) == PROFILE
    with pytest.raises(HealthError):
        _offer(monthly_premium_minor=450.0)


def _offer(**kw):
    base = dict(offer_id="o", broker_id="b", association="Guild", carrier="C", line=Line.HEALTH, name="Plan",
                guild_tier="Silver", monthly_premium_minor=41_500, currency="USD", coverage_tier=CoverageTier.SELF)
    return PlanOffer(**{**base, **kw})


@pytest.mark.parametrize("kw", [dict(sponsor_type="platform"), dict(platform_contribution_minor=100),
                                dict(member_directed_payment=False)])
def test_co_employment_guardrails(kw):
    with pytest.raises(ComplianceError):
        check_offer(_offer(**kw))
    check_offer(_offer())
    check_offer(_offer(sponsor_type="carrier"))


def test_forbidden_payload_keys_anywhere():
    payload = {"a": {"b": [{"platform_id": "x"}]}, "payroll": 1, "note": {"diagnosis": "…"}}
    assert set(forbidden_keys(payload)) == {"a.b[0].platform_id", "payroll", "note.diagnosis"}
    with pytest.raises(ComplianceError):
        check_payload(payload)


def test_quote_payload_is_minimal():
    q = quote_payload(PROFILE, today=TODAY, lines=[Line.HEALTH, Line.DENTAL])
    assert q == {"state": "FL", "age_band": "30-39", "coverage_tier": "self", "lines": ["dental", "health"],
                 "currency": "USD"}
    blob = json.dumps(q)
    assert "1990" not in blob and "GLD-448812" not in blob


def test_enrollment_payload_and_consent_rows():
    applicant = Applicant("Jordan Rivera", "jordan@example.com", date(1990, 4, 2))
    p = enrollment_payload(_offer(), PROFILE, applicant, coverage_start=date(2026, 10, 1),
                           consented_at="2026-09-27T12:00:00.000Z")
    assert forbidden_keys(p) == []
    rows = dict(describe_payload(p))
    assert rows["Legal name"] == "Jordan Rivera" and rows["Date of birth"] != "1990-04-02"      # masked on screen
    assert rows["Who's covered"] == "Just me" and rows["Coverage type"] == "Health"
    assert rows["Association member ID"].startswith("GL") and "448812" not in rows["Association member ID"]
    assert "Jordan" not in json.dumps(redact(p))
    with pytest.raises(HealthError):
        enrollment_payload(_offer(), PROFILE, Applicant("J R", "j@x.io", date(1991, 1, 1)),
                           coverage_start=date(2026, 10, 1), consented_at="x")
    with pytest.raises(HealthError):
        Applicant("J", "bad", date(1990, 1, 1))


# =============================================================================================
# brokerages
# =============================================================================================


def test_sandbox_catalog_prices_are_integer_and_deterministic():
    sb = SandboxBroker()
    q = sb.quote(quote_payload(PROFILE, today=TODAY, lines=list(Line)))
    by_id = {o.offer_id: o for o in q.offers}
    assert by_id["sbx-health-silver-self"].monthly_premium_minor == 41_500                  # 30–39, self
    assert by_id["sbx-dental-basic-self"].monthly_premium_minor == 2_400
    assert all(isinstance(o.monthly_premium_minor, int) for o in q.offers)
    assert len(q.refused) == 1 and "platform" in q.refused[0]                               # co-employment filter
    fam = sb.quote(quote_payload(MemberProfile(state="FL", birth_year=1982, coverage_tier=CoverageTier.FAMILY),
                                 today=TODAY, lines=list(Line)))
    fam_ids = {o.offer_id: o for o in fam.offers}
    assert fam_ids["sbx-health-silver-family"].monthly_premium_minor == 149_027              # 41500×1.26×2.85
    assert not any(o.line is Line.LIFE for o in fam.offers)                                  # member-only life


@pytest.mark.parametrize("premium", [415.0, "41500", True, None])
def test_offers_with_non_integer_premiums_are_rejected(premium):
    raw = {"offer_id": "x", "association": "A", "carrier": "C", "line": "health", "name": "N",
           "monthly_premium_minor": premium, "currency": "USD", "coverage_tier": "self"}
    with pytest.raises(BrokerError):
        parse_offer(raw, "b")


def test_approved_broker_allowlist():
    env = {"IFLOW_HEALTH_BROKERS": "acme|Acme Benefits|https://api.acme.example, bad|Plain|http://x.example, "
                                      "evil-id|Evil|https://e.example, creds|C|https://u:p@c.example, junk"}
    brokers = approved_brokers(env)
    assert set(brokers) == {"sandbox", "acme"} and brokers["acme"].base_url == "https://api.acme.example"


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


def _rest(responses):
    info = approved_brokers({"IFLOW_HEALTH_BROKERS": "acme|Acme|https://api.acme.example"})["acme"]
    t = FakeTransport(responses)
    return RestBroker(info, "tok-" + "x" * 30, transport=t), t


def test_rest_broker_quote_and_enroll():
    offer = {"offer_id": "a1", "association": "Acme Guild", "carrier": "Acme Mutual", "line": "dental",
             "name": "Acme Dental", "monthly_premium_minor": 3_100, "currency": "USD", "coverage_tier": "self"}
    rb, t = _rest([HttpResponse(200, json.dumps({"offers": [offer, {**offer, "monthly_premium_minor": 31.0}]}).encode()),
                   HttpResponse(200, json.dumps({"enrollment_ref": "ACME-1", "status": "pending",
                                                 "coverage_start": "2026-10-01"}).encode())])
    q = rb.quote(quote_payload(PROFILE, today=TODAY, lines=[Line.DENTAL]))
    assert [o.offer_id for o in q.offers] == ["a1"] and len(q.refused) == 1
    method, url, kw = t.calls[0]
    assert (method, url) == ("POST", "https://api.acme.example/v1/quotes")
    assert kw["headers"]["Authorization"].startswith("Bearer tok-") and "GLD" not in kw["body"].decode()
    applicant = Applicant("Jordan Rivera", "jordan@example.com", date(1990, 4, 2))
    p = enrollment_payload(q.offers[0], PROFILE, applicant, coverage_start=date(2026, 10, 1), consented_at="x")
    receipt = rb.enroll(p)
    assert (receipt.enrollment_ref, receipt.status) == ("ACME-1", "pending") and "tok-" not in repr(rb)


def test_rest_broker_errors_never_echo_payload():
    rb, _ = _rest([HttpResponse(401, b"")])
    applicant = Applicant("Jordan Rivera", "jordan@example.com", date(1990, 4, 2))
    p = enrollment_payload(_offer(), PROFILE, applicant, coverage_start=date(2026, 10, 1), consented_at="x")
    with pytest.raises(BrokerError) as exc:
        rb.enroll(p)
    assert "Jordan" not in str(exc.value) and "jordan@" not in str(exc.value) and "rejected" in str(exc.value)
    rb2, _ = _rest([HttpResponse(200, b'{"enrollment_ref": "x", "status": "approved!", "coverage_start": "?"}')])
    with pytest.raises(BrokerError):
        rb2.status("x")
    with pytest.raises(BrokerError):
        RestBroker(approved_brokers()["sandbox"], "t")
    with pytest.raises(ComplianceError):
        rb.quote({"state": "FL", "platforms": ["uber"]})


# =============================================================================================
# enrollment workflow
# =============================================================================================


def test_browse_enroll_and_activate(db, caplog):
    sb = SandboxBroker()
    cat = browse(sb, PROFILE, today=TODAY, evidence=GOOD)
    silver = next(q for q in cat.offers if q.offer.offer_id == "sbx-health-silver-self")
    assert silver.eligibility.status == "likely_eligible" and len(cat.refused) == 1
    applicant = Applicant("Jordan Rivera", "jordan@example.com", date(1990, 4, 2))
    caplog.set_level(logging.DEBUG)
    plan_id = enroll(db, sb, silver, PROFILE, applicant, today=TODAY, consented_at="2026-09-27T12:00:00.000Z")
    (plan,) = active_plans(db, include_pending=True)
    assert (plan.id, plan.status, plan.line, plan.broker_id, plan.monthly_premium_minor, plan.coverage_start,
            plan.due_day) == (plan_id, "pending", "health", "sandbox", 41_500, date(2026, 10, 1), 1)
    assert plan.enrollment_ref.startswith("SBX-") and active_plans(db) == []          # pending isn't "active" yet
    # applicant details were used once and stored nowhere
    with db.connection() as conn:
        dump = "\n".join(str(tuple(r)) for t in ("health_plans", "health_premium_entries", "device_settings")
                         for r in conn.execute(f"SELECT * FROM {t}"))
    assert "Jordan" not in dump and "jordan@example.com" not in dump and "1990-04-02" not in dump
    assert "Jordan" not in caplog.text and "1990-04-02" not in caplog.text
    assert refresh_statuses(db, today=date(2026, 9, 30)) == 0
    assert refresh_statuses(db, today=date(2026, 10, 1)) == 1 and active_plans(db)[0].status == "active"


def test_enroll_refuses_ineligible_and_past_starts(db):
    sb = SandboxBroker()
    newbie = MemberProfile(state="FL", birth_year=1990, association_member_since=TODAY)
    q = next(q for q in browse(sb, newbie, today=TODAY, evidence=GOOD).offers if q.offer.line is Line.HEALTH)
    assert q.eligibility.status == "not_eligible"
    applicant = Applicant("Jordan Rivera", "jordan@example.com", date(1990, 4, 2))
    with pytest.raises(HealthError):
        enroll(db, sb, q, newbie, applicant, today=TODAY, consented_at="x")
    ok = next(q for q in browse(sb, PROFILE, today=TODAY, evidence=GOOD).offers if q.offer.line is Line.DENTAL)
    with pytest.raises(HealthError):
        enroll(db, sb, ok, PROFILE, applicant, today=TODAY, consented_at="x", coverage_start=TODAY)
    assert first_of_next_month(date(2026, 12, 15)) == date(2027, 1, 1)


def test_one_active_plan_per_line(db):
    h1 = save_plan(db, association="G", plan_name="H1", monthly_premium_minor=40_000, coverage_start=TODAY)
    save_plan(db, association="G", plan_name="D1", monthly_premium_minor=3_000, coverage_start=TODAY, line="dental")
    h2 = save_plan(db, association="G", plan_name="H2", monthly_premium_minor=42_000, coverage_start=TODAY)
    assert [(p.line, p.plan_name) for p in active_plans(db)] == [("health", "H2"), ("dental", "D1")]
    assert h1 != h2
    with pytest.raises(ValueError):                                     # PremiumError
        save_plan(db, association="G", plan_name="X", monthly_premium_minor=1, coverage_start=TODAY, line="vision")


def test_health_profile_is_sealed_on_platform_storage(db):
    from core.crypto import FieldSealer
    from core.device_settings import SettingsStore

    store = SettingsStore(db, sealer=FieldSealer("ef" * 32), require_sealing=True)
    store.update(health_profile=PROFILE.to_dict(), health_broker_tokens={"acme": "secret-token"})
    with db.connection() as conn:
        raw = " ".join(r[0] for r in conn.execute("SELECT value_json FROM device_settings"))
    assert "GLD-448812" not in raw and "secret-token" not in raw and "1990" not in raw
    assert MemberProfile.from_dict(store.load().health_profile) == PROFILE


def test_health_engine_network_boundary():
    """Only brokers.py may reach the network (lazily, for approved HTTPS brokerages)."""
    pkg = Path(__file__).resolve().parents[1] / "core" / "health"
    forbidden = ("urllib.request", "http", "socket", "requests", "httpx", "core.ai", "core.telemetry")
    offenders = []
    for path in pkg.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
            for n in names:
                if any(n == f or n.startswith(f + ".") for f in forbidden):
                    offenders.append(f"{path.name}:{n}")
                if n == "core.fleet.http" and path.name != "brokers.py":
                    offenders.append(f"{path.name}:{n}")
    assert offenders == []
