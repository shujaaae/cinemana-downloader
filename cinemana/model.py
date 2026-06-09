"""Data structures for the Cinemana downloader.

All values coming from the Cinemana API are strings (e.g. "1", "1080p"), so the
factory helpers here coerce them into the right Python types and parse heights
from the irregular quality names (``m480``, ``mp4-4k``) by preferring the
``resolution`` field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MOVIE = 1
SERIES = 2


def _to_int(value: Any, default: int = 0) -> int:
    """Best-effort int coercion for the API's stringly-typed numbers."""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def parse_height(resolution: Any, name: Any = "") -> int:
    """Derive a numeric pixel height used for ranking/selecting qualities.

    The ``name`` field is unreliable ("m480", "mp4-4k"), so we prefer the
    ``resolution`` field ("1080p", "480p", "4k"). "4k"/"2160" map to 2160.
    Returns 0 when nothing parseable is found (sorts to the bottom).
    """
    for source in (resolution, name):
        if source is None:
            continue
        text = str(source).strip().lower()
        if not text:
            continue
        if "4k" in text:
            return 2160
        digits = re.search(r"(\d{3,4})", text)
        if digits:
            return int(digits.group(1))
    return 0


@dataclass
class QualityVariant:
    """One downloadable video quality (signed ``video_url`` expires)."""

    name: str
    height: int
    container: str
    video_url: str

    @classmethod
    def from_api(cls, entry: dict) -> "QualityVariant":
        return cls(
            name=str(entry.get("name") or entry.get("resolution") or "unknown"),
            height=parse_height(entry.get("resolution"), entry.get("name")),
            container=str(entry.get("container") or "mp4"),
            video_url=str(entry.get("videoUrl") or entry.get("url") or ""),
        )


@dataclass
class SubtitleFile:
    """One subtitle track (signed ``url`` expires)."""

    name: str
    lang: str
    ext: str
    url: str

    @classmethod
    def from_api(cls, entry: dict) -> "SubtitleFile":
        return cls(
            name=str(entry.get("name") or entry.get("type") or "sub"),
            lang=str(entry.get("type") or entry.get("name") or "und").lower(),
            ext=str(entry.get("extention") or entry.get("extension") or "srt").lower(),
            url=str(entry.get("file") or entry.get("url") or ""),
        )

    @property
    def is_valid(self) -> bool:
        return bool(self.url) and bool(self.ext)


@dataclass
class VideoInfo:
    """Metadata for a single video, from ``allVideoInfo/id/{id}``."""

    nb: str
    en_title: str
    ar_title: str
    year: str
    kind: int
    season: int
    episode: int
    root_series: str
    duration: float

    @classmethod
    def from_api(cls, data: dict) -> "VideoInfo":
        return cls(
            nb=str(data.get("nb") or ""),
            en_title=str(data.get("en_title") or "").strip(),
            ar_title=str(data.get("ar_title") or "").strip(),
            year=str(data.get("year") or "").strip(),
            kind=_to_int(data.get("kind"), MOVIE),
            season=_to_int(data.get("season"), 0),
            episode=_to_int(data.get("episodeNummer"), 0),
            root_series=str(data.get("rootSeries") or "0"),
            duration=float(data.get("duration") or 0) if str(data.get("duration") or "").replace(".", "", 1).isdigit() else 0.0,
        )

    @property
    def title(self) -> str:
        """Preferred display title (English first, Arabic fallback)."""
        return self.en_title or self.ar_title or f"video-{self.nb}"

    @property
    def is_movie(self) -> bool:
        return self.kind == MOVIE


@dataclass
class Episode:
    """One downloadable unit: a series episode, or a movie (season/episode 0)."""

    nb: str
    season: int
    episode: int
    title: str
    year: str
    is_movie: bool = False

    @classmethod
    def from_season_entry(cls, entry: dict) -> "Episode":
        en = str(entry.get("en_title") or "").strip()
        ar = str(entry.get("ar_title") or "").strip()
        return cls(
            nb=str(entry.get("nb") or ""),
            season=_to_int(entry.get("season"), 0),
            episode=_to_int(entry.get("episodeNummer"), 0),
            title=en or ar or f"video-{entry.get('nb')}",
            year=str(entry.get("year") or "").strip(),
            is_movie=False,
        )

    @classmethod
    def from_info(cls, info: VideoInfo) -> "Episode":
        return cls(
            nb=info.nb,
            season=info.season if not info.is_movie else 0,
            episode=info.episode if not info.is_movie else 0,
            title=info.title,
            year=info.year,
            is_movie=info.is_movie,
        )

    @property
    def sort_key(self) -> tuple[int, int]:
        return (self.season, self.episode)
