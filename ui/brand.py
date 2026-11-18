"""iFlow Core brand imagery (generated from images/ by scripts/make_brand_assets.py).

Images are referenced by path inside the assets directory, which Flet serves to the client.
ASSETS_DIR sits next to the code, so it resolves the same way from a source checkout, a
PyInstaller bundle (`_internal/assets`) and a `flet build` app.
"""

from __future__ import annotations

from pathlib import Path

import flet as ft

from ui.theme import DARK, Palette

ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
LOGO = "brand/logo.png"                # emblem + wordmark + tagline, dark lettering (light surfaces)
LOGO_DARK = "brand/logo_dark.png"      # same with light lettering (dark surfaces)
EMBLEM = "brand/emblem.png"
LOGO_ASPECT = 1113 / 788               # width / height of the lock-up
EMBLEM_ASPECT = 484 / 537

NAME = "iFlow Core"
TAGLINE = "Command center for gig workers"


def logo(pal: Palette, *, width: int = 280, key: str | None = "brand-logo") -> ft.Image:
    """Full lock-up, picking the lettering that contrasts with the current theme."""
    return ft.Image(src=LOGO_DARK if pal is DARK else LOGO, width=width, height=round(width / LOGO_ASPECT),
                    fit=ft.BoxFit.CONTAIN, semantics_label=f"{NAME}: {TAGLINE}", key=key)


def emblem(size: int = 40, *, key: str | None = "brand-emblem") -> ft.Image:
    return ft.Image(src=EMBLEM, width=round(size * EMBLEM_ASPECT), height=size, fit=ft.BoxFit.CONTAIN,
                    semantics_label=NAME, key=key)
