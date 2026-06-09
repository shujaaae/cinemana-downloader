"""Orchestration layer between the API/engine and the UI.

Keeps the GUI thin: it prepares a :class:`SeriesPlan` (title + ordered episode
list + available qualities) and runs a CONCURRENT download loop — up to
``concurrency`` episodes at once, each optionally split across ``segments``
parallel connections — emitting events through simple callbacks so the UI can
update widgets without knowing anything about HTTP, manifests, or resume logic.

Per-episode live control (pause / resume / cancel) is exposed through
thread-safe ``request_*`` methods that the UI thread may call directly; they
only flip :class:`threading.Event`s and nudge a work queue.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests
from requests.adapters import HTTPAdapter

from . import downloader, naming, segmented
from .api import CinemanaAPI, parse_id
from .i18n import t
from .manifest import (
    Manifest, STATUS_DONE, STATUS_DOWNLOADING, STATUS_ERROR, STATUS_PAUSED,
    STATUS_PENDING,
)
from .model import Episode, QualityVariant
from .rate import RateRegistry, eta_seconds

DEFAULT_HEIGHT = 1080
DEFAULT_CONCURRENCY = 3
DEFAULT_SEGMENTS = 4
MAX_CONNECTIONS = 32          # hard cap on concurrency * segments
RATE_TICK_S = 0.5             # how often the rate ticker emits speed/ETA events


def height_label(height: int) -> str:
    if height >= 2160:
        return "4K"
    return f"{height}p" if height else "؟"


@dataclass
class SeriesPlan:
    series_id: str
    title: str
    is_movie: bool
    episodes: list[Episode]
    available_heights: list[int] = field(default_factory=list)

    @property
    def default_height(self) -> int:
        if DEFAULT_HEIGHT in self.available_heights:
            return DEFAULT_HEIGHT
        return self.available_heights[0] if self.available_heights else DEFAULT_HEIGHT


@dataclass
class Events:
    """Optional UI callbacks. All are safe to leave as no-ops."""

    on_log: Callable[[str], None] = lambda msg: None
    on_status: Callable[[str, str, dict], None] = lambda nb, status, extra: None
    on_progress: Callable[[str, int, "int | None"], None] = lambda nb, done, total: None
    # Emitted ONCE per episode when segmentation is decided. seg_totals[k] = byte
    # size of block k. A single-element list means one block (single-connection or
    # unknown-size path); a block total of 0 means "indeterminate" (unknown total).
    on_segments: Callable[[str, "list[int]"], None] = lambda nb, seg_totals: None
    # Emitted as bytes arrive: absolute downloaded bytes for block k of episode nb.
    on_segment_progress: Callable[[str, int, int], None] = lambda nb, k, done_k: None
    # nb=None -> aggregate/global speed+eta; nb set -> that episode's.
    on_rate: Callable[["str | None", float, "float | None"], None] = lambda nb, speed, eta: None
    on_series_done: Callable[[dict], None] = lambda summary: None


class EpisodeControl:
    """Per-episode pause/cancel flags, set from the UI thread, read by workers."""

    def __init__(self):
        self._pause = threading.Event()
        self._cancel = threading.Event()

    def pause(self):
        self._pause.set()

    def resume(self):
        self._pause.clear()

    def cancel(self):
        self._cancel.set()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()


# Per-episode lifecycle states (UI-visible coordination).
_QUEUED, _DOWNLOADING, _PAUSED, _DONE, _ERROR, _CANCELLED = (
    "queued", "downloading", "paused", "done", "error", "cancelled")


class DownloadService:
    def __init__(self, root: Path, api: CinemanaAPI | None = None,
                 events: Events | None = None):
        self.root = Path(root)
        self.api = api or CinemanaAPI()
        self.events = events or Events()
        self.session = self.api.session

        # Run-scoped state (populated by run(), guarded by _lock).
        self._lock = threading.RLock()
        self.controls: dict[str, EpisodeControl] = {}
        self.rates = RateRegistry()
        self._progress: dict[str, tuple[int, "int | None"]] = {}
        self._ep_state: dict[str, str] = {}
        self._outstanding: set[str] = set()
        self._ready: "queue.Queue | None" = None
        self._stop_global: Callable[[], bool] = lambda: False
        self._abort = threading.Event()
        self._wake = threading.Event()        # nudges the run() supervisor
        self._manifest: Manifest | None = None
        self._plan: SeriesPlan | None = None

    # -- planning -------------------------------------------------------------

    def prepare(self, raw_input: str) -> SeriesPlan:
        """Resolve a pasted URL/id into an ordered plan + available qualities."""
        vid = parse_id(raw_input)
        self.events.on_log(t("log_fetching_info"))
        info = self.api.video_info(vid)
        if info.is_movie:
            episodes = [Episode.from_info(info)]
            series_id = info.nb
            title = info.title
            self.events.on_log(t("log_found_movie"))
        else:
            episodes = self.api.video_season(vid)
            if not episodes:
                episodes = [Episode.from_info(info)]
            # Stable series key: smallest episode id (same for any pasted episode).
            series_id = min((e.nb for e in episodes), key=lambda n: int(n) if n.isdigit() else n)
            title = info.title
            seasons = len({e.season for e in episodes if not e.is_movie})
            self.events.on_log(t("log_found_episodes", n=len(episodes), seasons=seasons))
        # Sample one episode's qualities to populate the dropdown.
        heights: list[int] = []
        try:
            variants = self.api.transcoded_files(episodes[0].nb)
            heights = sorted({v.height for v in variants if v.height}, reverse=True)
            if heights:
                self.events.on_log(
                    t("log_qualities", qualities=", ".join(height_label(h) for h in heights)))
        except Exception as exc:  # noqa: BLE001 - non-fatal, dropdown can default
            self.events.on_log(t("log_quality_fetch_failed", err=exc))
        return SeriesPlan(series_id, title, info.is_movie, episodes, heights)

    # -- live control (callable from the UI thread) ---------------------------

    def request_pause(self, nb: str) -> None:
        ctl = self.controls.get(nb)
        if not ctl:
            return
        with self._lock:
            if self._ep_state.get(nb) in (_QUEUED, _DOWNLOADING):
                ctl.pause()

    def request_resume(self, nb: str) -> None:
        ctl = self.controls.get(nb)
        if not ctl or self._ready is None:
            return
        with self._lock:
            st = self._ep_state.get(nb)
            if st == _PAUSED:
                ctl.resume()
                self._ep_state[nb] = _QUEUED
                self._ready.put(nb)
            elif st == _DOWNLOADING and ctl.paused:
                # Abort a pause that was requested but hasn't taken effect yet.
                ctl.resume()

    def request_cancel(self, nb: str) -> None:
        ctl = self.controls.get(nb)
        if not ctl:
            return
        ctl.cancel()
        with self._lock:
            st = self._ep_state.get(nb)
            if st == _PAUSED:
                # Idle episode won't be picked again -> finalize it here.
                self._ep_state[nb] = _CANCELLED
                self._outstanding.discard(nb)
                self._progress.pop(nb, None)
        if st == _PAUSED and self._manifest and self._plan:
            self._manifest.set_status(self._plan.series_id, nb, STATUS_PENDING, save=False)
            ep = next((e for e in self._plan.episodes if e.nb == nb), None)
            if ep:
                self._emit_status(ep, STATUS_PENDING, {})
            self._wake.set()

    def request_pause_all(self) -> None:
        """Pause every active episode (global Pause button)."""
        for nb in list(self.controls):   # snapshot keys: controls may mutate
            self.request_pause(nb)

    def request_resume_all(self) -> None:
        """Resume every paused episode (global Resume button). No-op when idle."""
        for nb in list(self.controls):
            self.request_resume(nb)

    # -- concurrent run -------------------------------------------------------

    def run(self, plan: SeriesPlan, target_height: int,
            selected_nbs: "set[str] | None" = None,
            should_stop: Callable[[], bool] | None = None,
            concurrency: int = DEFAULT_CONCURRENCY,
            segments: int = DEFAULT_SEGMENTS,
            subtitle_langs: str = "all") -> dict:
        """Download the selected episodes concurrently. Returns a summary dict."""
        self._stop_global = should_stop or (lambda: False)
        self._abort.clear()
        self._subtitle_langs = subtitle_langs
        concurrency = max(1, min(int(concurrency), 6))
        segments = max(1, min(int(segments), 32))
        if concurrency * segments > MAX_CONNECTIONS:
            segments = max(1, MAX_CONNECTIONS // concurrency)
            self.events.on_log(
                t("log_conn_capped", n=concurrency, m=segments, max=MAX_CONNECTIONS))
        self._tune_pool(concurrency * segments)

        manifest = Manifest.load(self.root)
        self._manifest, self._plan = manifest, plan
        manifest.upsert_series(plan.series_id, title=plan.title, is_movie=plan.is_movie,
                               quality_height=target_height)
        for ep in plan.episodes:
            manifest.upsert_episode(plan.series_id, ep)
        manifest.save()
        manifest.start_flusher()

        eps_by_nb = {ep.nb: ep for ep in plan.episodes}
        selected = [ep for ep in plan.episodes
                    if selected_nbs is None or ep.nb in selected_nbs]
        work = [ep for ep in selected
                if manifest.status(plan.series_id, ep.nb) != STATUS_DONE]
        self.events.on_log(
            t("log_manifest_loaded", todo=len(work), done=len(selected) - len(work)))

        self._ready = queue.Queue()
        self.controls = {ep.nb: EpisodeControl() for ep in work}
        self.rates = RateRegistry()
        with self._lock:
            self._outstanding = {ep.nb for ep in work}
            self._progress = {}
            self._ep_state = {ep.nb: _QUEUED for ep in work}
        self._wake.clear()

        # Reflect already-finished episodes in the UI immediately.
        for ep in selected:
            if manifest.status(plan.series_id, ep.nb) == STATUS_DONE:
                self._emit_status(ep, STATUS_DONE, {})

        for ep in work:
            self._ready.put(ep.nb)

        workers = [
            threading.Thread(target=self._worker,
                             args=(manifest, plan, eps_by_nb, target_height, segments),
                             name=f"dl-{i}", daemon=True)
            for i in range(concurrency)
        ]
        for th in workers:
            th.start()

        ticker_stop = threading.Event()
        ticker = threading.Thread(target=self._rate_ticker, args=(ticker_stop,),
                                  name="rate-ticker", daemon=True)
        ticker.start()

        try:
            while True:
                if self._stopped():
                    break
                with self._lock:
                    finished = not self._outstanding
                if finished:
                    break
                self._wake.wait(timeout=0.25)
                self._wake.clear()
        finally:
            for _ in workers:
                self._ready.put(None)   # sentinels unblock idle workers
            for th in workers:
                th.join(timeout=30)
            ticker_stop.set()
            ticker.join(timeout=2)
            manifest.stop_flusher()

        summary = manifest.counts(plan.series_id)
        self.events.on_series_done(summary)
        return summary

    def _stopped(self) -> bool:
        return self._stop_global() or self._abort.is_set()

    def _tune_pool(self, size: int) -> None:
        adapter = HTTPAdapter(pool_connections=max(10, size), pool_maxsize=max(10, size))
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    # -- worker ---------------------------------------------------------------

    def _worker(self, manifest: Manifest, plan: SeriesPlan, eps_by_nb: dict,
                target_height: int, segments: int) -> None:
        while True:
            nb = self._ready.get()
            try:
                if nb is None:
                    return
                if self._stopped():
                    continue  # draining toward the sentinel
                ep = eps_by_nb.get(nb)
                ctl = self.controls.get(nb)
                if ep is None or ctl is None:
                    continue
                if ctl.cancelled:
                    self._set_state(nb, _CANCELLED, remove=True)
                    continue
                if ctl.paused:
                    manifest.set_status(plan.series_id, nb, STATUS_PAUSED, save=False)
                    self._set_state(nb, _PAUSED)
                    self._emit_status(ep, STATUS_PAUSED, {})
                    continue
                if manifest.status(plan.series_id, nb) == STATUS_DONE:
                    self._emit_status(ep, STATUS_DONE, {})
                    self._set_state(nb, _DONE, remove=True)
                    continue
                self._set_state(nb, _DOWNLOADING)
                try:
                    self._download_episode(manifest, plan, ep, target_height, segments, ctl)
                    self._set_state(nb, _DONE, remove=True)
                except downloader.StoppedError:
                    self._handle_stop(manifest, plan, ep, ctl)
                except downloader.DiskFullError as exc:
                    manifest.set_status(plan.series_id, nb, STATUS_ERROR, error=str(exc))
                    self._emit_status(ep, STATUS_ERROR, {"error": str(exc)})
                    self.events.on_log(t("log_disk_stop", err=exc))
                    self._abort.set()  # disk full -> stop the whole run
                    self._set_state(nb, _ERROR, remove=True)
                except Exception as exc:  # noqa: BLE001 - record & move on
                    manifest.set_status(plan.series_id, nb, STATUS_ERROR, error=str(exc))
                    self._emit_status(ep, STATUS_ERROR, {"error": str(exc)})
                    self.events.on_log(t("log_episode_error", label=_label(ep), err=exc))
                    self._set_state(nb, _ERROR, remove=True)
            finally:
                self._ready.task_done()

    def _handle_stop(self, manifest, plan, ep, ctl) -> None:
        """A graceful StoppedError fired: classify it (cancel/pause/global stop)."""
        nb = ep.nb
        if ctl.cancelled:
            manifest.set_status(plan.series_id, nb, STATUS_PENDING, save=False)
            self._emit_status(ep, STATUS_PENDING, {})
            self._set_state(nb, _CANCELLED, remove=True)
        elif ctl.paused:
            manifest.set_status(plan.series_id, nb, STATUS_PAUSED, save=False)
            self._emit_status(ep, STATUS_PAUSED, {})
            with self._lock:
                self._ep_state[nb] = _PAUSED
                self._progress.pop(nb, None)
            self.rates.reset_episode(nb)
        else:  # global stop / abort
            manifest.set_status(plan.series_id, nb, STATUS_PENDING)
            self.events.on_log(t("log_global_stopped"))
            self._set_state(nb, _QUEUED, remove=True)

    def _set_state(self, nb: str, state: str, *, remove: bool = False) -> None:
        with self._lock:
            self._ep_state[nb] = state
            if remove:
                self._outstanding.discard(nb)
                self._progress.pop(nb, None)
        if remove:
            self._wake.set()

    # -- one episode ----------------------------------------------------------

    def _download_episode(self, manifest: Manifest, plan: SeriesPlan, ep: Episode,
                          target_height: int, segments: int, ctl: EpisodeControl):
        sid = plan.series_id
        stem = naming.episode_stem(ep.season, ep.episode, ep.title, ep.year, ep.is_movie)
        paths = naming.build_paths(self.root, plan.title, ep.season, stem, ep.is_movie)
        rec = manifest.episode(sid, ep.nb)
        rel = lambda p: str(Path(p).relative_to(self.root)) if _under(p, self.root) else str(p)

        if paths["video"].exists() and rec and rec.get("status") == STATUS_DONE:
            self._emit_status(ep, STATUS_DONE, {})
            return

        manifest.set_status(sid, ep.nb, STATUS_DOWNLOADING,
                            video_path=rel(paths["video"]), part_path=rel(paths["part"]),
                            attempts=(rec.get("attempts", 0) + 1 if rec else 1))
        self._emit_status(ep, STATUS_DOWNLOADING, {})
        self.events.on_log(t("log_episode_start", label=_label(ep), title=ep.title))

        chosen: dict = {}

        def url_provider() -> str:
            if not chosen.get("resolved"):
                self.events.on_log(t("log_resolving", label=_label(ep)))
                chosen["resolved"] = True
            variants = self.api.transcoded_files(ep.nb)
            variant = downloader.select_variant(variants, target_height)
            if variant.height != target_height and not chosen.get("warned"):
                self.events.on_log(t(
                    "log_quality_fallback", label=_label(ep),
                    want=height_label(target_height), got=height_label(variant.height)))
                chosen["warned"] = True
            chosen["variant"] = variant
            return variant.video_url

        def on_progress(done: int, total):
            manifest.update_episode(sid, ep.nb, save=False,
                                    downloaded_bytes=done, total_bytes=total)
            with self._lock:
                self._progress[ep.nb] = (done, total)
            self.events.on_progress(ep.nb, done, total)

        should_stop = lambda: self._stopped() or ctl.paused or ctl.cancelled
        size = segmented.download_file_segmented(
            self.session, url_provider, paths["part"], paths["video"],
            segments=segments,
            on_progress=on_progress,
            on_rate_bytes=lambda d: self.rates.add(ep.nb, d),
            on_segments=lambda tots: self.events.on_segments(ep.nb, tots),
            on_segment_progress=lambda k, d: self.events.on_segment_progress(ep.nb, k, d),
            should_stop=should_stop, should_pause=lambda: ctl.paused,
            log=self.events.on_log,
        )

        variant: QualityVariant | None = chosen.get("variant")
        manifest.update_episode(
            sid, ep.nb, save=False,
            total_bytes=size, downloaded_bytes=size,
            actual_quality_name=variant.name if variant else None,
            actual_quality_height=variant.height if variant else None,
        )

        # Subtitles: all available languages/formats; failures are warnings only.
        self._download_subtitles(manifest, sid, ep, paths["stem_path"])

        manifest.set_status(sid, ep.nb, STATUS_DONE, error=None)
        self._emit_status(ep, STATUS_DONE, {"size": size})
        self.events.on_log(t("log_episode_done", label=_label(ep)))

    def _download_subtitles(self, manifest: Manifest, sid: str, ep: Episode, stem_path: Path):
        try:
            subs = self.api.translation_files(ep.nb)
        except Exception as exc:  # noqa: BLE001
            self.events.on_log(t("log_subs_fetch_failed", label=_label(ep), err=exc))
            return
        want = getattr(self, "_subtitle_langs", "all")
        subs = [s for s in subs if _sub_wanted(s.lang, want)]
        used: set[str] = set()
        records = []
        for sub in subs:
            dest = naming.subtitle_path(stem_path, sub.lang, sub.ext, used)
            ok = downloader.download_subtitle(self.session, sub, dest)
            rel_path = str(dest.relative_to(self.root)) if _under(dest, self.root) else str(dest)
            records.append({"lang": sub.lang, "ext": sub.ext, "path": rel_path, "done": ok})
            if not ok:
                self.events.on_log(
                    t("log_sub_failed", lang=sub.lang, ext=sub.ext, label=_label(ep)))
        if records:
            done_count = sum(1 for r in records if r["done"])
            self.events.on_log(
                t("log_subs_summary", label=_label(ep), done=done_count, found=len(records)))
        manifest.update_episode(sid, ep.nb, save=False, subtitles=records)

    # -- rate ticker ----------------------------------------------------------

    def _rate_ticker(self, stop_ev: threading.Event) -> None:
        while not stop_ev.wait(RATE_TICK_S):
            with self._lock:
                prog = dict(self._progress)
                outstanding = set(self._outstanding)
            for nb, (done, total) in prog.items():
                sp = self.rates.episode_speed(nb)
                remaining = (total - done) if total else None
                self.events.on_rate(nb, sp, eta_seconds(remaining, sp))
            gsp = self.rates.global_speed()
            rem = 0
            known = False
            for nb in outstanding:
                pr = prog.get(nb)
                if pr and pr[1]:
                    rem += max(0, pr[1] - pr[0])
                    known = True
            self.events.on_rate(None, gsp, eta_seconds(rem if known else None, gsp))

    # -- helpers --------------------------------------------------------------

    def _emit_status(self, ep: Episode, status: str, extra: dict):
        self.events.on_status(ep.nb, status, extra)


def _label(ep: Episode) -> str:
    if ep.is_movie:
        return t("movie_label")
    return f"S{ep.season:02d}E{ep.episode:02d}"


def _under(path, root) -> bool:
    try:
        Path(path).relative_to(root)
        return True
    except ValueError:
        return False


def _sub_wanted(lang: str, want: str) -> bool:
    """True if a subtitle track's language should be downloaded for ``want``.

    ``want`` is one of ``"all"`` (every language -- the engine default, today's
    behavior), ``"ar"``, ``"en"`` or ``"both"``. Matching is on the normalized,
    lowercased ``SubtitleFile.lang`` and tolerates ``ara``/``eng``-style codes.
    """
    if want == "all":
        return True
    code = (lang or "").lower()
    is_ar, is_en = code.startswith("ar"), code.startswith("en")
    if want == "ar":
        return is_ar
    if want == "en":
        return is_en
    if want == "both":
        return is_ar or is_en
    return True
