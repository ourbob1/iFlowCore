"""
Route table and guard logic (framework-free so it can be unit-tested).

Routes
------
    /unlock      database key entry / first-run passphrase creation
    /            dashboard
    /finances    ingestion, ledger, tax set-asides
    /fleet       trips, vehicles, maintenance, auto-trip review
    /radar       demand radar
    /insights    personal intelligence: trends, peak hours, area heatmap
    /health      iFlow Health: coverage, premium withholding reserve, guild plans (Tier 3)
    /settings    preferences, sentry, feeds, tax profile
    /plans       tiers, license, upgrades (under Settings)

Phones (compact width) get a 5-item bottom bar; iFlow Health (heart) and
Settings (gear) move to the app bar and open as full pages with a back arrow.

While the database is locked every route resolves to /unlock (remembering
where the user was headed); unknown routes fall back to the dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True)
class RouteSpec:
    path: str
    title: str
    icon: str              # ft.Icons name
    selected_icon: str
    in_nav: bool = True
    in_compact_nav: bool = True        # False: reached from the app bar on phones instead of the bottom bar
    nav_parent: str | None = None      # pages outside the nav highlight (and go back to) this route


ROUTES: tuple[RouteSpec, ...] = (
    RouteSpec("/", "Dashboard", "DASHBOARD_OUTLINED", "DASHBOARD"),
    RouteSpec("/finances", "Finances", "ACCOUNT_BALANCE_WALLET_OUTLINED", "ACCOUNT_BALANCE_WALLET"),
    RouteSpec("/fleet", "Fleet", "DIRECTIONS_CAR_OUTLINED", "DIRECTIONS_CAR"),
    RouteSpec("/radar", "Radar", "RADAR_OUTLINED", "RADAR"),
    RouteSpec("/insights", "Insights", "INSIGHTS_OUTLINED", "INSIGHTS"),
    RouteSpec("/health", "iFlow Health", "FAVORITE_BORDER", "FAVORITE", in_compact_nav=False),
    RouteSpec("/settings", "Settings", "SETTINGS_OUTLINED", "SETTINGS", in_compact_nav=False),
    RouteSpec("/plans", "Plans", "WORKSPACE_PREMIUM_OUTLINED", "WORKSPACE_PREMIUM", in_nav=False,
              nav_parent="/settings"),
    RouteSpec("/unlock", "Unlock", "LOCK_OUTLINE", "LOCK", in_nav=False),
)
BY_PATH = {r.path: r for r in ROUTES}
NAV_ROUTES = tuple(r for r in ROUTES if r.in_nav)
COMPACT_NAV_ROUTES = tuple(r for r in NAV_ROUTES if r.in_compact_nav)
SETTINGS = "/settings"
UNLOCK = "/unlock"
HOME = "/"


@dataclass(frozen=True)
class Resolution:
    spec: RouteSpec
    params: dict[str, str]
    redirect_to: str | None = None       # set when the requested path differs from the shown one


def normalize(route: str | None) -> tuple[str, dict[str, str]]:
    parts = urlsplit(route or "/")
    path = "/" + parts.path.strip("/")
    params = {k: v[-1] for k, v in parse_qs(parts.query).items()}
    return path, params


def resolve(route: str | None, *, unlocked: bool) -> Resolution:
    path, params = normalize(route)
    if not unlocked:
        if path == UNLOCK:
            return Resolution(BY_PATH[UNLOCK], params)
        return Resolution(BY_PATH[UNLOCK], {"next": path}, redirect_to=UNLOCK)
    if path == UNLOCK:                                   # already unlocked
        target = params.get("next", HOME)
        target = target if target in BY_PATH and target != UNLOCK else HOME
        return Resolution(BY_PATH[target], {}, redirect_to=target)
    if path not in BY_PATH:
        return Resolution(BY_PATH[HOME], {}, redirect_to=HOME)
    return Resolution(BY_PATH[path], params)


def nav_routes(compact: bool = False) -> tuple[RouteSpec, ...]:
    return COMPACT_NAV_ROUTES if compact else NAV_ROUTES


def nav_index(path: str, *, compact: bool = False) -> int | None:
    for i, spec in enumerate(nav_routes(compact)):
        if spec.path == path:
            return i
    return None


def path_for_index(index: int, *, compact: bool = False) -> str:
    return nav_routes(compact)[index].path
