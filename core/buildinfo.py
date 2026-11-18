"""
Build metadata baked into packaged apps (never secrets).

The packaging scripts write `_build_info.json` next to `main.py` in the
staged app:

    {"channel": "production", "version": "0.9.0", "target": "linux",
     "built_at": "2026-09-27T12:00:00Z", "backend": "pyinstaller"}

Why it matters: a source checkout runs as *development* when IFLOW_ENV is
unset (plaintext dev databases allowed, simulated licenses allowed). A
packaged app must not: its default environment comes from the channel here,
so a shipped desktop build requires SQLCipher and refuses simulation tokens
even if the user never sets an environment variable. An explicit
IFLOW_ENV still wins (for QA builds).

The file holds no keys, tokens or user data, and the loader ignores anything
it doesn't recognize.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

FILENAME = "_build_info.json"
CHANNELS = {"production": "production", "beta": "production", "qa": "test", "dev": "development"}


@dataclass(frozen=True)
class BuildInfo:
    channel: str = "source"
    version: str | None = None
    target: str | None = None
    built_at: str | None = None
    backend: str | None = None

    @property
    def packaged(self) -> bool:
        return self.channel != "source"

    @property
    def default_environment(self) -> str | None:
        """Environment a packaged build runs in when IFLOW_ENV is unset (None for source checkouts)."""
        return CHANNELS.get(self.channel)


def candidate_dirs() -> list[Path]:
    dirs = []
    frozen_root = getattr(sys, "_MEIPASS", None)           # PyInstaller one-file / one-dir
    if frozen_root:
        dirs.append(Path(frozen_root))
    dirs.append(Path(__file__).resolve().parents[1])        # app root (flet build / source)
    return dirs


def load(dirs: list[Path] | None = None) -> BuildInfo:
    for d in dirs if dirs is not None else candidate_dirs():
        path = d / FILENAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict) or raw.get("channel") not in CHANNELS:
            continue
        return BuildInfo(**{k: str(raw[k]) for k in ("channel", "version", "target", "built_at", "backend")
                            if raw.get(k) is not None})
    return BuildInfo()
