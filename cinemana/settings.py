"""Persistent app-wide preferences (currently just the UI language).

Stored as JSON at ``~/.cinemana/settings.json`` — the user's home directory is
chosen over the app folder because a future packaged ``.exe`` may live in a
read-only location (e.g. Program Files). This is independent of the per-download
``.cinemana_state.json`` manifest, which is a different file with a different
purpose.

Both functions are defensive: a missing or corrupt file yields the defaults, and
a non-writable location is swallowed silently — preferences must never be able to
crash the app.
"""

from __future__ import annotations

import json
from pathlib import Path

SETTINGS_DIR = Path.home() / ".cinemana"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"
DEFAULTS = {"language": "ar"}


def load_settings() -> dict:
    """Return saved settings merged over the defaults; defaults on any failure."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {**DEFAULTS, **data}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return dict(DEFAULTS)


def save_settings(values: dict) -> None:
    """Merge ``values`` into the saved settings and persist. Never raises."""
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        merged = {**load_settings(), **values}
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
