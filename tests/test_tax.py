"""Tax math. Expected values are hand-computed or taken from the official
rate-schedule "base tax" columns (IRS Rev. Proc. 2025-32, FTB 2025 540)."""

from __future__ import annotations

from datetime import date

import pytest

from core.financial.tax import (
    Bracket,
    TaxEstimator,
    TaxError,
    TaxInputs,
    TaxTableRegistry,
    UnsupportedJurisdiction,
    estimate_flat,
    estimate_for_year,
    marginal_rate_bp,
    mileage_deduction,
    progressive_tax,
    qbi_deduction,
    quarterly_due_dates,
    self_employment_tax,
    summarize_year,
)

from core.financial.money import apply_bp

from .conftest import new_id

D = 100  # dollars → cents


@pytest.fixture(scope="module")
def reg():
    return TaxTableRegistry.load()


@pytest.fixture(scope="module")
def fed26(reg):
    return reg.get("US", 2026)[0]


# --- tables -------------------------------------------------------------------------


def test_registry_contents(reg):
    assert {"US", "US-CA", "US-IL", "US-PA", "US-TX", "US-FL", "US-WA"} <= set(reg.jurisdictions())
    fed25, _ = reg.get("US", 2025)
    assert fed25.self_employment.social_security_wage_base == 176_100 * D
    assert reg.get("US", 2026)[0].self_employment.social_security_wage_base == 184_500 * D
    assert fed25.standard_deduction_for("single") == 15_750 * D     # OBBBA-amended 2025 amount
    assert reg.get("US", 2026)[0].standard_deduction_for("married_joint") == 32_200 * D


def test_every_bracket_table_ascends_from_zero(reg):
    for j in reg.jurisdictions():
        for year in (2025, 2026):
            table, _ = reg.get(j, year)
            for rows in table.brackets.values():
                assert rows[0].lower_minor == 0
                assert all(a.lower_minor < b.lower_minor for a, b in zip(rows, rows[1:]))


def test_missing_year_falls_back_and_is_flagged(reg):
    table, provisional = reg.get("US", 2027)
    assert provisional and table.tax_year == 2026
    assert reg.get("US", 2026)[1] is False
    with pytest.raises(UnsupportedJurisdiction):
        reg.get("US", 2019)
    with pytest.raises(UnsupportedJurisdiction):
        reg.get("GB", 2026)


# --- brackets -------------------------------------------------------------------------


@pytest.mark.parametrize("status,taxable,expected", [
    ("single", 105_700, 17_966.00),
    ("single", 12_400, 1_240.00),
    ("married_joint", 100_800, 11_600.00),
    ("head_of_household", 105_700, 16_155.00),
    ("head_of_household", 201_750, 39_207.00),
    ("head_of_household", 640_600, 191_171.00),
    ("married_separate", 384_350, 103_291.75),
    ("qualifying_surviving_spouse", 100_800, 11_600.00),   # uses joint schedule
])
def test_federal_2026_matches_rev_proc(fed26, status, taxable, expected):
    assert progressive_tax(taxable * D, fed26.brackets_for(status)) == round(expected * D)


CA_BASES = {
    "single": [(11_079, 110.79), (26_264, 414.49), (41_452, 1_022.01), (57_542, 1_987.41), (72_724, 3_201.97),
               (371_479, 30_986.19), (445_771, 38_638.27), (742_953, 72_219.84)],
    "married_joint": [(22_158, 221.58), (52_528, 828.98), (82_904, 2_044.02), (115_084, 3_974.82),
                      (145_448, 6_403.94), (742_958, 61_972.37), (891_542, 77_276.52), (1_485_906, 144_439.65)],
    "head_of_household": [(22_173, 221.73), (52_530, 828.87), (67_716, 1_436.31), (83_805, 2_401.65),
                          (98_990, 3_616.45), (505_208, 41_394.72), (606_251, 51_802.15), (1_010_417, 97_472.91)],
}


@pytest.mark.parametrize("status", CA_BASES)
def test_california_2025_matches_ftb_schedule_to_the_cent(reg, status):
    ca, _ = reg.get("US-CA", 2025)
    for taxable, base in CA_BASES[status]:
        assert progressive_tax(taxable * D, ca.brackets_for(status)) == round(base * D), (status, taxable)


def test_progressive_edge_cases():
    b = (Bracket(0, 1000), Bracket(10_000, 2000))
    assert progressive_tax(0, b) == 0
    assert progressive_tax(-500, b) == 0
    assert progressive_tax(10_001, b) == 1000
    assert progressive_tax(10_005, b) == 1001
    assert marginal_rate_bp(10_000, b) == 1000
    assert marginal_rate_bp(10_001, b) == 2000
    assert marginal_rate_bp(0, b) == 0


# --- self-employment tax -------------------------------------------------------------


def test_se_tax_typical(fed26):
    se = self_employment_tax(45_000 * D, fed26.self_employment, "single")
    assert se.net_earnings == 4_155_750                  # × 92.35%
    assert se.social_security == 515_313                 # 12.4%
    assert se.medicare == 120_517                        # 2.9%
    assert se.additional_medicare == 0
    assert se.total == 635_830
    assert se.deductible_half == 317_915


def test_se_tax_below_400_threshold(fed26):
    se = self_employment_tax(400 * D, fed26.self_employment, "single")   # NE = $369.40
    assert se.total == 0 and se.net_earnings == 36_940


def test_se_tax_wage_base_and_additional_medicare(fed26):
    se = self_employment_tax(250_000 * D, fed26.self_employment, "single")
    assert se.net_earnings == 23_087_500
    assert se.social_security == 2_287_800               # capped at $184,500
    assert se.medicare == 669_538                        # 6,695.375 → half-up
    assert se.additional_medicare == 27_788              # 0.9% over $200k
    assert se.total == 2_985_126
    assert se.deductible_half == 1_478_669               # excludes Additional Medicare


def test_se_tax_w2_wages_reduce_room(fed26):
    se = self_employment_tax(45_000 * D, fed26.self_employment, "single",
                             w2_social_security_wages=150_000 * D, w2_medicare_wages=190_000 * D)
    assert se.social_security == 427_800                 # only $34,500 of room left
    assert se.additional_medicare == apply_expected(4_155_750 - (200_000 - 190_000) * D)


def apply_expected(over):  # 0.9% half-up
    return (over * 90 + 5_000) // 10_000


# --- QBI -------------------------------------------------------------------------------


def test_qbi_limited_by_taxable_income(fed26):
    assert qbi_deduction(4_182_085, 2_572_085, "single", fed26.qbi) == 514_417


def test_qbi_phase_in_2025(reg):
    fed25, _ = reg.get("US", 2025)
    # Halfway through the $50k single phase-in range → half of 20% of QBI.
    assert qbi_deduction(100_000 * D, (197_300 + 25_000) * D, "single", fed25.qbi) == 10_000 * D
    assert qbi_deduction(100_000 * D, (197_300 + 50_000) * D, "single", fed25.qbi) == 0


def test_qbi_2026_minimum_deduction(fed26):
    # Fully phased out, but OBBBA's $400 floor applies with ≥ $1,000 of QBI.
    assert qbi_deduction(100_000 * D, 600_000 * D, "single", fed26.qbi) == 400 * D
    assert qbi_deduction(999 * D, 600_000 * D, "single", fed26.qbi) == 0
    # Joint filers get the wider $150k range.
    assert qbi_deduction(100_000 * D, (403_500 + 75_000) * D, "married_joint", fed26.qbi) == 10_000 * D


def test_qbi_non_positive(fed26):
    assert qbi_deduction(-5, 100, "single", fed26.qbi) == 0
    assert qbi_deduction(100, 0, "single", fed26.qbi) == 0


# --- mileage ---------------------------------------------------------------------------

THOUSAND_MILES_M = 1_609_344


def test_mileage_uses_effective_dated_rates(fed26, reg):
    assert mileage_deduction({date(2026, 5, 1): THOUSAND_MILES_M}, fed26) == 72_500
    assert mileage_deduction({date(2026, 8, 1): THOUSAND_MILES_M}, fed26) == 76_000
    both = {date(2026, 6, 30): THOUSAND_MILES_M, date(2026, 7, 1): THOUSAND_MILES_M}
    assert mileage_deduction(both, fed26) == 148_500
    assert mileage_deduction({date(2025, 3, 1): THOUSAND_MILES_M}, reg.get("US", 2025)[0]) == 70_000


def test_mileage_rounding_and_override(fed26):
    assert mileage_deduction({date(2026, 1, 2): 1609}, fed26) == 72      # 72.48¢ → 72
    assert mileage_deduction({date(2026, 1, 2): 1610}, fed26) == 73      # 72.53¢ → 73
    assert mileage_deduction({date(2026, 1, 2): 100_000}, fed26, override_rate_minor=50, override_unit="km") == 5_000
    with pytest.raises(TaxError):
        mileage_deduction({date(2026, 1, 2): -1}, fed26)


# --- full estimates --------------------------------------------------------------------


def test_full_estimate_single_california(reg):
    est = TaxEstimator(reg).estimate(TaxInputs(
        2026, "single", "US-CA",
        gross_receipts=60_000 * D, deductible_expenses=5_000 * D, mileage_deduction=10_000 * D,
    ))
    assert est.net_profit == 45_000 * D
    assert est.se.total == 635_830
    assert est.adjusted_gross_income == 4_182_085
    assert est.qbi_deduction == 514_417
    assert est.federal_taxable_income == 2_057_668
    assert est.federal_income_tax == 222_120
    assert est.federal_marginal_rate_bp == 1200
    assert est.state_taxable_income == 3_611_485
    assert est.state_income_tax == 80_852
    assert est.total_tax == 938_802
    assert est.quarterly_payments == (234_701, 234_701, 234_700, 234_700)
    assert sum(est.quarterly_payments) == est.total_tax
    assert est.effective_rate_bp == 2086
    assert est.provisional == ("US-CA: using 2025 tables for 2026",)


def test_no_income_tax_state_and_withholding(reg):
    est = TaxEstimator(reg).estimate(TaxInputs(2026, "single", "US-TX", gross_receipts=45_000 * D,
                                               withholding=2_000 * D))
    assert est.state_income_tax == 0
    assert any("no broad-based income tax" in n for n in est.notes)
    assert est.balance_due == est.total_tax - 2_000 * D


def test_flat_state_and_ca_surcharge(reg):
    il = TaxEstimator(reg).estimate(TaxInputs(2025, "single", "US-IL", gross_receipts=50_000 * D))
    assert il.state_income_tax == apply_bp(il.adjusted_gross_income, 495)

    ca = TaxEstimator(reg).estimate(TaxInputs(2025, "single", "US-CA", gross_receipts=1_200_000 * D))
    ca_table, _ = reg.get("US-CA", 2025)
    base = progressive_tax(ca.state_taxable_income, ca_table.brackets_for("single"))
    surcharge = (ca.state_taxable_income - 1_000_000 * D + 50) // 100
    assert ca.state_income_tax == base + surcharge


def test_loss_year_owes_nothing(reg):
    est = TaxEstimator(reg).estimate(TaxInputs(2026, gross_receipts=1_000 * D, deductible_expenses=3_000 * D))
    assert est.net_profit < 0
    assert est.total_tax == 0 and est.quarterly_payments == (0, 0, 0, 0)


def test_bad_status_rejected(reg):
    with pytest.raises(TaxError):
        TaxEstimator(reg).estimate(TaxInputs(2026, "married"))


def test_quarterly_due_dates_skip_weekends():
    assert quarterly_due_dates(2026) == (date(2026, 4, 15), date(2026, 6, 15), date(2026, 9, 15), date(2027, 1, 15))
    q = quarterly_due_dates(2028)
    assert q[0] == date(2028, 4, 17) and all(d.weekday() < 5 for d in q)


def test_estimate_flat():
    assert estimate_flat(10_000 * D, 2000, 1000) == {"income_tax": 200_000, "self_employment_tax": 100_000,
                                                     "total": 300_000}


# --- ledger aggregation ---------------------------------------------------------------


def _seed(db, platform_id):
    with db.transaction() as conn:
        rows = [
            (60_000 * D, 6_000 * D, "USD", None, "2026-03-01T12:00:00Z"),
            (1_000 * D, 0, "EUR", 1_100 * D, "2026-04-01T12:00:00Z"),     # pinned base amount
            (500 * D, 0, "EUR", None, "2026-04-02T12:00:00Z"),            # unconverted → excluded
            (9_999 * D, 0, "USD", None, "2025-12-31T23:59:59Z"),          # other year
        ]
        for gross, fee, ccy, base, when in rows:
            conn.execute(
                "INSERT INTO transactions (id, platform_id, gross_amount_minor, platform_fee_minor, currency, "
                "base_amount_minor, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_id(), platform_id, gross, fee, ccy, base, when))
        for cat, amount, bp in [("fuel", 3_000 * D, 10_000), ("phone", 1_000 * D, 6_000),
                                ("meals", 200 * D, 10_000), ("parking_tolls", 150 * D, 10_000)]:
            conn.execute(
                "INSERT INTO expenses (id, category, amount_minor, currency, business_use_bp, occurred_at) "
                "VALUES (?, ?, ?, 'USD', ?, '2026-05-01')", (new_id(), cat, amount, bp))
        for when, meters, purpose in [("2026-05-01T09:00:00Z", THOUSAND_MILES_M, "business"),
                                      ("2026-08-01T09:00:00Z", THOUSAND_MILES_M, "business"),
                                      ("2026-08-02T09:00:00Z", THOUSAND_MILES_M, "commute")]:
            conn.execute("INSERT INTO mileage_logs (id, started_at, distance_m, purpose) VALUES (?, ?, ?, ?)",
                         (new_id(), when, meters, purpose))


def test_summarize_year_standard_mileage(db, platform_id, fed26):
    _seed(db, platform_id)
    s = summarize_year(db, 2026, base_currency="USD", table=fed26)
    assert s.gross_receipts == 61_100 * D
    assert s.platform_fees == 6_000 * D
    # fuel excluded (standard mileage); phone 60%; meals 50%; parking kept.
    assert s.deductible_expenses == (600 + 100 + 150) * D
    assert s.business_meters == 2 * THOUSAND_MILES_M        # commute excluded
    assert s.mileage_deduction == 148_500
    assert s.excluded_unconverted == 1 and s.warnings


def test_summarize_year_actual_expenses(db, platform_id, fed26):
    _seed(db, platform_id)
    s = summarize_year(db, 2026, base_currency="USD", vehicle_method="actual", table=fed26)
    assert s.deductible_expenses == (3_000 + 600 + 100 + 150) * D
    assert s.mileage_deduction == 0


def test_estimate_for_year_uses_default_profile(db, platform_id):
    _seed(db, platform_id)
    with db.transaction() as conn:
        conn.execute("INSERT INTO tax_profiles (tax_year, country_code, region_code, filing_status, is_default) "
                     "VALUES (2026, 'US', 'US-PA', 'married_joint', 1)")
    est, summary = estimate_for_year(db, 2026)
    assert est.filing_status == "married_joint" and est.region == "US-PA"
    assert est.net_profit == summary.gross_receipts - summary.platform_fees - summary.deductible_expenses \
        - summary.mileage_deduction
    assert est.state_income_tax == apply_bp(est.adjusted_gross_income, 307)


def test_estimate_for_year_mileage_override(db, platform_id):
    _seed(db, platform_id)
    with db.transaction() as conn:
        conn.execute("INSERT INTO tax_profiles (tax_year, country_code, mileage_rate_minor, mileage_rate_unit) "
                     "VALUES (2026, 'US', 50, 'mi')")
    _, summary = estimate_for_year(db, 2026)
    assert summary.mileage_deduction == 100_000      # 2,000 mi × $0.50


def test_non_us_profile_is_explicitly_unsupported(db):
    with db.transaction() as conn:
        conn.execute("INSERT INTO tax_profiles (tax_year, country_code, income_tax_rate_bp) VALUES (2026, 'GB', 2000)")
    with pytest.raises(UnsupportedJurisdiction, match="estimate_flat"):
        estimate_for_year(db, 2026, base_currency="GBP")
