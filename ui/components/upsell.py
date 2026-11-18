"""Locked-feature prompts: say plainly what the feature is, which plan has it, and that data is safe."""

from __future__ import annotations

from typing import Callable

import flet as ft

from core.licensing import FEATURES, Feature
from ui.components.common import card, text
from ui.theme import Palette


def plan_link(feature: Feature) -> str:
    return f"/plans?focus={FEATURES[feature].tier.value}"


def locked_row(feature: Feature, pal: Palette, navigate: Callable[[str], None], *, key: str | None = None) -> ft.Row:
    info = FEATURES[feature]
    return ft.Row([
        ft.Icon(ft.Icons.LOCK_OUTLINE, size=18, color=pal.text_secondary),
        ft.Container(text(f"{info.label} is part of {info.tier.label}.", pal, size=13, role="secondary"), expand=True),
        ft.TextButton("See plans", on_click=lambda e: navigate(plan_link(feature)), key=f"{key}-plans" if key else None),
    ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER, key=key)


def locked_card(feature: Feature, pal: Palette, navigate: Callable[[str], None], *, title: str,
                key: str | None = None, extra: str | None = None) -> ft.Container:
    info = FEATURES[feature]
    body: list[ft.Control] = [text(info.description, pal, size=13, role="secondary")]
    if extra:
        body.append(text(extra, pal, size=12, role="muted"))
    body.append(locked_row(feature, pal, navigate, key=f"{key}-lock" if key else None))
    return card(*body, pal=pal, title=title, key=key)


def locked_dialog(feature: Feature, navigate: Callable[[str], None], close: Callable[[], None], *,
                  detail: str | None = None) -> ft.AlertDialog:
    info = FEATURES[feature]

    def go(e: object) -> None:
        close()
        navigate(plan_link(feature))

    return ft.AlertDialog(
        modal=True, key="locked-dialog",
        title=ft.Text(f"{info.label} needs {info.tier.label}"),
        content=ft.Text(" ".join(x for x in (detail, info.description,
                                             "Everything you've already recorded stays available.") if x)),
        actions=[ft.TextButton("Not now", on_click=lambda e: close()),
                 ft.FilledButton("See plans", on_click=go, key="locked-see-plans")],
    )
