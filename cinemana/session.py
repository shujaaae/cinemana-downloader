"""Persistent *resume snapshot* of the last GUI session.

Stored as JSON at ``~/.cinemana/last_session.json``. This is deliberately kept
separate from ``settings.py``'s ``settings.json`` (global preferences such as the
UI language): the session is a "where was I?" snapshot that can be cleared or go
corrupt without ever touching preferences, and vice-versa. It is also distinct
from the per-download ``.cinemana_state.json`` manifest, which remains the
crash-safe source of truth for per-episode status and byte progress.

What is stored: the form inputs (url, dest, quality, concurrency, segments), the
user's episode selection, and the full fetched plan so the tree can be rebuilt
*offline* on the next launch. Per-episode status/percentage is NOT stored here --
it is re-read from the manifest at ``dest`` when the session is restored.

Like ``settings.py`` everything here is defensive: a missing or corrupt file
yields ``{}``/``None`` and a non-writable location is swallowed -- restoring a
session must never be able to crash the app.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .model import Episode
from .service import SeriesPlan

SESSION_DIR = Path.home() / ".cinemana"
SESSION_FILE = SESSION_DIR / "last_session.json"


# -- plan <-> dict serialization ----------------------------------------------

def episode_to_dict(ep: Episode) -> dict:
    """Serialize one Episode (plain dataclass) to a JSON-safe dict."""
    return asdict(ep)


def episode_from_dict(d: dict) -> "Episode | None":
    """Rebuild an Episode from a dict, coercing types. None if it has no nb."""
    if not isinstance(d, dict):
        return None
    nb = str(d.get("nb") or "").strip()
    if not nb:
        return None
    try:
        return Episode(
            nb=nb,
            season=int(d.get("season") or 0),
            episode=int(d.get("episode") or 0),
            title=str(d.get("title") or ""),
            year=str(d.get("year") or ""),
            is_movie=bool(d.get("is_movie", False)),
        )
    except (TypeError, ValueError):
        return None


def plan_to_dict(plan: SeriesPlan) -> dict:
    """Serialize a SeriesPlan to a JSON-safe dict (excludes signed URLs)."""
    return {
        "series_id": plan.series_id,
        "title": plan.title,
        "is_movie": plan.is_movie,
        "available_heights": list(plan.available_heights),
        "episodes": [episode_to_dict(ep) for ep in plan.episodes],
    }


def plan_from_dict(d: dict) -> "SeriesPlan | None":
    """Rebuild a SeriesPlan; None on any malformed/empty input."""
    if not isinstance(d, dict):
        return None
    series_id = str(d.get("series_id") or "").strip()
    raw_eps = d.get("episodes")
    if not series_id or not isinstance(raw_eps, list):
        return None
    episodes = [ep for ep in (episode_from_dict(e) for e in raw_eps) if ep]
    if not episodes:
        return None
    heights = d.get("available_heights")
    heights = [int(h) for h in heights] if isinstance(heights, list) else []
    return SeriesPlan(
        series_id=series_id,
        title=str(d.get("title") or ""),
        is_movie=bool(d.get("is_movie", False)),
        episodes=episodes,
        available_heights=heights,
    )


# -- load / save --------------------------------------------------------------

def load_session() -> dict:
    """Return the saved session dict, or ``{}`` on missing/corrupt/unreadable."""
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def save_session(values: dict) -> None:
    """Merge ``values`` into the saved session and persist. Never raises."""
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        merged = {**load_session(), **values}
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def clear_session() -> None:
    """Best-effort delete of the session file (e.g. a future "reset")."""
    try:
        SESSION_FILE.unlink(missing_ok=True)
    except OSError:
        pass
