"""
Scrub personal and financial data out of error reports.

Error messages are free text and routinely carry things we must never ship
off the device: file paths with the user's name, feed URLs with private
tokens, vendor amounts, coordinates, VINs, API keys. Everything that leaves
`ErrorReporter` passes through `scrub_text`; stack frames keep only file,
line and function (never local variables).

The rules are deliberately greedy: a redacted digit in a crash report costs
nothing, a leaked account number is a breach.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable

MAX_MESSAGE_CHARS = 500
APP_ROOT = Path(__file__).resolve().parents[2]

_KEY_NAMES = r"(?:api[_-]?key|token|secret|password|passphrase|pass|pwd|auth(?:orization)?|bearer|key|client[_-]?id|auth[_-]?code)"

# Order matters: whole URLs and credentials first, then structured ids, then numbers.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(?:https?|webcal|wss?|ftp)://([^/\s:'\"?#]+)[^\s'\"<>]*"), r"<url:\1>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
    (re.compile(rf"(?i)\b({_KEY_NAMES})\b(\s*[=:]\s*)(['\"]?)[^\s'\",;&]+\3"), r"\1\2<redacted>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"\b[0-9a-fA-F]{24,}\b"), "<hex>"),
    (re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b"), "<vin>"),
    (re.compile(r"\b(?=[A-Za-z0-9+/_-]*\d)[A-Za-z0-9+/_-]{32,}={0,2}"), "<secret>"),
    (re.compile(r"-?\d{1,3}\.\d{4,}"), "<coord>"),
    (re.compile(r"[$€£¥₹]\s?-?[\d,]+(?:\.\d+)?|-?[\d,]+(?:\.\d+)?\s?(?:USD|EUR|GBP|CAD|AUD|MXN|JPY)\b"), "<amount>"),
    (re.compile(r"\b\d[\d -]{4,}\d\b"), "<digits>"),
)


def _path_rules() -> list[tuple[str, str]]:
    """Longest-first literal prefixes to collapse: app root, site-packages, home."""
    rules: list[tuple[str, str]] = [(str(APP_ROOT), "<app>")]
    for p in {*sys.path}:
        if p and ("site-packages" in p or "dist-packages" in p):
            rules.append((p, "<lib>"))
    rules.append((sys.prefix, "<python>"))
    rules.append((sys.base_prefix, "<python>"))
    try:
        rules.append((str(Path.home()), "~"))
    except RuntimeError:            # no home on some mobile sandboxes
        pass
    return sorted({r for r in rules if r[0] and len(r[0]) > 1}, key=lambda r: -len(r[0]))


_PATHS = _path_rules()
# Anything still looking like a user-specific absolute path after the prefixes above.
_USER_PATH = re.compile(r"(?i)(?:[a-z]:\\\\?|/)(?:users|home|private/var/mobile|data/user/\d+)[\\/][^\s'\":]+")


def scrub_text(value: object, *, extra_paths: Iterable[str | Path] = (), limit: int = MAX_MESSAGE_CHARS) -> str:
    """Redact personal data from free text and cap its length."""
    text = str(value)
    for raw in sorted((str(p) for p in extra_paths if p), key=len, reverse=True):
        text = text.replace(raw, "<data>")
    for prefix, repl in _PATHS:
        text = text.replace(prefix, repl)
    text = _USER_PATH.sub("<path>", text)
    for rx, repl in _RULES:
        text = rx.sub(repl, text)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def scrub_filename(filename: str) -> tuple[str, bool]:
    """Stack-frame file → (display path, in_app). Never an absolute user path."""
    try:
        resolved = Path(filename).resolve()
    except (OSError, RuntimeError, ValueError):
        return "<unknown>", False
    parts = resolved.parts
    for marker in ("site-packages", "dist-packages"):
        if marker in parts:
            return "/".join(parts[parts.index(marker) + 1:]), False
    try:
        return resolved.relative_to(APP_ROOT).as_posix(), True
    except ValueError:
        pass
    if "lib" in parts:                  # stdlib: python3.x/<module>
        idx = len(parts) - 1 - parts[::-1].index("lib")
        return "/".join(parts[idx + 1:]), False
    return resolved.name, False
