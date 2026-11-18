"""Reusable building blocks: cards, KPI tiles, meters, status banners, empty states."""

from __future__ import annotations

from typing import Any, Callable

import flet as ft

from ui.layout import LayoutSpec
from ui.theme import RADIUS, STATUS_COLOR, STATUS_ICON, STATUS_LABEL, Palette, Status


def text(value: str, pal: Palette, *, size: int = 14, weight: ft.FontWeight | None = None,
         role: str = "primary", selectable: bool = False) -> ft.Text:
    color = {"primary": pal.text_primary, "secondary": pal.text_secondary, "muted": pal.text_muted}[role]
    return ft.Text(value, size=size, weight=weight, color=color, selectable=selectable)


def card(*controls: ft.Control, pal: Palette, title: str | None = None, col: dict | None = None,
         actions: list[ft.Control] | None = None, padding: int = 16, key: str | None = None) -> ft.Container:
    header: list[ft.Control] = []
    if title or actions:
        # wrap=True lets actions drop under the title on narrow phones instead of overflowing.
        header = [ft.Row(
            [text(title or "", pal, size=16, weight=ft.FontWeight.W_600), ft.Row(actions or [], spacing=4)],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            wrap=True, run_spacing=8,
        )]
    return ft.Container(
        content=ft.Column([*header, *controls], spacing=12, tight=True),
        padding=ft.Padding.all(padding), bgcolor=pal.surface_raised, border_radius=ft.BorderRadius.all(RADIUS),
        border=ft.Border.all(1, pal.grid), col=col, key=key,
    )


def kpi_tile(label: str, value: str, pal: Palette, *, sub: str | None = None, col: dict | None = None,
             key: str | None = None) -> ft.Container:
    """Hero number tile — a number, not a chart."""
    parts: list[ft.Control] = [
        text(label, pal, size=13, role="secondary"),
        text(value, pal, size=26, weight=ft.FontWeight.W_700, selectable=True),
    ]
    if sub:
        parts.append(text(sub, pal, size=12, role="muted"))
    return card(*parts, pal=pal, col=col, key=key)


def meter(label: str, value_bp: int, caption: str, pal: Palette, *, color: str, key: str | None = None) -> ft.Column:
    """Horizontal gauge for a 0–100% quantity (clamped), with its value in text."""
    fraction = max(0, min(10_000, value_bp)) / 10_000    # UI geometry only
    return ft.Column([
        ft.Row([text(label, pal, size=13, role="secondary"), text(caption, pal, size=13, weight=ft.FontWeight.W_600)],
               alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.ProgressBar(value=fraction, color=color, bgcolor=pal.grid, bar_height=8,
                       border_radius=ft.BorderRadius.all(4), semantics_label=f"{label}: {caption}"),
    ], spacing=6, key=key)


def status_banner(level: Status, title: str, message: str, pal: Palette, *, trailing: ft.Control | None = None,
                  key: str | None = None, label: str | None = None) -> ft.Container:
    """Status is never color-alone: icon + label + text. `label` replaces the deadline-style prefix
    ("On track", "Due soon"…); "" shows the title alone (operational messages: the title says it)."""
    label = STATUS_LABEL[level] if label is None else label
    color = STATUS_COLOR[level]
    return ft.Container(
        content=ft.Row([
            ft.Icon(getattr(ft.Icons, STATUS_ICON[level]), color=color, size=28),
            ft.Column([
                text(f"{label} · {title}" if label else title, pal, size=14, weight=ft.FontWeight.W_600),
                text(message, pal, size=13, role="secondary"),
            ], spacing=2, expand=True, tight=True),
            *([trailing] if trailing else []),
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12),
        padding=ft.Padding.all(14), border_radius=ft.BorderRadius.all(RADIUS),
        border=ft.Border.only(left=ft.BorderSide(4, color)), bgcolor=pal.surface_raised, key=key,
    )


def notice_banner(level: Status, title: str, message: str, pal: Palette, **kw: Any) -> ft.Container:
    """A status banner for action results and account/console messages: icon + title + text."""
    return status_banner(level, title, message, pal, label="", **kw)


def empty_state(icon: str, title: str, message: str, pal: Palette, *,
                action: tuple[str, Callable] | None = None, key: str | None = None) -> ft.Container:
    children: list[ft.Control] = [
        ft.Icon(getattr(ft.Icons, icon), size=40, color=pal.text_muted),
        text(title, pal, size=16, weight=ft.FontWeight.W_600),
        text(message, pal, size=13, role="secondary"),
    ]
    if action:
        children.append(ft.FilledButton(action[0], on_click=action[1]))
    return ft.Container(
        content=ft.Column(children, horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8, tight=True),
        padding=ft.Padding.all(24), alignment=ft.Alignment.CENTER, key=key,
    )


def primary_button(label: str, on_click: Callable, layout: LayoutSpec, *, icon: str | None = None,
                   key: str | None = None, disabled: bool = False) -> ft.FilledButton:
    return ft.FilledButton(label, icon=getattr(ft.Icons, icon) if icon else None, on_click=on_click,
                           height=layout.min_target, key=key, disabled=disabled)
