"""
Tiers and feature flags.

Three tiers, each including everything below it:

  Solo Core (free)          manual tracking, CSV statement import, mileage log, encrypted local storage,
                            plain CSV export, and every feature that shipped before tiers existed
                            (Insights, demand radar, tax estimates, AI tax Q&A, crash reports)
  iFlow Pro                 background motion sentry, VIN decoding & multi-vehicle fleet, Quicken /
                            QuickBooks / QIF exports, AI receipt scanning
  iFlow Health & Guild      Association Health Plan (AHP) pool tracking, fractional premium withholding

Gating rules (the "local-first promise"):
  * Tiers gate *actions*, never *data*. A lapsed license never hides, locks or
    deletes anything already recorded; it only stops new gated work (starting
    the sentry, decoding a VIN, an OCR scan, an advanced export format).
  * Plain CSV export of the ledger and mileage log is always free: users can
    always take their data with them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Tier(str, Enum):
    SOLO = "solo"
    PRO = "pro"
    GUILD = "guild"

    @property
    def rank(self) -> int:
        return _RANK[self]

    def includes(self, other: "Tier") -> bool:
        return self.rank >= other.rank

    @property
    def label(self) -> str:
        return TIER_INFO[self].name


_RANK = {Tier.SOLO: 0, Tier.PRO: 1, Tier.GUILD: 2}


@dataclass(frozen=True)
class TierInfo:
    name: str
    tagline: str


TIER_INFO: dict[Tier, TierInfo] = {
    Tier.SOLO: TierInfo("Solo Core", "Everything you need to track your gig business, free forever."),
    Tier.PRO: TierInfo("iFlow Pro", "Automatic trips, your fleet, accountant-ready exports and receipt scanning."),
    Tier.GUILD: TierInfo("iFlow Health & Guild", "Pro, plus health-plan pooling and premium set-asides."),
}


class Feature(str, Enum):
    # Solo Core
    MANUAL_TRACKING = "manual_tracking"
    CSV_IMPORT = "csv_import"
    MILEAGE_LOG = "mileage_log"
    LOCAL_STORAGE = "local_storage"
    BASIC_EXPORT = "basic_export"
    INSIGHTS = "insights"
    TAX_ESTIMATES = "tax_estimates"
    # iFlow Pro
    MOTION_SENTRY = "motion_sentry"
    VIN_DECODE = "vin_decode"
    FLEET = "fleet"
    ADVANCED_EXPORTS = "advanced_exports"
    RECEIPT_OCR = "receipt_ocr"
    # iFlow Health & Guild
    AHP_POOL = "ahp_pool"
    PREMIUM_WITHHOLDING = "premium_withholding"


@dataclass(frozen=True)
class FeatureInfo:
    tier: Tier
    label: str
    description: str


FEATURES: dict[Feature, FeatureInfo] = {
    Feature.MANUAL_TRACKING: FeatureInfo(Tier.SOLO, "Manual income & expense tracking",
                                         "Log earnings, expenses and receipts by hand."),
    Feature.CSV_IMPORT: FeatureInfo(Tier.SOLO, "CSV & PDF statement uploads",
                                    "Import platform statements with mapping profiles."),
    Feature.MILEAGE_LOG: FeatureInfo(Tier.SOLO, "Mileage log", "Log trips by hand; IRS mileage deduction included."),
    Feature.LOCAL_STORAGE: FeatureInfo(Tier.SOLO, "Encrypted local storage",
                                       "Your data stays on your device, encrypted."),
    Feature.BASIC_EXPORT: FeatureInfo(Tier.SOLO, "CSV export", "Ledger and mileage log as CSV, anytime."),
    Feature.INSIGHTS: FeatureInfo(Tier.SOLO, "Insights & demand radar",
                                  "Peak hours, trends and area heatmaps, computed on your device."),
    Feature.TAX_ESTIMATES: FeatureInfo(Tier.SOLO, "Tax estimates & set-aside alerts",
                                       "Quarterly estimates from your own numbers."),
    Feature.MOTION_SENTRY: FeatureInfo(Tier.PRO, "Automatic trip detection",
                                       "The motion sentry logs drives in the background."),
    Feature.VIN_DECODE: FeatureInfo(Tier.PRO, "VIN decoding", "Decode make, model, trim and equipment (NHTSA/DataOne)."),
    Feature.FLEET: FeatureInfo(Tier.PRO, "Fleet management", "Unlimited vehicles (Solo Core: one)."),
    Feature.ADVANCED_EXPORTS: FeatureInfo(Tier.PRO, "Quicken & QuickBooks exports",
                                          "QFX/OFX, QIF and QuickBooks CSV for your accountant."),
    Feature.RECEIPT_OCR: FeatureInfo(Tier.PRO, "AI receipt scanning", "Read receipts with your own AI server."),
    Feature.AHP_POOL: FeatureInfo(Tier.GUILD, "Association Health Plan tracking",
                                  "Track your AHP pool, premiums and coverage months."),
    Feature.PREMIUM_WITHHOLDING: FeatureInfo(Tier.GUILD, "Premium withholding",
                                             "Set aside a slice of each payout for your health premium."),
}

FREE_VEHICLE_LIMIT = 1


def features_for(tier: Tier) -> list[Feature]:
    return [f for f, info in FEATURES.items() if tier.includes(info.tier)]


def new_in(tier: Tier) -> list[Feature]:
    """Features a tier adds over the one below it (for comparison checklists)."""
    return [f for f, info in FEATURES.items() if info.tier is tier]
