"""
Freemium tiers, feature flags and offline licenses (Phase 7).

    tiers.py    Tier / Feature definitions and what each tier includes
    pricing.py  integer price book, annual savings, proration quotes
    tokens.py   Ed25519-signed license tokens, verified with a bundled public key
    state.py    license state machine (free/active/grace/expired/invalid), entitlement checks

No license check ever contacts a server.
"""

from core.licensing.pricing import Period, Plan, PricingError, plan, plans
from core.licensing.state import FREE_STATE, FeatureLocked, LicenseManager, LicenseState, Status, evaluate
from core.licensing.tiers import FEATURES, FREE_VEHICLE_LIMIT, TIER_INFO, Feature, Tier
from core.licensing.tokens import LicenseClaims, LicenseError

__all__ = [
    "FEATURES", "FREE_STATE", "FREE_VEHICLE_LIMIT", "TIER_INFO", "Feature", "FeatureLocked", "LicenseClaims",
    "LicenseError", "LicenseManager", "LicenseState", "Period", "Plan", "PricingError", "Status", "Tier", "evaluate",
    "plan", "plans",
]
