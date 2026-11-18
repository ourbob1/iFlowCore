"""Helpers for component tests: a fake Flet page and control-tree queries."""

from __future__ import annotations

import dataclasses
from typing import Any, Iterator

import flet as ft

BaseControl = ft.BaseControl


class FakePage:
    """Just enough of ft.Page for the shell: no Flutter client needed."""

    def __init__(self, width: int = 1280, platform: str = "linux", route: str = "/") -> None:
        self.width = width
        self.platform = platform
        self.route = route
        self.views: list[Any] = []
        self.services: list[Any] = []
        self.theme_mode = None
        self.platform_brightness = ft.Brightness.LIGHT
        self.dialogs: list[Any] = []
        self.updates = 0
        self.title = ""
        self.theme = self.dark_theme = None

    def update(self) -> None:
        self.updates += 1

    def show_dialog(self, dialog: Any) -> None:
        dialog.open = True
        self.dialogs.append(dialog)

    def pop_dialog(self) -> None:
        """Real Flet semantics: close the topmost *open* overlay (SnackBars included)."""
        for dialog in reversed(self.dialogs):
            if getattr(dialog, "open", False):
                dialog.open = False
                self.dialogs.remove(dialog)
                return

    def closed_dialog(self, dialog: Any) -> None:
        if dialog in self.dialogs:
            self.dialogs.remove(dialog)

    def run_thread(self, fn: Any, *args: Any) -> None:
        fn(*args)

    def run_task(self, fn: Any, *args: Any, **kwargs: Any) -> None:
        pass


def walk(control: Any) -> Iterator[Any]:
    """Depth-first traversal over every control reachable from `control`."""
    stack = [control]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, BaseControl):
            yield node
            children: list[Any] = []
            for f in dataclasses.fields(node):
                if f.name.startswith("_") or f.name in ("parent", "page", "data"):
                    continue
                try:
                    value = getattr(node, f.name)
                except Exception:
                    continue
                if isinstance(value, BaseControl):
                    children.append(value)
                elif isinstance(value, (list, tuple)):
                    children.extend(v for v in value if isinstance(v, BaseControl))
            stack.extend(reversed(children))       # keep document order


def find_key(control: Any, key: str) -> Any:
    for node in walk(control):
        if getattr(node, "key", None) == key:
            return node
    return None


def texts(control: Any) -> list[str]:
    out = []
    for node in walk(control):
        if isinstance(node, ft.Text) and isinstance(node.value, str):
            out.append(node.value)
    return out


def of_type(control: Any, cls: type) -> list[Any]:
    return [n for n in walk(control) if isinstance(n, cls)]
