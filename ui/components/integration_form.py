"""
The connection form for a platform integration, rendered from its schema's `fields`.

Used in two places so they can't drift apart: the user's connection wizard (Finances) and the
Integration Studio's live preview in the Admin Console. Returns the controls plus a `read()` that
collects the current values (secret fields included; callers decide where those go).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

import flet as ft

from core.integration.schema import IntegrationSchema


@dataclass
class RenderedForm:
    controls: list[ft.Control]
    read: Callable[[], dict[str, str]]


def build_form(schema: IntegrationSchema, values: Mapping[str, str], *, key_prefix: str,
               errors: Mapping[str, str] | None = None, secrets_saved: frozenset[str] = frozenset()) -> RenderedForm:
    errors = errors or {}
    inputs: dict[str, Any] = {}
    controls: list[ft.Control] = []
    for f in schema.fields:
        key = f"{key_prefix}-{f.key}"
        label = f.label + (" *" if f.required else "")
        hint = f.placeholder or None
        err = errors.get(f.key)
        value = str(values.get(f.key, "") or "")
        if f.type == "checkbox":
            control: Any = ft.Checkbox(label=f.label, value=value.lower() == "true", key=key)
        elif f.type == "select":
            control = ft.Dropdown(label=label, value=value or None, key=key, width=320,
                                  options=[ft.DropdownOption(key=v, text=t) for v, t in f.options], error_text=err)
        elif f.type == "secret":
            saved = f.key in secrets_saved
            control = ft.TextField(label=label, password=True, can_reveal_password=True, key=key, width=420,
                                   hint_text="saved on this device; type to replace" if saved else hint,
                                   error=err)
        else:
            control = ft.TextField(label=label, value=value, key=key, width=220 if f.type in ("date", "number") else 420,
                                   hint_text=hint or ("YYYY-MM-DD" if f.type == "date" else None), error=err,
                                   keyboard_type=ft.KeyboardType.NUMBER if f.type == "number" else None)
        inputs[f.key] = control
        controls.append(control)
        if f.help:
            controls.append(ft.Text(f.help, size=12, opacity=0.75, key=f"{key}-help"))

    def read() -> dict[str, str]:
        out: dict[str, str] = {}
        for f in schema.fields:
            c = inputs[f.key]
            if f.type == "checkbox":
                out[f.key] = "true" if c.value else "false"
            else:
                out[f.key] = str(c.value or "").strip()
        return out

    return RenderedForm(controls, read)
