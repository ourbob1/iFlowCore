"""
Runtime platform detection.

Order of evidence (first match wins):

  1. `IFLOW_PLATFORM`  — explicit override for tests/CI (validated)
  2. `FLET_PLATFORM`      — set by Flet's embedded Python runtime in packaged
                            apps ("android", "ios", "macos", "windows", "linux")
  3. `sys.platform`       — "android" / "ios" on CPython ≥ 3.13 (PEP 738/730);
                            "emscripten" in the browser (Pyodide)
  4. `sys.getandroidapilevel` — present on every CPython built for Android
  5. desktop OS from `sys.platform`

Packaged Flet apps on older interpreters report sys.platform "linux" (Android)
or "darwin" (iOS), which is why the Flet variable is checked before it.
"""

from __future__ import annotations

import os
import sys
from typing import Literal, Mapping

RuntimePlatform = Literal["android", "ios", "windows", "macos", "linux", "web"]
PLATFORMS: tuple[str, ...] = ("android", "ios", "windows", "macos", "linux", "web")
MOBILE: frozenset[str] = frozenset({"android", "ios"})
DESKTOP: frozenset[str] = frozenset({"windows", "macos", "linux"})

OVERRIDE_ENV = "IFLOW_PLATFORM"
FLET_ENV = "FLET_PLATFORM"

_ALIASES = {"darwin": "macos", "win32": "windows", "cygwin": "windows", "emscripten": "web", "wasi": "web"}


class PlatformError(ValueError):
    pass


def _normalize(value: str) -> str:
    v = value.strip().lower()
    return _ALIASES.get(v, v)


def detect_platform(
    env: Mapping[str, str] | None = None,
    *,
    sys_platform: str | None = None,
    android_api: bool | None = None,
) -> RuntimePlatform:
    env = os.environ if env is None else env

    override = env.get(OVERRIDE_ENV, "").strip()
    if override:
        value = _normalize(override)
        if value not in PLATFORMS:
            raise PlatformError(f"{OVERRIDE_ENV}={override!r} is not one of {PLATFORMS}")
        return value  # type: ignore[return-value]

    flet = env.get(FLET_ENV, "").strip()
    if flet:
        value = _normalize(flet)
        if value in PLATFORMS:
            return value  # type: ignore[return-value]

    raw = _normalize(sys_platform if sys_platform is not None else sys.platform)
    if raw in ("android", "ios", "web"):
        return raw  # type: ignore[return-value]
    if android_api if android_api is not None else hasattr(sys, "getandroidapilevel"):
        return "android"
    if raw.startswith("linux"):
        return "linux"
    if raw in ("windows", "macos"):
        return raw  # type: ignore[return-value]
    return "linux"


def is_mobile(platform: str) -> bool:
    return platform in MOBILE


def is_desktop(platform: str) -> bool:
    return platform in DESKTOP
