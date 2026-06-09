"""Crash-safe download state.

The manifest lives at ``<root>/.cinemana_state.json`` and records, per series,
the chosen quality and the status of every episode. It is written atomically
(temp file + ``os.fsync`` + ``os.replace``) so a power loss can never leave a
half-written state file.

The manifest is a *convenience* index, not the source of truth: the authoritative
resume offset is always the real size of the ``.part`` file on disk, re-read on
every attempt. A corrupt manifest is backed up and rebuilt from scratch.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path

STATE_FILENAME = ".cinemana_state.json"

STATUS_PENDING = "pending"
STATUS_DOWNLOADING = "downloading"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_PAUSED = "paused"

# How often the background flusher persists accumulated save=False changes.
FLUSH_INTERVAL_S = 2.0


class Manifest:
    """Thread-safe, atomically-persisted download state for one folder."""

    def __init__(self, root: Path, data: dict | None = None):
        self.root = Path(root)
        self.path = self.root / STATE_FILENAME
        self._lock = threading.RLock()        # guards self._data
        self._io_lock = threading.Lock()      # serializes disk writes (tmp reuse)
        self._data = data if data is not None else {"version": 1, "series": {}}
        # Background debounced flusher state.
        self._dirty = False
        self._flush_wake = threading.Event()
        self._flush_stop = threading.Event()
        self._flusher: threading.Thread | None = None

    # -- load / save ----------------------------------------------------------

    @classmethod
    def load(cls, root: Path) -> "Manifest":
        root = Path(root)
        path = root / STATE_FILENAME
        if not path.exists():
            return cls(root)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or "series" not in data:
                raise ValueError("bad shape")
            return cls(root, data)
        except (ValueError, OSError):
            # Corrupt/unreadable: preserve it and start fresh.
            try:
                backup = path.with_suffix(f".corrupt-{int(time.time())}.json")
                os.replace(path, backup)
            except OSError:
                pass
            return cls(root)

    def _snapshot(self) -> dict:
        """Deep-copy the state under the data lock and clear the dirty flag.

        Snapshotting (rather than writing ``self._data`` directly) lets the slow
        fsync happen *outside* the data lock, so worker threads recording
        progress are never blocked on disk I/O.
        """
        with self._lock:
            self._dirty = False
            return copy.deepcopy(self._data)

    def _write_atomic(self, data: dict) -> None:
        # io_lock serializes writers so the single temp path is never interleaved.
        with self._io_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)

    def save(self) -> None:
        """Immediate, synchronous atomic flush (durability-critical points)."""
        self._write_atomic(self._snapshot())

    def mark_dirty(self) -> None:
        with self._lock:
            self._dirty = True

    # -- background debounced flusher -----------------------------------------

    def start_flusher(self, interval: float = FLUSH_INTERVAL_S) -> None:
        """Spawn a daemon thread that persists accumulated changes periodically.

        Coalesces a storm of ``save=False`` progress updates (from many
        concurrent segment threads) into ~one disk write per ``interval``.
        """
        if self._flusher and self._flusher.is_alive():
            return
        self._flush_stop.clear()

        def _loop():
            while not self._flush_stop.is_set():
                self._flush_wake.wait(timeout=interval)
                self._flush_wake.clear()
                if self._dirty:
                    self._write_atomic(self._snapshot())
            # Final flush so nothing accumulated near shutdown is lost.
            if self._dirty:
                self._write_atomic(self._snapshot())

        self._flusher = threading.Thread(target=_loop, name="manifest-flusher", daemon=True)
        self._flusher.start()

    def stop_flusher(self) -> None:
        self._flush_stop.set()
        self._flush_wake.set()
        t = self._flusher
        if t and t.is_alive():
            t.join(timeout=10)
        self._flusher = None

    # -- series / episode access ---------------------------------------------

    def _series(self, series_id: str) -> dict:
        # Caller MUST hold self._lock: setdefault mutates self._data.
        return self._data.setdefault("series", {}).setdefault(series_id, {})

    def upsert_series(self, series_id: str, *, title: str, is_movie: bool,
                      quality_height: int) -> None:
        with self._lock:
            s = self._series(series_id)
            s.setdefault("series_id", series_id)
            s["title"] = title
            s["is_movie"] = is_movie
            s["chosen_quality_height"] = quality_height
            s.setdefault("episodes", {})
            now = _utcstamp()
            s.setdefault("created_at", now)
            s["updated_at"] = now
            self._dirty = True

    def upsert_episode(self, series_id: str, episode) -> dict:
        """Insert the episode if absent; return its record."""
        with self._lock:
            episodes = self._series(series_id).setdefault("episodes", {})
            rec = episodes.get(episode.nb)
            if rec is None:
                rec = {
                    "nb": episode.nb,
                    "season": episode.season,
                    "episode": episode.episode,
                    "title": episode.title,
                    "status": STATUS_PENDING,
                    "video_path": None,
                    "part_path": None,
                    "total_bytes": None,
                    "downloaded_bytes": 0,
                    "actual_quality_name": None,
                    "actual_quality_height": None,
                    "segments": None,   # informational per-segment progress (disk is truth)
                    "subtitles": [],
                    "error": None,
                    "attempts": 0,
                }
                episodes[episode.nb] = rec
                self._dirty = True
            return rec

    def episode(self, series_id: str, nb: str) -> dict | None:
        with self._lock:
            return self._series(series_id).get("episodes", {}).get(nb)

    def status(self, series_id: str, nb: str) -> str:
        with self._lock:
            rec = self._series(series_id).get("episodes", {}).get(nb)
            return rec["status"] if rec else STATUS_PENDING

    def update_episode(self, series_id: str, nb: str, *, save: bool = True, **fields) -> None:
        with self._lock:
            rec = self._series(series_id).get("episodes", {}).get(nb)
            if rec is None:
                return
            rec.update(fields)
            self._series(series_id)["updated_at"] = _utcstamp()
            self._dirty = True
        if save:
            self.save()

    def set_status(self, series_id: str, nb: str, status: str, *, save: bool = True, **fields) -> None:
        self.update_episode(series_id, nb, status=status, save=save, **fields)

    # -- read-only helpers ----------------------------------------------------

    def series_record(self, series_id: str) -> dict:
        with self._lock:
            return self._series(series_id)

    def counts(self, series_id: str) -> dict:
        out = {STATUS_PENDING: 0, STATUS_DOWNLOADING: 0, STATUS_DONE: 0,
               STATUS_ERROR: 0, STATUS_PAUSED: 0}
        with self._lock:
            # Snapshot statuses inside the lock to avoid "dict changed size".
            statuses = [rec.get("status", STATUS_PENDING)
                        for rec in self._series(series_id).get("episodes", {}).values()]
        for st in statuses:
            out[st] = out.get(st, 0) + 1
        out["total"] = len(statuses)
        return out


def _utcstamp() -> str:
    # time.gmtime() avoids importing datetime just for an ISO string.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
