"""What every view receives from the shell."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import flet as ft

from ui.layout import LayoutSpec
from ui.state import AppState
from ui.theme import Palette


@dataclass
class ViewContext:
    state: AppState
    layout: LayoutSpec
    pal: Palette
    navigate: Callable[[str], None]
    refresh: Callable[[], None]
    notify: Callable[[str], None]
    show_dialog: Callable[[ft.DialogControl], None]
    close_dialog: Callable[[], None]
    params: dict[str, str] = field(default_factory=dict)
    services: dict[str, Any] = field(default_factory=dict)   # file_picker, motion bridge, radar factory…

    @property
    def db(self):
        if self.state.db is None:
            raise RuntimeError("Database is locked")
        return self.state.db


def page_column(ctx: ViewContext, *controls: ft.Control, key: str) -> ft.Column:
    """Scrollable page body with layout-dependent padding."""
    return ft.Column(
        [ft.Container(ft.Column(list(controls), spacing=16, tight=True), padding=ft.Padding.all(ctx.layout.padding))],
        scroll=ft.ScrollMode.AUTO, expand=True, key=key,
    )
