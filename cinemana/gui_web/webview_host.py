"""Bridge between the Aurora HTML/JS front-end and the download engine.

The front-end never speaks HTTP, manifests, or threads. It calls the methods of
:class:`JsApi` (exposed to JavaScript by pywebview as ``window.pywebview.api``)
and receives a single stream of ``window.onAppEvent([...])`` batches pushed from
Python. This keeps the proven thread+queue model from the old Tk GUI:

* The download engine runs on background worker threads it manages itself.
* Engine callbacks (the :class:`~cinemana.service.Events` bundle) only ever push
  plain dict events onto a :class:`queue.Queue` — they never touch the webview.
* A daemon *pump* thread drains that queue ~12×/s and calls
  ``window.evaluate_js("window.onAppEvent(<json>)")`` so the UI updates live.

IMPORTANT (perf/stability): pywebview builds ``window.pywebview.api`` by walking
the **public** attributes of this object and *recursing* into any non-callable
one. So every bit of state here is a ``_``-prefixed (private) attribute — only
the API methods are public. Exposing e.g. the pywebview ``Window`` (whose
``.native`` is a .NET control with an infinitely-recursing ``AccessibilityObject``
chain) or a ``requests.Session`` would make that walk spin the CPU and freeze the
UI. Do not add public data attributes.

Every user-facing string still flows through :mod:`cinemana.i18n`; the whole
``STRINGS`` table is handed to JS once at boot so the language can flip in place
(and the layout mirror to RTL) without a round-trip.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import webview
from webview.window import FixPoint

from .. import i18n
from .. import naming
from ..api import parse_id
from ..manifest import Manifest
from ..service import (
    DEFAULT_CONCURRENCY, DEFAULT_SEGMENTS, MAX_CONNECTIONS, DownloadService,
    Events, SeriesPlan, height_label,
)
from ..settings import load_settings, save_settings
from ..session import load_session, plan_from_dict, plan_to_dict, save_session
from ..i18n import get_language, set_language, t

# How often the pump flushes queued engine events to the webview. Batching +
# coalescing keeps the number of (relatively expensive) evaluate_js round-trips
# bounded even when many segment threads report progress at once.
PUMP_INTERVAL_S = 0.08
PUMP_BATCH_CAP = 2000

# Minimum window size. Shared between create_window() and win_resize() (and
# mirrored in app.js) so the user-driven edge/corner resize can never shrink the
# window below what the layout supports.
MIN_W, MIN_H = 980, 680


def _human_size(n) -> str:
    if not n:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{n}"


class JsApi:
    """The object pywebview exposes to JavaScript as ``window.pywebview.api``.

    Public (non-underscore) methods are callable from JS and return JSON-able
    values. **All other state is private** (see the module docstring). Long work
    is delegated to background threads so a JS call never blocks the UI.
    """

    def __init__(self):
        self._window: webview.Window | None = None

        # Engine run state (mirrors the old Tk GUI's bookkeeping).
        self._service: DownloadService | None = None
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._plan: SeriesPlan | None = None
        self._hero: dict | None = None

        # Last-known form values, kept so we can re-persist on close.
        self._form: dict = {}

        # Python -> JS event plumbing.
        self._q: "queue.Queue" = queue.Queue()
        self._ui_ready = threading.Event()
        self._closed = threading.Event()
        self._pump_thread = threading.Thread(target=self._pump_loop, name="evt-pump", daemon=True)

        self._maximized = False

    # -- wiring ---------------------------------------------------------------

    def _bind(self, window: "webview.Window") -> None:
        self._window = window
        window.events.closing += self._on_closing
        self._pump_thread.start()

    # -- bootstrap ------------------------------------------------------------

    def get_bootstrap(self) -> dict:
        """Everything the UI needs to render its first frame (synchronous).

        Restores the previous session offline: the saved plan rebuilds the tree
        and the manifest at ``dest`` overlays per-episode status/percentage,
        exactly like the old Tk ``restore_session``.
        """
        settings = load_settings()
        lang = settings.get("language", i18n.DEFAULT_LANG)
        set_language(lang)

        sess = load_session()
        plan = plan_from_dict(sess.get("plan")) if sess.get("plan") else None
        self._plan = plan
        self._hero = sess.get("hero") if isinstance(sess.get("hero"), dict) else None

        # Persisted defaults (Settings tab) seed values when the last session
        # doesn't override them.
        dest = sess.get("dest") or settings.get("dest") or str(Path.cwd())
        self._form = {
            "url": sess.get("url", ""),
            "dest": dest,
            "quality_height": sess.get("quality_height"),
            "concurrency": sess.get("concurrency", DEFAULT_CONCURRENCY),
            "segments": sess.get("segments", DEFAULT_SEGMENTS),
            "subtitle_langs": sess.get("subtitle_langs", "ar"),
            "selected_nbs": sess.get("selected_nbs", []),
        }

        scan = self._scan_disk(plan, dest) if plan else {}
        return {
            "lang": lang,
            "dir": "rtl" if lang == "ar" else "ltr",
            "strings": i18n.STRINGS,
            "defaults": {
                "concurrency": DEFAULT_CONCURRENCY,
                "segments": DEFAULT_SEGMENTS,
                "max_connections": MAX_CONNECTIONS,
                "max_concurrency": 6,
                "max_segments": 32,
            },
            "prefs": {
                "dest": dest,
                "quality_height": settings.get("default_quality"),
                "concurrency": settings.get("default_concurrency", DEFAULT_CONCURRENCY),
                "segments": settings.get("default_segments", DEFAULT_SEGMENTS),
            },
            "session": dict(self._form),
            "plan": self._plan_payload(plan) if plan else None,
            "hero": self._hero,
            "statuses": scan,
            "scan_summary": self._scan_summary(scan, plan) if plan else None,
            "disk": self._disk(dest),
        }

    def ui_ready(self) -> None:
        """JS calls this once its DOM + ``onAppEvent`` handler are live."""
        self._ui_ready.set()

    # -- fetch ----------------------------------------------------------------

    def fetch(self, url: str) -> None:
        raw = (url or "").strip()
        if not raw:
            self._push({"kind": "fetch_error", "err": t("dlg_paste_url_first")})
            return
        threading.Thread(target=self._fetch_worker, args=(raw,), daemon=True).start()

    def _fetch_worker(self, raw: str) -> None:
        events = Events(on_log=lambda msg: self._push({"kind": "log", "msg": msg}))
        dest = self._form.get("dest") or str(Path.cwd())
        service = DownloadService(Path(dest), events=events)
        try:
            plan = service.prepare(raw)
        except Exception as exc:  # noqa: BLE001 - surface as a UI error toast
            self._push({"kind": "fetch_error", "err": str(exc)})
            return
        self._plan = plan
        self._hero = None
        self._push({"kind": "plan", "plan": self._plan_payload(plan)})
        # Disk scan: tell the UI which episodes are already on disk vs. missing.
        # A separate event because the 'plan' handler rebuilds the tree (resetting
        # rows to pending); _coalesce preserves discrete-event order so 'scan'
        # always lands after 'plan'.
        scan = self._scan_disk(plan, dest)
        self._push({"kind": "scan", "scan": scan,
                    "summary": self._scan_summary(scan, plan)})
        self._persist()
        # Rich hero metadata is a best-effort extra: the episode list already
        # works without it, so any failure here is silently ignored.
        try:
            hero = self._fetch_hero(service, raw, plan)
            if hero:
                self._hero = hero
                self._push({"kind": "hero", "hero": hero})
                self._persist()
        except Exception:  # noqa: BLE001
            pass

    def _fetch_hero(self, service: DownloadService, raw: str, plan: SeriesPlan) -> "dict | None":
        """Pull poster / synopsis / genres / IMDb for the hero card.

        These fields are not part of the engine's :class:`SeriesPlan`, so we read
        the raw ``allVideoInfo`` document and probe it generically — the exact key
        names vary across the API, and any missing field just degrades the card.
        """
        vid = parse_id(raw)
        data = service.api._get_json(f"allVideoInfo/id/{vid}")
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            return None

        lang = get_language()

        def first_url() -> str:
            best = ""
            for k, v in data.items():
                if not isinstance(v, str) or not v.startswith("http"):
                    continue
                kl = k.lower()
                if "trailer" in kl or "video" in kl or "url" not in kl and not any(
                        s in kl for s in ("img", "image", "poster", "thumb", "cover")):
                    continue
                if any(s in kl for s in ("img", "image", "poster", "thumb", "cover")):
                    return v
                best = best or v
            return best

        def synopsis() -> str:
            candidates: list[tuple[int, str]] = []
            for k, v in data.items():
                if not isinstance(v, str) or len(v.strip()) < 30:
                    continue
                kl = k.lower()
                if any(s in kl for s in ("content", "story", "plot", "summary",
                                          "description", "overview", "synopsis")):
                    # Prefer a value whose key matches the active language.
                    score = len(v) + (1_000_000 if kl.startswith(lang) else 0)
                    candidates.append((score, v.strip()))
            candidates.sort(reverse=True)
            return candidates[0][1] if candidates else ""

        def genres() -> list[str]:
            raw_g = data.get("genres") or data.get("categories") or data.get("genre")
            out: list[str] = []
            if isinstance(raw_g, list):
                for g in raw_g:
                    if isinstance(g, dict):
                        name = (g.get(f"{lang}_title") or g.get("en_title")
                                or g.get("ar_title") or g.get("name") or g.get("title"))
                        if name:
                            out.append(str(name).strip())
                    elif isinstance(g, str) and g.strip():
                        out.append(g.strip())
            elif isinstance(raw_g, str):
                out = [s.strip() for s in raw_g.split(",") if s.strip()]
            return out[:3]

        def imdb() -> str:
            for k, v in data.items():
                kl = k.lower()
                if any(s in kl for s in ("imdb", "stars", "rating")) and v not in (None, "", "0"):
                    try:
                        f = float(str(v).strip())
                        if f > 0:
                            return f"{f:.1f}".rstrip("0").rstrip(".")
                    except (ValueError, TypeError):
                        if isinstance(v, str) and v.strip():
                            return v.strip()
            return ""

        n_seasons = len({e.season for e in plan.episodes if not e.is_movie})
        n_episodes = len([e for e in plan.episodes if not e.is_movie]) or len(plan.episodes)
        return {
            "poster": first_url(),
            "year": str(data.get("year") or "").strip(),
            "genres": genres(),
            "imdb": imdb(),
            "synopsis": synopsis(),
            "n_seasons": n_seasons,
            "n_episodes": n_episodes,
        }

    # -- run / start ----------------------------------------------------------

    def start(self, payload: dict) -> None:
        """Begin (or resume-from-disk) a download run. Mirrors Tk ``_on_start``."""
        payload = payload or {}
        if not self._plan:
            return
        dest = (payload.get("dest") or self._form.get("dest") or "").strip()
        if not dest:
            self._push({"kind": "fetch_error", "err": t("dlg_choose_dest")})
            return
        selected = set(payload.get("selected_nbs") or [])
        if not selected:
            self._push({"kind": "fetch_error", "err": t("dlg_no_episode")})
            return
        if self._running():
            return  # already downloading; ignore a double start

        height = int(payload.get("height") or self._plan.default_height)
        n = max(1, int(payload.get("concurrency") or DEFAULT_CONCURRENCY))
        m = max(1, int(payload.get("segments") or DEFAULT_SEGMENTS))
        subs = payload.get("subtitle_langs") or "ar"
        if subs not in ("ar", "en", "both"):
            subs = "ar"

        self._form.update({
            "dest": dest, "quality_height": height,
            "concurrency": n, "segments": m,
            "subtitle_langs": subs,
            "selected_nbs": sorted(selected),
        })
        self._persist()

        self._stop_event.clear()
        self._push({"kind": "log", "msg": t(
            "log_start_run", n=len(selected), q=height_label(height),
            nn=n, m=m, dest=dest)})

        events = Events(
            on_log=lambda msg: self._push({"kind": "log", "msg": msg}),
            on_status=lambda nb, st, extra: self._push(
                {"kind": "status", "nb": nb, "status": st, "extra": extra}),
            on_progress=lambda nb, d, tot: self._push(
                {"kind": "progress", "nb": nb, "done": d, "total": tot}),
            on_segments=lambda nb, tots: self._push(
                {"kind": "segments", "nb": nb, "seg_totals": tots}),
            on_segment_progress=lambda nb, k, d: self._push(
                {"kind": "seg_progress", "nb": nb, "k": k, "done": d}),
            on_rate=lambda nb, sp, eta: self._push(
                {"kind": "rate", "nb": nb, "speed": sp, "eta": eta}),
            on_series_done=lambda s: self._push({"kind": "done", "summary": s}),
        )
        self._service = DownloadService(Path(dest), events=events)
        self._worker = threading.Thread(
            target=self._run_worker, args=(self._plan, height, selected, n, m, subs),
            daemon=True)
        self._worker.start()

    def _run_worker(self, plan, height, selected, n, m, subs) -> None:
        try:
            self._service.run(plan, height, selected_nbs=selected,
                              should_stop=self._stop_event.is_set,
                              concurrency=n, segments=m, subtitle_langs=subs)
        except Exception as exc:  # noqa: BLE001
            self._push({"kind": "log", "msg": t("log_unexpected_stop", err=exc)})
            self._push({"kind": "done", "summary": None})

    # -- live controls (thread-safe; delegate straight to the service) --------

    def stop(self) -> None:
        if self._running():
            self._stop_event.set()
            self._push({"kind": "log", "msg": t("log_stopping")})

    def pause_all(self) -> None:
        if self._running():
            self._service.request_pause_all()
            self._push({"kind": "log", "msg": t("log_pause_all")})

    def resume_all(self) -> None:
        if self._running():
            self._service.request_resume_all()
            self._push({"kind": "log", "msg": t("log_resume_all")})

    def pause(self, nb: str) -> None:
        if self._service:
            self._service.request_pause(str(nb))

    def resume(self, nb: str) -> None:
        if self._service:
            self._service.request_resume(str(nb))

    def cancel(self, nb: str) -> None:
        if self._service:
            self._service.request_cancel(str(nb))

    def _running(self) -> bool:
        return bool(self._service and self._worker and self._worker.is_alive())

    # -- misc UI calls --------------------------------------------------------

    def browse_dest(self) -> "dict | None":
        if not self._window:
            return None
        current = self._form.get("dest") or str(Path.cwd())
        folder_type = getattr(getattr(webview, "FileDialog", None), "FOLDER",
                              getattr(webview, "FOLDER_DIALOG", 20))
        try:
            result = self._window.create_file_dialog(folder_type, directory=current)
        except Exception:  # noqa: BLE001
            return None
        if not result:
            return None
        dest = result[0] if isinstance(result, (list, tuple)) else str(result)
        self._form["dest"] = dest
        self._persist()
        # Also remember it as the persisted default folder (Settings tab).
        save_settings({"dest": dest})
        out = {"dest": dest, "disk": self._disk(dest)}
        # Re-scan the new folder so the episode list reflects what's already there.
        if self._plan:
            scan = self._scan_disk(self._plan, dest)
            out["scan"] = scan
            out["summary"] = self._scan_summary(scan, self._plan)
        return out

    def scan(self) -> dict:
        """Scan the current destination on disk for the loaded plan.

        Returns ``{"scan": {nb: {...}}, "summary": {...}}`` for the manual
        Rescan button. Disk-authoritative (see :meth:`_scan_disk`); runs
        synchronously (a bounded handful of stat() calls per episode).
        """
        if not self._plan:
            return {"scan": {}, "summary": None}
        dest = self._form.get("dest") or str(Path.cwd())
        scan = self._scan_disk(self._plan, dest)
        return {"scan": scan, "summary": self._scan_summary(scan, self._plan)}

    def set_language(self, lang: str) -> None:
        lang = lang if lang in i18n.LANGUAGES else i18n.DEFAULT_LANG
        set_language(lang)
        save_settings({"language": lang})

    def save_prefs(self, values: dict) -> None:
        """Persist the Settings-tab defaults (quality / concurrency / segments).

        Stored in ``settings.json`` alongside ``language``; ``save_settings``
        merges arbitrary keys, so no engine change is needed. Only the known
        keys are forwarded so the UI can't write junk into the settings file.
        """
        if not isinstance(values, dict):
            return
        allowed = ("default_quality", "default_concurrency", "default_segments")
        clean = {k: values[k] for k in allowed if k in values}
        if clean:
            save_settings(clean)

    def save_state(self, values: dict) -> None:
        """JS calls this whenever inputs/selection change (resume snapshot)."""
        if isinstance(values, dict):
            self._form.update(values)
        self._persist()

    # -- library --------------------------------------------------------------

    def get_library(self) -> dict:
        """Downloaded content from the manifest at the current ``dest``.

        Read-only: loads a *fresh* :class:`Manifest` (a local, never stored on
        ``self`` — the public-attr walk would otherwise recurse into it) and
        flattens it into series -> seasons -> episodes for the Library tab.
        """
        dest = self._form.get("dest") or str(Path.cwd())
        root = Path(dest)
        try:
            m = Manifest.load(root)
            series_map = m._data.get("series") or {}
        except Exception:  # noqa: BLE001 - never let the Library crash the UI
            return {"dest": dest, "series": []}

        series_out = []
        for sid, srec in series_map.items():
            if not isinstance(srec, dict):
                continue
            episodes = []
            for nb, rec in (srec.get("episodes") or {}).items():
                if not isinstance(rec, dict):
                    continue
                season = rec.get("season")
                ep_no = rec.get("episode")
                is_movie = bool(srec.get("is_movie"))
                vpath = rec.get("video_path")
                abs_path = str(root / vpath) if vpath else ""
                exists = bool(abs_path) and Path(abs_path).exists()
                episodes.append({
                    "nb": nb,
                    "season": season,
                    "episode": ep_no,
                    "is_movie": is_movie,
                    "label": None if is_movie or season is None or ep_no is None
                             else f"S{int(season):02d}E{int(ep_no):02d}",
                    "title": rec.get("title") or "",
                    "status": rec.get("status", "pending"),
                    "downloaded_bytes": rec.get("downloaded_bytes") or 0,
                    "total_bytes": rec.get("total_bytes"),
                    "abs_path": abs_path,
                    "exists": exists,
                })
            episodes.sort(key=lambda e: (
                e["season"] if isinstance(e["season"], int) else 0,
                e["episode"] if isinstance(e["episode"], int) else 0,
            ))
            series_out.append({
                "series_id": sid,
                "title": srec.get("title") or "",
                "is_movie": bool(srec.get("is_movie")),
                "counts": m.counts(sid),
                "episodes": episodes,
            })
        series_out.sort(key=lambda s: s["title"].lower())
        return {"dest": dest, "series": series_out}

    def open_path(self, path: str) -> None:
        """Open a downloaded file with the OS default application."""
        p = (path or "").strip()
        if not p or not Path(p).exists():
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(p)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p])
            else:
                subprocess.Popen(["xdg-open", p])
        except Exception:  # noqa: BLE001
            pass

    def reveal_path(self, path: str) -> None:
        """Reveal a file in the OS file manager (selected when possible)."""
        p = (path or "").strip()
        if not p:
            return
        target = Path(p)
        try:
            if sys.platform.startswith("win"):
                if target.exists():
                    subprocess.Popen(["explorer", "/select,", str(target)])
                elif target.parent.exists():
                    os.startfile(str(target.parent))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                if target.exists():
                    subprocess.Popen(["open", "-R", str(target)])
                elif target.parent.exists():
                    subprocess.Popen(["open", str(target.parent)])
            else:
                folder = target.parent if not target.is_dir() else target
                if folder.exists():
                    subprocess.Popen(["xdg-open", str(folder)])
        except Exception:  # noqa: BLE001
            pass

    # -- window controls (frameless titlebar) ---------------------------------

    def win_minimize(self) -> None:
        if self._window:
            try:
                self._window.minimize()
            except Exception:  # noqa: BLE001
                pass

    def win_maximize(self) -> None:
        if not self._window:
            return
        try:
            if self._maximized:
                self._window.restore()
            else:
                self._window.maximize()
            self._maximized = not self._maximized
        except Exception:  # noqa: BLE001
            pass

    def win_resize(self, width, height, fix_east=False, fix_south=False) -> None:
        """Resize the frameless window from a JS edge/corner grip.

        ``fix_east`` keeps the right edge anchored (so the left edge moves);
        ``fix_south`` keeps the bottom anchored (so the top edge moves). The
        backend only reads the EAST/SOUTH bits of ``fix_point``; the base
        NORTH|WEST is harmless. Width/height are clamped to the minimum here as
        a backstop in case the JS clamp is bypassed.
        """
        if not self._window:
            return
        try:
            w = max(MIN_W, int(width))
            h = max(MIN_H, int(height))
            fp = FixPoint.NORTH | FixPoint.WEST
            if fix_east:
                fp |= FixPoint.EAST
            if fix_south:
                fp |= FixPoint.SOUTH
            self._window.resize(w, h, fp)
        except Exception:  # noqa: BLE001
            pass

    def win_close(self) -> None:
        if self._window:
            self._window.destroy()

    # -- persistence / restore helpers ----------------------------------------

    def _persist(self) -> None:
        data = dict(self._form)
        data["plan"] = plan_to_dict(self._plan) if self._plan else None
        data["hero"] = self._hero
        save_session(data)

    def _plan_payload(self, plan: SeriesPlan) -> dict:
        heights = plan.available_heights or [1080, 720, 480, 360, 240]
        episodes = []
        for ep in plan.episodes:
            episodes.append({
                "nb": ep.nb,
                "season": ep.season,
                "episode": ep.episode,
                "title": ep.title,
                "year": ep.year,
                "is_movie": ep.is_movie,
                "label": None if ep.is_movie else f"S{ep.season:02d}E{ep.episode:02d}",
            })
        n_seasons = len({e.season for e in plan.episodes if not e.is_movie})
        n_episodes = len([e for e in plan.episodes if not e.is_movie]) or len(plan.episodes)
        return {
            "series_id": plan.series_id,
            "title": plan.title,
            "is_movie": plan.is_movie,
            "episodes": episodes,
            "heights": [{"value": h, "label": height_label(h)} for h in heights],
            "default_height": plan.default_height,
            "n_seasons": n_seasons,
            "n_episodes": n_episodes,
        }

    def _restore_statuses(self, plan: "SeriesPlan | None", dest: str) -> dict:
        """Overlay saved per-episode status + % from the manifest (offline).

        Partially-downloaded episodes show as *paused* with their percentage, so
        the user sees exactly where each file stopped — same as the old Tk app.
        """
        out: dict = {}
        if not plan:
            return out
        try:
            m = Manifest.load(Path(dest))
        except Exception:  # noqa: BLE001
            return out
        for ep in plan.episodes:
            rec = m.episode(plan.series_id, ep.nb)
            if not rec:
                continue
            status = rec.get("status", "pending")
            done = rec.get("downloaded_bytes") or 0
            total = rec.get("total_bytes")
            if status == "done":
                out[ep.nb] = {"status": "done", "done": total, "total": total}
            elif status == "error":
                out[ep.nb] = {"status": "error", "done": done, "total": total}
            elif done > 0 or status in ("paused", "downloading"):
                out[ep.nb] = {"status": "paused", "done": done, "total": total}
            # else: pending with 0 bytes -> leave UI default
        return out

    @staticmethod
    def _file_ok(p: Path) -> bool:
        """True if ``p`` is a real, non-empty file. Never raises."""
        try:
            return p.is_file() and p.stat().st_size > 0
        except OSError:
            return False

    def _scan_disk(self, plan: "SeriesPlan | None", dest: str) -> dict:
        """Disk-authoritative scan: which episodes are already downloaded.

        Returns the same ``nb -> {status, done, total}`` shape the front-end's
        ``applyStatuses`` consumes (pending episodes are omitted). For each
        episode the expected ``.mp4`` path is recomputed from the engine's own
        :mod:`cinemana.naming` helpers, so the scan works even when the manifest
        is missing or out of sync (files downloaded elsewhere, manifest deleted).

        * ``done``    — the final ``.mp4`` exists (size > 0). The reported size is
          the file's real ``st_size`` on disk, so it shows without a manifest.
        * ``paused``  — no final file, but a ``.part`` / ``.part.K`` exists or the
          manifest still has partial bytes (shown with its %).
        * (omitted)   — neither exists -> the UI leaves the row pending.

        The manifest, when present, only *adds* a second candidate path (the exact
        name the engine wrote) and the byte totals; it is never required.
        """
        out: dict = {}
        if not plan:
            return out
        root = Path(dest)
        # Manifest is a best-effort supplement only — local var, never on self
        # (a public-attr walk would otherwise recurse into it; see get_library).
        try:
            m = Manifest.load(root)
        except Exception:  # noqa: BLE001
            m = None

        for ep in plan.episodes:
            stem = naming.episode_stem(ep.season, ep.episode, ep.title,
                                       year=ep.year, is_movie=ep.is_movie)
            paths = naming.build_paths(root, plan.title, ep.season, stem, ep.is_movie)
            video = paths["video"]
            rec = m.episode(plan.series_id, ep.nb) if m else None

            # 1) Final .mp4 present? (computed path, then the manifest's recorded one)
            candidates = [video]
            if rec and rec.get("video_path"):
                candidates.append(root / rec["video_path"])
            done_path = next((p for p in candidates if self._file_ok(p)), None)
            if done_path:
                try:
                    size = done_path.stat().st_size
                except OSError:
                    size = (rec or {}).get("total_bytes") or 0
                out[ep.nb] = {"status": "done", "done": size, "total": size}
                continue

            # 2) Partial: a .part / .part.K on disk, or manifest partial bytes.
            part_bytes = self._partial_bytes(paths["part"], rec, root)
            man_done = (rec or {}).get("downloaded_bytes") or 0
            if part_bytes > 0 or man_done > 0:
                out[ep.nb] = {
                    "status": "paused",
                    "done": part_bytes or man_done,
                    "total": (rec or {}).get("total_bytes"),
                }
            # 3) else: not downloaded -> omit (UI keeps the pending default)
        return out

    @staticmethod
    def _partial_bytes(part: Path, rec: "dict | None", root: Path) -> int:
        """Largest partial-file size on disk for an episode (best-effort, 0 if none).

        Covers the single ``.mp4.part`` and the segmented ``.mp4.part.K`` chunks,
        plus the manifest's recorded ``part_path`` if different.
        """
        best = 0
        candidates = [part]
        if rec and rec.get("part_path"):
            candidates.append(root / rec["part_path"])
        for p in candidates:
            try:
                if p.is_file():
                    best = max(best, p.stat().st_size)
            except OSError:
                pass
        # Segmented downloads write <stem>.mp4.part.0, .part.1, ... — sum them.
        try:
            seg_total = 0
            for chunk in part.parent.glob(part.name + ".*"):
                try:
                    seg_total += chunk.stat().st_size
                except OSError:
                    pass
            best = max(best, seg_total)
        except OSError:
            pass
        return best

    def _scan_summary(self, scan: dict, plan: "SeriesPlan | None") -> "dict | None":
        """Whole-series counts for the 'N downloaded · M not downloaded' line.

        Computed over *all* ``plan.episodes`` because the scan map omits pending
        episodes; ``not_downloaded`` therefore includes partials and pending.
        """
        if not plan:
            return None
        total = len(plan.episodes)
        downloaded = sum(1 for v in scan.values() if v.get("status") == "done")
        partial = sum(1 for v in scan.values() if v.get("status") == "paused")
        return {
            "downloaded": downloaded,
            "partial": partial,
            "not_downloaded": total - downloaded - partial,
            "total": total,
        }

    def _disk(self, dest: str) -> dict:
        """Disk usage for the 'Saving to' card. Best-effort; never raises."""
        try:
            path = Path(dest)
            probe = path if path.exists() else path.anchor or Path.cwd()
            usage = shutil.disk_usage(str(probe))
            used_pct = int(usage.used * 100 / usage.total) if usage.total else 0
            return {
                "used_pct": used_pct,
                "free_label": _human_size(usage.free),
                "total_label": _human_size(usage.total),
                "dest": dest,
            }
        except Exception:  # noqa: BLE001
            return {"used_pct": 0, "free_label": "", "total_label": "", "dest": dest}

    # -- Python -> JS pump ----------------------------------------------------

    def _push(self, event: dict) -> None:
        self._q.put(event)

    def _pump_loop(self) -> None:
        self._ui_ready.wait()
        while not self._closed.is_set():
            time.sleep(PUMP_INTERVAL_S)
            drained = []
            while len(drained) < PUMP_BATCH_CAP:
                try:
                    evt = self._q.get_nowait()
                except queue.Empty:
                    break
                if evt is None:
                    self._closed.set()
                    break
                drained.append(evt)
            if not drained:
                continue
            batch = _coalesce(drained)
            if batch:
                self._emit(batch)

    def _emit(self, batch: list) -> None:
        if not self._window:
            return
        try:
            data = json.dumps(batch, ensure_ascii=True)
            self._window.evaluate_js(f"window.onAppEvent({data})")
        except Exception:  # noqa: BLE001 - a closing window can race the pump
            pass

    # -- window lifecycle -----------------------------------------------------

    def _on_closing(self) -> bool:
        """Persist + (if downloading) confirm before the window closes."""
        self._persist()
        if self._running():
            try:
                ok = self._window.create_confirmation_dialog(
                    t("app_title"), t("dlg_close_while_downloading"))
            except Exception:  # noqa: BLE001
                ok = True
            if not ok:
                return False
            self._stop_event.set()
        self._closed.set()
        self._q.put(None)
        return True


def _coalesce(events: list) -> list:
    """Collapse the high-frequency events in one batch to their latest value.

    During a download the engine fires ``progress``/``seg_progress``/``rate`` once
    per network chunk per segment. Only the newest value per (episode) / (episode,
    block) matters to the UI, so we keep just those and forward the discrete
    events (log/status/segments/plan/hero/done/…) in order. This keeps each
    ``evaluate_js`` payload tiny no matter how fast the download runs.
    """
    discrete = []
    prog: dict = {}
    segp: dict = {}
    rate: dict = {}
    finalized = set()           # episodes that ended in this batch
    for ev in events:
        kind = ev.get("kind")
        if kind == "progress":
            prog[ev["nb"]] = ev
        elif kind == "seg_progress":
            segp[(ev["nb"], ev["k"])] = ev
        elif kind == "rate":
            rate[ev.get("nb")] = ev
        else:
            discrete.append(ev)
            if kind == "status" and ev.get("status") in ("done", "paused", "error"):
                finalized.add(ev.get("nb"))
    out = list(discrete)
    out.extend(ev for nb, ev in prog.items() if nb not in finalized)
    out.extend(ev for (nb, _k), ev in segp.items() if nb not in finalized)
    out.extend(ev for nb, ev in rate.items() if nb is None or nb not in finalized)
    return out


def run() -> None:
    """Launch the Aurora window. Entry point used by ``app.py``."""
    settings = load_settings()
    set_language(settings.get("language", i18n.DEFAULT_LANG))

    api = JsApi()
    here = Path(__file__).resolve().parent
    index = here / "index.html"

    window = webview.create_window(
        t("app_title"),
        url=str(index),
        js_api=api,
        width=1180,
        height=800,
        min_size=(MIN_W, MIN_H),
        frameless=True,
        easy_drag=False,
        background_color="#0b1622",
    )
    api._bind(window)
    webview.start()
