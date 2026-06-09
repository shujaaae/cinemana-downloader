"""Download-speed metering and ETA formatting.

A :class:`RateMeter` smooths bytes/sec over a short sliding window using
``time.monotonic()`` (immune to wall-clock jumps / DST). :class:`RateRegistry`
aggregates a per-episode meter plus a global meter so the UI can show both a
per-row speed/ETA and an overall figure.

Thread-safe: many segment threads call :meth:`RateMeter.add` concurrently, and
the GUI's rate ticker reads :meth:`speed_bps` from another thread.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Deque, Tuple


class RateMeter:
    """Smoothed bytes/sec over a sliding ``window_s`` window."""

    def __init__(self, window_s: float = 3.0,
                 clock: Callable[[], float] = time.monotonic):
        self.window_s = window_s
        self._clock = clock
        self._samples: Deque[Tuple[float, int]] = deque()
        self._start: float | None = None
        self._lock = threading.Lock()

    def add(self, n_bytes: int) -> None:
        if n_bytes <= 0:
            return
        now = self._clock()
        with self._lock:
            if self._start is None:
                self._start = now
            self._samples.append((now, n_bytes))
            self._trim(now)

    def _trim(self, now: float) -> None:
        cutoff = now - self.window_s
        s = self._samples
        while s and s[0][0] < cutoff:
            s.popleft()

    def speed_bps(self) -> float:
        now = self._clock()
        with self._lock:
            if self._start is None:
                return 0.0
            self._trim(now)
            total = sum(n for _, n in self._samples)
            # Average over the window once we have a full window of history,
            # else over the elapsed time since the first byte (avoids a wild
            # spike from the very first chunk while still ramping smoothly).
            elapsed = now - self._start
            denom = min(self.window_s, elapsed)
            if denom <= 0:
                return 0.0
            return total / denom

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._start = None


class RateRegistry:
    """A global meter plus one meter per episode (keyed by ``nb``)."""

    def __init__(self, window_s: float = 3.0,
                 clock: Callable[[], float] = time.monotonic):
        self._window_s = window_s
        self._clock = clock
        self._global = RateMeter(window_s, clock)
        self._meters: dict[str, RateMeter] = {}
        self._lock = threading.Lock()

    def meter(self, nb: str) -> RateMeter:
        with self._lock:
            m = self._meters.get(nb)
            if m is None:
                m = RateMeter(self._window_s, self._clock)
                self._meters[nb] = m
            return m

    def add(self, nb: str, n_bytes: int) -> None:
        """Feed a byte delta to both the episode and the global meter."""
        self.meter(nb).add(n_bytes)
        self._global.add(n_bytes)

    def episode_speed(self, nb: str) -> float:
        return self.meter(nb).speed_bps()

    def global_speed(self) -> float:
        return self._global.speed_bps()

    def active_nbs(self) -> list[str]:
        with self._lock:
            return list(self._meters.keys())

    def reset_episode(self, nb: str) -> None:
        with self._lock:
            m = self._meters.get(nb)
        if m is not None:
            m.reset()


# -- formatting helpers -------------------------------------------------------

def human_speed(bps: float) -> str:
    """Format a bytes/sec figure like ``"1.4 MB/s"`` (empty when zero)."""
    if not bps or bps <= 0:
        return ""
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    f = float(bps)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f} {u}" if u == "B/s" else f"{f:.1f} {u}"
        f /= 1024
    return ""


def eta_seconds(remaining_bytes: "int | None", speed_bps: float) -> "float | None":
    """Seconds remaining, or ``None`` when unknown (no size / zero speed)."""
    if remaining_bytes is None or remaining_bytes <= 0:
        return None
    if not speed_bps or speed_bps <= 0:
        return None
    return remaining_bytes / speed_bps


def format_eta(seconds: "float | None") -> str:
    """Format ETA as ``mm:ss`` or ``h:mm:ss`` (empty when unknown)."""
    if seconds is None or seconds != seconds or seconds <= 0:  # None / NaN / <=0
        return ""
    secs = int(seconds)
    if secs >= 3600:
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}"
    m, s = divmod(secs, 60)
    return f"{m:02d}:{s:02d}"
