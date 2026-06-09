"""Cinemana (Shabakaty) API client.

All endpoints are open GET requests under
``https://cinemana.shabakaty.com/api/android/`` and need no authentication.

The signed video/subtitle URLs returned by ``transcoded_files`` /
``translation_files`` expire after a few hours, so callers must fetch them
*fresh* at the moment a download starts or resumes — never cache them up front.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlparse

import requests

from .model import Episode, QualityVariant, SubtitleFile, VideoInfo

BASE_URL = "https://cinemana.shabakaty.com/api/android"
DEFAULT_TIMEOUT = (10, 30)  # (connect, read)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 CinemanaDownloader/1.0"
)

_ID_RE = re.compile(r"(\d{3,})")


class CinemanaError(Exception):
    """Raised for API/network failures or unparseable input."""


def parse_id(raw: str) -> str:
    """Extract a numeric video id from a bare id or a full Cinemana URL.

    The id lives in the URL *path* (``/video/ar/1200938``), never the query
    string, so we only inspect the path to avoid matching numbers in params.
    """
    if raw is None:
        raise CinemanaError("لم يتم إدخال رابط أو معرّف.")
    text = str(raw).strip()
    if not text:
        raise CinemanaError("لم يتم إدخال رابط أو معرّف.")
    if text.isdigit():
        return text
    parsed = urlparse(text if "//" in text else "https://" + text)
    match = _ID_RE.search(parsed.path or "")
    if not match:
        # Fall back to scanning the whole string as a last resort.
        match = _ID_RE.search(text)
    if not match:
        raise CinemanaError(f"تعذّر استخراج معرّف الفيديو من: {raw}")
    return match.group(1)


class CinemanaAPI:
    """Thin client over the Cinemana Android API with a reused session."""

    def __init__(self, session: requests.Session | None = None, timeout=DEFAULT_TIMEOUT,
                 retries: int = 3):
        self.timeout = timeout
        self.retries = retries
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def _get_json(self, path: str):
        """GET a JSON endpoint, retrying transient failures.

        The Cinemana servers occasionally return an empty/garbage body, and the
        user's connection can be flaky, so we retry both network errors and JSON
        decode errors a few times before giving up.
        """
        url = f"{BASE_URL}/{path.lstrip('/')}"
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                if attempt < self.retries:
                    time.sleep(min(5.0, 1.5 * attempt))
        raise CinemanaError(f"فشل الاتصال بالخادم ({path}): {last_exc}") from last_exc

    # -- metadata -------------------------------------------------------------

    def video_info(self, video_id: str) -> VideoInfo:
        """Full metadata for one video (``allVideoInfo``)."""
        data = self._get_json(f"allVideoInfo/id/{video_id}")
        if isinstance(data, list):  # some deployments wrap it in a list
            data = data[0] if data else {}
        if not isinstance(data, dict):
            raise CinemanaError("بيانات الفيديو غير متوقعة من الخادم.")
        return VideoInfo.from_api(data)

    def video_season(self, video_id: str) -> list[Episode]:
        """All episodes across all seasons, sorted by (season, episode)."""
        data = self._get_json(f"videoSeason/id/{video_id}")
        if not isinstance(data, list):
            return []
        episodes = [Episode.from_season_entry(e) for e in data if isinstance(e, dict)]
        episodes.sort(key=lambda e: e.sort_key)
        return episodes

    # -- signed, expiring resources (fetch fresh per download) ----------------

    def transcoded_files(self, video_id: str) -> list[QualityVariant]:
        """Fresh list of quality variants with signed (expiring) URLs."""
        data = self._get_json(f"transcoddedFiles/id/{video_id}")
        if not isinstance(data, list):
            return []
        variants = [QualityVariant.from_api(e) for e in data if isinstance(e, dict)]
        return [v for v in variants if v.video_url]

    def translation_files(self, video_id: str) -> list[SubtitleFile]:
        """Fresh subtitle list. ``translationFiles`` returns an *object* whose
        ``translations`` field holds the array; fall back to ``allVideoInfo``
        (same array) if that call fails or comes back empty."""
        entries = []
        try:
            data = self._get_json(f"translationFiles/id/{video_id}")
            if isinstance(data, dict):
                entries = data.get("translations") or []
            elif isinstance(data, list):
                entries = data
        except CinemanaError:
            entries = []
        if not entries:
            try:
                info = self._get_json(f"allVideoInfo/id/{video_id}")
                if isinstance(info, dict):
                    entries = info.get("translations") or []
            except CinemanaError:
                entries = []
        subs = [SubtitleFile.from_api(e) for e in entries if isinstance(e, dict)]
        return [s for s in subs if s.is_valid]
