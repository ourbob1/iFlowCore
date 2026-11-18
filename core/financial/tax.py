"""
Tax liability estimator (self-employment + federal/state income tax).

Everything is integer minor units; rates are basis points. Rounding happens
once per line item with ROUND_HALF_UP, mirroring how the forms are filled.

Scope and simplifications (surfaced to users as estimate caveats)
-----------------------------------------------------------------
* US Schedule SE: net earnings = net profit × 92.35%; Social Security 12.4%
  up to the wage base (reduced by W-2 Social Security wages); Medicare 2.9%
  uncapped; Additional Medicare 0.9% above the filing-status threshold
  (reduced by W-2 Medicare wages); no SE tax below $400 of net earnings.
  Half of SE tax (excluding Additional Medicare) is deducted from income.
* Federal income tax: AGI − standard deduction − QBI deduction, then brackets.
  QBI is modeled for a non-SSTB sole proprietor with no W-2 wages paid and no
  qualified property — the normal gig-worker case — including the phase-in
  range and (2026+) the $400 minimum deduction. Itemized deductions, credits,
  capital gains rates, and AMT are not modeled.
* State tax starts from federal AGI (states differ; CA, for example, does
  not allow QBI) less the state standard deduction, then state brackets and
  surcharges. Credits (e.g. CA exemption credits) are not modeled.
* Tables are data files in `tax_tables/`. If a year's table isn't published
  yet, the latest earlier year is used and the estimate is flagged
  `provisional` — the approach the California FTB itself prescribes for
  estimated payments.

Nothing here is tax advice; it produces planning estimates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from core.financial.expenses import VEHICLE_OPERATING_CATEGORIES, deductible_amount
from core.financial.money import BP_SCALE, allocate, apply_bp
from core.timeutil import local_date

TABLES_DIR = Path(__file__).with_name("tax_tables")
METERS_PER_MILE = Decimal("1609.344")
METERS_PER_KM = Decimal(1000)

FILING_STATUSES = (
    "single", "married_joint", "married_separate", "head_of_household", "qualifying_surviving_spouse", "other",
)
# Fallback chain when a table doesn't list a status explicitly.
_STATUS_FALLBACKS: Mapping[str, tuple[str, ...]] = {
    "qualifying_surviving_spouse": ("qualifying_surviving_spouse", "married_joint"),
    "other": ("other", "single"),
}


class TaxError(ValueError):
    pass


class UnsupportedJurisdiction(TaxError):
    pass


def _check_status(status: str) -> str:
    if status not in FILING_STATUSES:
        raise TaxError(f"Unknown filing status '{status}'; allowed: {FILING_STATUSES}")
    return status


def _lookup(table: Mapping[str, Any], status: str, what: str) -> Any:
    for key in (*_STATUS_FALLBACKS.get(status, (status,)), "*", "default"):
        if key in table:
            return table[key]
    raise TaxError(f"No {what} for filing status '{status}'")


def _round_div(numerator: int, denominator: int) -> int:
    """Integer division rounded half-up (numerator may be negative)."""
    q = Decimal(numerator) / Decimal(denominator)
    return int(q.quantize(Decimal(1), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Table model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bracket:
    lower_minor: int      # bracket applies to income above this amount
    rate_bp: int


@dataclass(frozen=True)
class SERules:
    net_earnings_factor_bp: int
    social_security_rate_bp: int
    medicare_rate_bp: int
    social_security_wage_base: int
    minimum_net_earnings: int
    additional_medicare_rate_bp: int
    additional_medicare_threshold: Mapping[str, int]


@dataclass(frozen=True)
class QBIRules:
    rate_bp: int
    threshold: Mapping[str, int]
    phase_in_range: Mapping[str, int]
    minimum_deduction: int = 0
    minimum_qbi_for_minimum: int = 0


@dataclass(frozen=True)
class Surcharge:
    name: str
    rate_bp: int
    threshold: Mapping[str, int]


@dataclass(frozen=True)
class MileageRate:
    effective_from: date
    rate_per_mile_deci_minor: int   # tenths of a minor unit: 725 = 72.5¢


@dataclass(frozen=True)
class TaxTable:
    jurisdiction: str
    tax_year: int
    currency: str
    kind: Literal["progressive", "none"]
    brackets: Mapping[str, tuple[Bracket, ...]] = field(default_factory=dict)
    standard_deduction: Mapping[str, int] = field(default_factory=dict)
    surcharges: tuple[Surcharge, ...] = ()
    self_employment: SERules | None = None
    qbi: QBIRules | None = None
    mileage_rates: tuple[MileageRate, ...] = ()
    # Depreciation portion of the business mileage rate (basis reduction).
    mileage_depreciation_rates: tuple[MileageRate, ...] = ()
    # §280F caps for passenger autos placed in service in this table's year.
    auto_depreciation_limits: Mapping[str, Any] | None = None
    source: str = ""
    notes: str = ""

    def brackets_for(self, status: str) -> tuple[Bracket, ...]:
        return _lookup(self.brackets, _check_status(status), "brackets")

    def standard_deduction_for(self, status: str) -> int:
        if self.kind == "none":
            return 0
        return _lookup(self.standard_deduction, _check_status(status), "standard deduction")

    def mileage_rate_on(self, day: date) -> MileageRate:
        applicable = [r for r in self.mileage_rates if r.effective_from <= day]
        if not applicable:
            raise TaxError(f"No mileage rate for {day} in {self.jurisdiction} {self.tax_year}")
        return max(applicable, key=lambda r: r.effective_from)

    def mileage_depreciation_rate_on(self, day: date) -> MileageRate:
        applicable = [r for r in self.mileage_depreciation_rates if r.effective_from <= day]
        if not applicable:
            raise TaxError(f"No mileage depreciation rate for {day} in {self.jurisdiction} {self.tax_year}")
        return max(applicable, key=lambda r: r.effective_from)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaxTable":
        brackets = {}
        for status, rows in data.get("brackets", {}).items():
            parsed = tuple(Bracket(int(lo), int(bp)) for lo, bp in rows)
            if not parsed or parsed[0].lower_minor != 0:
                raise TaxError(f"{data['jurisdiction']} {data['tax_year']}: brackets must start at 0")
            if any(prev.lower_minor >= nxt.lower_minor for prev, nxt in zip(parsed, parsed[1:])):
                raise TaxError(f"{data['jurisdiction']} {data['tax_year']}: brackets must ascend")
            brackets[status] = parsed
        se = data.get("self_employment")
        qbi = data.get("qbi")
        return cls(
            jurisdiction=data["jurisdiction"],
            tax_year=int(data["tax_year"]),
            currency=data.get("currency", "USD"),
            kind=data.get("kind", "progressive"),
            brackets=brackets,
            standard_deduction={k: int(v) for k, v in data.get("standard_deduction", {}).items()},
            surcharges=tuple(Surcharge(s["name"], int(s["rate_bp"]), s["threshold"]) for s in data.get("surcharges", [])),
            self_employment=SERules(**se) if se else None,
            qbi=QBIRules(**qbi) if qbi else None,
            mileage_rates=tuple(
                MileageRate(date.fromisoformat(m["effective_from"]), int(m["rate_per_mile_deci_minor"]))
                for m in data.get("mileage_rates", [])
            ),
            mileage_depreciation_rates=tuple(
                MileageRate(date.fromisoformat(m["effective_from"]), int(m["rate_per_mile_deci_minor"]))
                for m in data.get("mileage_depreciation_rates", [])
            ),
            auto_depreciation_limits=data.get("passenger_auto_depreciation_limits"),
            source=data.get("source", ""),
            notes=data.get("notes", ""),
        )


class TaxTableRegistry:
    """All known tables, keyed by (jurisdiction, year)."""

    def __init__(self) -> None:
        self._tables: dict[tuple[str, int], TaxTable] = {}
        self._no_tax: set[str] = set()

    @classmethod
    def load(cls, directory: Path = TABLES_DIR) -> "TaxTableRegistry":
        reg = cls()
        for path in sorted(directory.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("kind") == "none" and "jurisdictions" in data:
                reg._no_tax.update(j.upper() for j in data["jurisdictions"])
            else:
                reg.add(TaxTable.from_dict(data))
        return reg

    def add(self, table: TaxTable) -> None:
        self._tables[(table.jurisdiction.upper(), table.tax_year)] = table

    def jurisdictions(self) -> list[str]:
        return sorted({j for j, _ in self._tables} | self._no_tax)

    def has(self, jurisdiction: str) -> bool:
        j = jurisdiction.upper()
        return j in self._no_tax or any(k[0] == j for k in self._tables)

    def get(self, jurisdiction: str, year: int) -> tuple[TaxTable, bool]:
        """Return (table, provisional). Provisional = an earlier year stood in."""
        j = jurisdiction.upper()
        if j in self._no_tax:
            return TaxTable(j, year, "USD", "none", source="no broad-based income tax"), False
        if (j, year) in self._tables:
            return self._tables[(j, year)], False
        earlier = [y for (jj, y) in self._tables if jj == j and y < year]
        if earlier:
            return self._tables[(j, max(earlier))], True
        raise UnsupportedJurisdiction(f"No tax table for {j} in or before {year}")


# ---------------------------------------------------------------------------
# Core calculations (pure functions)
# ---------------------------------------------------------------------------


def progressive_tax(taxable_minor: int, brackets: Sequence[Bracket]) -> int:
    """Tax on `taxable_minor` using marginal brackets.

    Each bracket's tax is rounded to a whole minor unit before summing — the
    same way published rate schedules compute their "base tax" column, so
    results match the official tables to the cent.
    """
    if taxable_minor <= 0:
        return 0
    total = 0
    for i, bracket in enumerate(brackets):
        if taxable_minor <= bracket.lower_minor:
            break
        upper = brackets[i + 1].lower_minor if i + 1 < len(brackets) else None
        top = taxable_minor if upper is None else min(taxable_minor, upper)
        total += apply_bp(top - bracket.lower_minor, bracket.rate_bp)
    return total


def marginal_rate_bp(taxable_minor: int, brackets: Sequence[Bracket]) -> int:
    rate = brackets[0].rate_bp
    for bracket in brackets:
        if taxable_minor > bracket.lower_minor:
            rate = bracket.rate_bp
    return rate if taxable_minor > 0 else 0


@dataclass(frozen=True)
class SETaxResult:
    net_earnings: int
    social_security: int
    medicare: int
    additional_medicare: int
    total: int
    deductible_half: int


def self_employment_tax(
    net_profit_minor: int,
    rules: SERules,
    status: str,
    *,
    w2_social_security_wages: int = 0,
    w2_medicare_wages: int = 0,
) -> SETaxResult:
    """Schedule SE + Form 8959 (Additional Medicare on SE income)."""
    net_earnings = max(0, apply_bp(net_profit_minor, rules.net_earnings_factor_bp))
    if net_earnings < rules.minimum_net_earnings:
        return SETaxResult(net_earnings, 0, 0, 0, 0, 0)

    ss_room = max(0, rules.social_security_wage_base - w2_social_security_wages)
    social_security = apply_bp(min(net_earnings, ss_room), rules.social_security_rate_bp)
    medicare = apply_bp(net_earnings, rules.medicare_rate_bp)

    threshold = _lookup(rules.additional_medicare_threshold, _check_status(status), "Additional Medicare threshold")
    se_threshold = max(0, threshold - w2_medicare_wages)
    additional = apply_bp(max(0, net_earnings - se_threshold), rules.additional_medicare_rate_bp)

    deductible_half = _round_div(social_security + medicare, 2)
    return SETaxResult(net_earnings, social_security, medicare, additional,
                       social_security + medicare + additional, deductible_half)


def qbi_deduction(qbi_minor: int, taxable_before_qbi: int, status: str, rules: QBIRules) -> int:
    """§199A deduction for a non-SSTB with no W-2 wages/UBIA (see module doc)."""
    if qbi_minor <= 0 or taxable_before_qbi <= 0:
        return 0
    status = _check_status(status)
    tentative = apply_bp(qbi_minor, rules.rate_bp)
    threshold = _lookup(rules.threshold, status, "QBI threshold")
    span = _lookup(rules.phase_in_range, status, "QBI phase-in range")
    excess = taxable_before_qbi - threshold
    if excess >= span:
        tentative = 0
    elif excess > 0:
        # W-2 wage limitation is $0, so the reduction is proportional to how far
        # into the phase-in range taxable income falls.
        tentative -= _round_div(tentative * excess, span)
    if rules.minimum_deduction and qbi_minor >= rules.minimum_qbi_for_minimum:
        tentative = max(tentative, rules.minimum_deduction)
    income_cap = apply_bp(taxable_before_qbi, rules.rate_bp)
    if rules.minimum_deduction and qbi_minor >= rules.minimum_qbi_for_minimum:
        income_cap = max(income_cap, min(rules.minimum_deduction, taxable_before_qbi))
    return max(0, min(tentative, income_cap))


def mileage_deduction(
    meters_by_day: Mapping[date, int],
    table: TaxTable,
    *,
    override_rate_minor: int | None = None,
    override_unit: Literal["mi", "km"] = "mi",
) -> int:
    """Standard mileage deduction. Distances are grouped per rate period and
    rounded once per period.

    `override_rate_minor` (whole minor units per unit, from a user's
    tax_profiles row) replaces the table's effective-dated rates.
    """
    if override_rate_minor is not None:
        per = METERS_PER_MILE if override_unit == "mi" else METERS_PER_KM
        meters = sum(meters_by_day.values())
        return int((Decimal(meters) * override_rate_minor / per).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    by_rate: dict[int, int] = {}
    for day, meters in meters_by_day.items():
        if meters < 0:
            raise TaxError("Negative distance")
        rate = table.mileage_rate_on(day).rate_per_mile_deci_minor
        by_rate[rate] = by_rate.get(rate, 0) + meters
    total = 0
    for rate, meters in by_rate.items():
        value = Decimal(meters) * rate / (METERS_PER_MILE * 10)
        total += int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return total


def _shift_weekend(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def quarterly_due_dates(tax_year: int) -> tuple[date, date, date, date]:
    """IRS 1040-ES due dates, moved off weekends (federal holidays not applied)."""
    return tuple(_shift_weekend(d) for d in (   # type: ignore[return-value]
        date(tax_year, 4, 15), date(tax_year, 6, 15), date(tax_year, 9, 15), date(tax_year + 1, 1, 15)
    ))


# ---------------------------------------------------------------------------
# Estimate orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaxInputs:
    tax_year: int
    filing_status: str = "single"
    region: str | None = None                 # ISO 3166-2, e.g. "US-CA"
    gross_receipts: int = 0
    platform_fees: int = 0
    deductible_expenses: int = 0
    mileage_deduction: int = 0
    other_income: int = 0                     # W-2 wages, interest... (already taxable)
    w2_social_security_wages: int = 0
    w2_medicare_wages: int = 0
    withholding: int = 0                      # federal+state tax already withheld/paid
    apply_qbi: bool = True

    @property
    def net_profit(self) -> int:
        return self.gross_receipts - self.platform_fees - self.deductible_expenses - self.mileage_deduction


@dataclass(frozen=True)
class TaxEstimate:
    tax_year: int
    filing_status: str
    net_profit: int
    se: SETaxResult
    adjusted_gross_income: int
    standard_deduction: int
    qbi_deduction: int
    federal_taxable_income: int
    federal_income_tax: int
    federal_marginal_rate_bp: int
    region: str | None
    state_taxable_income: int
    state_income_tax: int
    total_tax: int
    effective_rate_bp: int                    # total / (net profit + other income)
    balance_due: int                          # total − withholding (≥ 0)
    quarterly_payments: tuple[int, int, int, int]
    quarterly_due_dates: tuple[date, date, date, date]
    provisional: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


class TaxEstimator:
    def __init__(self, registry: TaxTableRegistry | None = None) -> None:
        self.registry = registry or TaxTableRegistry.load()

    def estimate(self, inputs: TaxInputs) -> TaxEstimate:
        status = _check_status(inputs.filing_status)
        provisional: list[str] = []
        notes: list[str] = []

        fed, fed_prov = self.registry.get("US", inputs.tax_year)
        if fed_prov:
            provisional.append(f"US federal: using {fed.tax_year} tables for {inputs.tax_year}")
        if fed.self_employment is None:
            raise TaxError("Federal table lacks self-employment rules")

        net_profit = inputs.net_profit
        se = self_employment_tax(
            net_profit, fed.self_employment, status,
            w2_social_security_wages=inputs.w2_social_security_wages,
            w2_medicare_wages=inputs.w2_medicare_wages,
        )

        agi = net_profit - se.deductible_half + inputs.other_income
        std = fed.standard_deduction_for(status)
        taxable_before_qbi = max(0, agi - std)

        qbi = 0
        if inputs.apply_qbi and fed.qbi and net_profit > 0:
            qbi = qbi_deduction(net_profit - se.deductible_half, taxable_before_qbi, status, fed.qbi)
        taxable = max(0, taxable_before_qbi - qbi)
        brackets = fed.brackets_for(status)
        fed_tax = progressive_tax(taxable, brackets)

        state_taxable = state_tax = 0
        region = inputs.region.upper() if inputs.region else None
        if region:
            state, state_prov = self.registry.get(region, inputs.tax_year)
            if state_prov:
                provisional.append(f"{region}: using {state.tax_year} tables for {inputs.tax_year}")
            if state.kind == "none":
                notes.append(f"{region} has no broad-based income tax")
            else:
                state_taxable = max(0, agi - state.standard_deduction_for(status))
                state_tax = progressive_tax(state_taxable, state.brackets_for(status))
                for s in state.surcharges:
                    over = state_taxable - _lookup(s.threshold, status, f"{s.name} threshold")
                    state_tax += apply_bp(max(0, over), s.rate_bp)
            if state.notes:
                notes.append(f"{region}: {state.notes}")
        if fed.notes:
            notes.append(f"US: {fed.notes}")

        total = se.total + fed_tax + state_tax
        income_base = net_profit + inputs.other_income
        effective = _round_div(total * BP_SCALE, income_base) if income_base > 0 else 0
        balance = max(0, total - inputs.withholding)
        quarters = tuple(allocate(balance, [1, 1, 1, 1])) if balance else (0, 0, 0, 0)

        return TaxEstimate(
            tax_year=inputs.tax_year,
            filing_status=status,
            net_profit=net_profit,
            se=se,
            adjusted_gross_income=agi,
            standard_deduction=std,
            qbi_deduction=qbi,
            federal_taxable_income=taxable,
            federal_income_tax=fed_tax,
            federal_marginal_rate_bp=marginal_rate_bp(taxable, brackets),
            region=region,
            state_taxable_income=state_taxable,
            state_income_tax=state_tax,
            total_tax=total,
            effective_rate_bp=effective,
            balance_due=balance,
            quarterly_payments=quarters,   # type: ignore[arg-type]
            quarterly_due_dates=quarterly_due_dates(inputs.tax_year),
            provisional=tuple(provisional),
            notes=tuple(notes),
        )


def estimate_flat(net_profit_minor: int, income_tax_rate_bp: int, self_employment_rate_bp: int) -> dict[str, int]:
    """Fallback for jurisdictions without tables: flat rates from a tax_profiles row."""
    income = apply_bp(max(0, net_profit_minor), income_tax_rate_bp)
    se = apply_bp(max(0, net_profit_minor), self_employment_rate_bp)
    return {"income_tax": income, "self_employment_tax": se, "total": income + se}


# ---------------------------------------------------------------------------
# Database aggregation (Schedule C style)
# ---------------------------------------------------------------------------


@dataclass
class YearSummary:
    tax_year: int
    base_currency: str
    gross_receipts: int = 0
    platform_fees: int = 0
    deductible_expenses: int = 0
    business_meters: int = 0
    mileage_deduction: int = 0
    vehicle_method: str = "standard_mileage"
    excluded_unconverted: int = 0
    warnings: list[str] = field(default_factory=list)


def summarize_year(
    db: Any,
    tax_year: int,
    *,
    base_currency: str,
    vehicle_method: Literal["standard_mileage", "actual"] = "standard_mileage",
    table: TaxTable | None = None,
    mileage_override: tuple[int, str] | None = None,
    timezone: str = "UTC",
) -> YearSummary:
    """Aggregate one tax year from the ledger in base-currency minor units.

    Trips are dated in the user's `timezone` (the mileage rate and tax year
    depend on the local calendar date). Income and expense rows still use
    their stored UTC timestamps for the year boundary.
    """
    if vehicle_method not in ("standard_mileage", "actual"):
        raise TaxError("vehicle_method must be 'standard_mileage' or 'actual'")
    base_currency = base_currency.upper()
    start, end = f"{tax_year}-01-01", f"{tax_year + 1}-01-01"
    s = YearSummary(tax_year, base_currency, vehicle_method=vehicle_method)

    with db.connection() as conn:
        for row in conn.execute(
            "SELECT gross_amount_minor, platform_fee_minor, currency, base_amount_minor FROM transactions "
            "WHERE deleted_at IS NULL AND occurred_at >= ? AND occurred_at < ?", (start, end)
        ):
            gross, fee = row["gross_amount_minor"], row["platform_fee_minor"]
            if row["currency"] == base_currency:
                s.gross_receipts += gross
                s.platform_fees += fee
            elif row["base_amount_minor"] is not None and gross:
                s.gross_receipts += row["base_amount_minor"]
                s.platform_fees += _round_div(fee * row["base_amount_minor"], gross)
            else:
                s.excluded_unconverted += 1

        for row in conn.execute(
            "SELECT amount_minor, currency, base_amount_minor, category, business_use_bp FROM expenses "
            "WHERE deleted_at IS NULL AND is_tax_deductible = 1 AND occurred_at >= ? AND occurred_at < ?",
            (start, end),
        ):
            if vehicle_method == "standard_mileage" and row["category"] in VEHICLE_OPERATING_CATEGORIES:
                continue
            amount = row["amount_minor"] if row["currency"] == base_currency else row["base_amount_minor"]
            if amount is None:
                s.excluded_unconverted += 1
                continue
            s.deductible_expenses += deductible_amount(amount, row["category"], row["business_use_bp"])

        meters_by_day: dict[date, int] = {}
        # Pad the UTC window by a day each side, then filter on the local date.
        for row in conn.execute(
            "SELECT started_at, distance_m FROM mileage_logs "
            "WHERE deleted_at IS NULL AND purpose = 'business' AND trip_state = 'completed' "
            "AND started_at >= ? AND started_at < ?",
            (f"{tax_year - 1}-12-31", f"{tax_year + 1}-01-02"),
        ):
            day = local_date(row["started_at"], timezone)
            if day.year == tax_year:
                meters_by_day[day] = meters_by_day.get(day, 0) + int(row["distance_m"])

    s.business_meters = sum(meters_by_day.values())
    if vehicle_method == "standard_mileage" and meters_by_day:
        if mileage_override:
            s.mileage_deduction = mileage_deduction(
                meters_by_day, table or TaxTable("US", tax_year, base_currency, "none"),
                override_rate_minor=mileage_override[0], override_unit=mileage_override[1],  # type: ignore[arg-type]
            )
        elif table is not None:
            s.mileage_deduction = mileage_deduction(meters_by_day, table)
        else:
            s.warnings.append("No mileage rate table; mileage deduction omitted")
    if s.excluded_unconverted:
        s.warnings.append(f"{s.excluded_unconverted} foreign-currency record(s) lack a base amount and were excluded")
    return s


def estimate_for_year(
    db: Any,
    tax_year: int,
    *,
    base_currency: str = "USD",
    estimator: TaxEstimator | None = None,
    filing_status: str | None = None,
    region: str | None = None,
    vehicle_method: Literal["standard_mileage", "actual"] = "standard_mileage",
    other_income: int = 0,
    w2_social_security_wages: int = 0,
    w2_medicare_wages: int = 0,
    withholding: int = 0,
    timezone: str = "UTC",
) -> tuple[TaxEstimate, YearSummary]:
    """End-to-end estimate from the ledger plus the year's default tax profile."""
    estimator = estimator or TaxEstimator()
    with db.connection() as conn:
        profile = conn.execute(
            "SELECT * FROM tax_profiles WHERE tax_year = ? AND deleted_at IS NULL "
            "ORDER BY is_default DESC, updated_at DESC LIMIT 1", (tax_year,)
        ).fetchone()

    status = filing_status or (profile["filing_status"] if profile else "single")
    if region is None and profile is not None and profile["region_code"]:
        region = profile["region_code"]
    if profile is not None and profile["country_code"] != "US":
        raise UnsupportedJurisdiction(
            f"No tables for country {profile['country_code']}; use estimate_flat() with the profile's rates"
        )

    fed, _ = estimator.registry.get("US", tax_year)
    override = None
    if profile is not None and profile["mileage_rate_minor"] is not None:
        override = (profile["mileage_rate_minor"], profile["mileage_rate_unit"])
    summary = summarize_year(
        db, tax_year, base_currency=base_currency, vehicle_method=vehicle_method, table=fed, mileage_override=override,
        timezone=timezone,
    )
    inputs = TaxInputs(
        tax_year=tax_year,
        filing_status=status,
        region=region,
        gross_receipts=summary.gross_receipts,
        platform_fees=summary.platform_fees,
        deductible_expenses=summary.deductible_expenses,
        mileage_deduction=summary.mileage_deduction,
        other_income=other_income,
        w2_social_security_wages=w2_social_security_wages,
        w2_medicare_wages=w2_medicare_wages,
        withholding=withholding,
    )
    return estimator.estimate(inputs), summary
