"""Windows-safe filename sanitization and output-path building.

Produces a Plex-friendly layout::

    <Series Title>/Season 01/S01E01 - <Episode Title>.mp4
    <Series Title>/Season 01/S01E01 - <Episode Title>.ar.vtt
    <Movie Title> (2024).mp4            # movies: no season folder
"""

from __future__ import annotations

from pathlib import Path

_ILLEGAL = '<>:"/\\|?*'
_ILLEGAL_TABLE = {ord(c): " " for c in _ILLEGAL}
_ILLEGAL_TABLE.update({c: " " for c in range(0, 32)})  # control chars

_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize(name: str, maxlen: int = 150) -> str:
    """Make ``name`` safe to use as a single Windows path component."""
    if not name:
        return "untitled"
    cleaned = str(name).translate(_ILLEGAL_TABLE)
    cleaned = " ".join(cleaned.split())  # collapse whitespace runs
    cleaned = cleaned.strip(" .")        # Windows forbids trailing dot/space
    if not cleaned:
        return "untitled"
    if cleaned.upper() in _RESERVED or cleaned.split(".")[0].upper() in _RESERVED:
        cleaned = "_" + cleaned
    if len(cleaned) > maxlen:
        cleaned = cleaned[:maxlen].strip(" .") or "untitled"
    return cleaned


def episode_stem(season: int, episode: int, title: str, year: str = "", is_movie: bool = False) -> str:
    """Build the file basename (without extension) for an episode or movie."""
    if is_movie:
        base = sanitize(title)
        return f"{base} ({year})" if year else base
    label = f"S{season:02d}E{episode:02d}"
    title_part = sanitize(title)
    return f"{label} - {title_part}" if title_part and title_part != "untitled" else label


def build_paths(root: Path, series_title: str, season: int, stem: str, is_movie: bool) -> dict:
    """Return the destination paths for a given episode/movie.

    Keys: ``base_dir``, ``video`` (.mp4), ``part`` (.mp4.part), ``stem_path``
    (path without extension, used to build sibling subtitle names).
    """
    series_dir = Path(root) / sanitize(series_title)
    base_dir = series_dir if is_movie else series_dir / f"Season {season:02d}"
    stem_path = base_dir / stem
    video = base_dir / f"{stem}.mp4"
    return {
        "base_dir": base_dir,
        "video": video,
        "part": base_dir / f"{stem}.mp4.part",
        "stem_path": stem_path,
    }


def subtitle_path(stem_path: Path, lang: str, ext: str, used: set[str]) -> Path:
    """Build a unique sibling subtitle path like ``<stem>.ar.vtt``.

    Duplicate (lang, ext) pairs get a numeric suffix: ``<stem>.ar.2.vtt``.
    ``used`` tracks the relative names already assigned for this episode.
    """
    lang = sanitize(lang or "und", 12).replace(" ", "")
    ext = sanitize(ext or "srt", 8).replace(" ", "").lstrip(".") or "srt"
    candidate = f".{lang}.{ext}"
    if candidate in used:
        i = 2
        while f".{lang}.{i}.{ext}" in used:
            i += 1
        candidate = f".{lang}.{i}.{ext}"
    used.add(candidate)
    return stem_path.with_name(stem_path.name + candidate)
