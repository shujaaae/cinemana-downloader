"""Multi-segment (parallel) download engine — IDM-style acceleration.

Splits ONE remote file across ``M`` parallel connections to use more of a slow
link's capacity. The design deliberately preserves the single-segment engine's
crash-safety invariant — *"the file size on disk IS the progress"* — by giving
each segment its OWN ``.partK`` file written append-only by the unmodified
:func:`cinemana.downloader.download_file` (with a byte window). So each
``.partK`` size is that segment's progress, resume is the same proven Range
logic per segment, and expired-URL refresh is handled per segment for free.

Crash-safety of the only genuinely new step — concatenation — is exhaustive:
the ``.partK`` files are the durable source and are never deleted until the
final file is atomically in place (see :func:`_concatenate`).

Fallbacks (never a regression vs. the old path): if the total size is unknown,
the server ignores Range (returns 200), the file is below ``min_split_size``,
or ``segments <= 1``, we transparently delegate to the single-segment
``download_file``.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

import requests

from . import downloader
from .downloader import CHUNK, DownloadError
from .i18n import t


# -- segment math & paths -----------------------------------------------------

def _segment_bounds(total: int, m: int) -> list[tuple[int, int]]:
    """Split ``[0, total)`` into ``m`` contiguous inclusive ranges.

    The last segment absorbs the remainder so the union is exactly the file.
    """
    base = total // m
    bounds = []
    start = 0
    for k in range(m):
        end = total - 1 if k == m - 1 else start + base - 1
        bounds.append((start, end))
        start = end + 1
    return bounds


def _seg_path(dest_part: Path, k: int) -> Path:
    """``out.mp4.part`` -> ``out.mp4.part.0`` (adjacent, glob-able)."""
    return dest_part.with_name(dest_part.name + f".{k}")


def _find_part_files(dest_part: Path) -> list[Path]:
    """All ``<dest_part>.<digits>`` segment files currently on disk."""
    parent = dest_part.parent
    prefix = dest_part.name + "."
    found: list[Path] = []
    if parent.exists():
        for p in parent.iterdir():
            if p.name.startswith(prefix) and p.name[len(prefix):].isdigit():
                found.append(p)
    return found


# -- probe: can we split? -----------------------------------------------------

def _probe_total(session: requests.Session, url_provider: Callable[[], str],
                 log: Callable[[str], None]) -> int | None:
    """Return the remote file size if Range is supported, else ``None``.

    A cheap ``Range: bytes=0-0`` request reveals both the total (via
    ``Content-Range``) and whether the server honours Range (206 vs 200).
    ``None`` means "do not split" (unknown size or no range support) and the
    caller falls back to the single-segment engine.
    """
    attempts = 0
    while True:
        attempts += 1
        try:
            url = url_provider()
            resp = session.get(
                url, headers={"Range": "bytes=0-0"}, stream=True,
                timeout=(downloader.CONNECT_TIMEOUT, downloader.READ_TIMEOUT),
                allow_redirects=True,
            )
            try:
                code = resp.status_code
                if code in (401, 403):
                    raise downloader.ExpiredURLError()
                if code == 206 or code == 416:
                    return downloader._total_from_content_range(resp)
                if code == 200:
                    return None  # server ignored Range -> cannot split safely
                resp.raise_for_status()
                return None
            finally:
                resp.close()
        except downloader.ExpiredURLError:
            if attempts >= downloader.MAX_ATTEMPTS:
                return None
            continue
        except requests.RequestException:
            if attempts >= downloader.MAX_ATTEMPTS:
                return None
            time.sleep(downloader.backoff_delay(attempts))
            continue


# -- resume / recovery decision ----------------------------------------------

def _resume_state(dest_part: Path, dest_final: Path, total: int, m: int) -> str:
    """Decide what to do on entry for one episode.

    Returns one of:
    * ``"done"``     — the final file already exists and is the right size.
    * ``"concat"``   — every ``.partK`` is fully downloaded (e.g. we died during
                       a previous concatenation); just (re)concatenate.
    * ``"download"`` — at least one segment still needs bytes.
    """
    if dest_final.exists() and dest_final.stat().st_size == total:
        return "done"
    bounds = _segment_bounds(total, m)
    for k, (start, end) in enumerate(bounds):
        p = _seg_path(dest_part, k)
        need = end - start + 1
        if not p.exists() or p.stat().st_size != need:
            return "download"
    return "concat"


# -- crash-safe concatenation -------------------------------------------------

def _concatenate(dest_part: Path, dest_final: Path, total: int, m: int) -> int:
    """Join all ``.partK`` into the final file, crash-safe at every step.

    Protocol (each step's crash recovery in parentheses):
      1. Verify every ``.partK`` is exactly full. (else -> caller resumes it)
      2. Stream all parts into a FRESH ``dest_part`` (wb), parts untouched, fsync.
         (crash: ``dest_part`` is disposable garbage; the full ``.partK`` files
          survive and the next run re-concatenates from scratch.)
      3. ``os.replace(dest_part, dest_final)`` — atomic promote.
         (crash before: no final file, parts intact -> re-concat. after: final
          file is complete and correct.)
      4. ONLY THEN delete the ``.partK`` files.
         (crash mid-cleanup: final file already correct; stray parts removed
          on the next run. Idempotent.)
    """
    bounds = _segment_bounds(total, m)
    parts = [_seg_path(dest_part, k) for k in range(m)]
    for k, (start, end) in enumerate(bounds):
        need = end - start + 1
        size = parts[k].stat().st_size if parts[k].exists() else 0
        if size != need:
            raise DownloadError(t("err_segment_incomplete", k=k, size=size, need=need))

    dest_part.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_part, "wb") as out:
        for p in parts:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out, CHUNK)
        out.flush()
        os.fsync(out.fileno())

    final_size = dest_part.stat().st_size
    if final_size != total:
        # Do NOT clean up; leave parts for investigation / a fresh re-concat.
        raise DownloadError(t("err_merged_size_mismatch", size=final_size, total=total))

    dest_final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(dest_part, dest_final)  # consumes dest_part (moved to final)
    _cleanup_segment_parts(dest_part)
    return final_size


def _cleanup_segment_parts(dest_part: Path) -> None:
    """Remove leftover numbered ``.partK`` files (NOT the live ``.part``)."""
    for p in _find_part_files(dest_part):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


# -- public entry point -------------------------------------------------------

def download_file_segmented(
    session: requests.Session,
    url_provider: Callable[[], str],
    dest_part: Path,
    dest_final: Path,
    *,
    segments: int = 4,
    min_split_size: int = 16 << 20,
    on_progress: Callable[[int, "int | None"], None] | None = None,
    on_rate_bytes: Callable[[int], None] | None = None,
    on_segments: Callable[["list[int]"], None] | None = None,
    on_segment_progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
    log: Callable[[str], None] | None = None,
    thread_factory: Callable[..., threading.Thread] | None = None,
) -> int:
    """Download one file across ``segments`` parallel connections, with resume.

    ``on_progress(done_total, total)`` reports the aggregate episode progress
    (absolute, race-tolerant). ``on_rate_bytes(delta)`` is fed the newly
    downloaded byte counts for a rate meter. ``on_segments(seg_totals)`` fires
    ONCE when the split is decided (one entry per block; a 1-element list / a 0
    total means a single, possibly indeterminate, block) and
    ``on_segment_progress(k, done_k)`` reports each block's absolute bytes for an
    IDM-style per-segment bar. Raises
    :class:`~cinemana.downloader.PausedError` /
    :class:`~cinemana.downloader.StoppedError` on a graceful pause/stop.
    Returns the final file size in bytes.
    """
    log = log or (lambda *_: None)
    thread_factory = thread_factory or (lambda **kw: threading.Thread(**kw))
    m = max(1, int(segments))

    def _single() -> int:
        """Single-connection path: reuse the proven engine, still feeding rate."""
        _cleanup_segment_parts(dest_part)  # drop stale segment files from a prior M
        prev = {"n": dest_part.stat().st_size if dest_part.exists() else 0}
        emitted = {"segments": False}

        def wrap(done, t):
            # One block for the whole file; learned lazily once the size is known.
            if on_segments and not emitted["segments"]:
                on_segments([t if t else 0])  # 0 -> indeterminate (unknown total)
                emitted["segments"] = True
            if on_segment_progress:
                on_segment_progress(0, done)
            if on_rate_bytes:
                d = done - prev["n"]
                if d > 0:
                    on_rate_bytes(d)
                prev["n"] = done
            if on_progress:
                on_progress(done, t)

        return downloader.download_file(
            session, url_provider, dest_part, dest_final,
            on_progress=wrap, should_stop=should_stop, log=log,
        )

    # 0) Already finished in a previous run?
    if dest_final.exists() and dest_final.stat().st_size > 0:
        _cleanup_segment_parts(dest_part)
        try:
            dest_part.unlink(missing_ok=True)
        except OSError:
            pass
        return dest_final.stat().st_size

    # 1) Can we split? Probe for size + range support.
    total = _probe_total(session, url_provider, log) if m > 1 else None

    if m <= 1 or total is None or total < min_split_size:
        if m > 1:
            log(t("log_single_conn"))
        return _single()

    m = max(1, min(m, total))  # never more segments than bytes
    if m == 1:
        return _single()
    bounds = _segment_bounds(total, m)
    part_paths = [_seg_path(dest_part, k) for k in range(m)]

    state = _resume_state(dest_part, dest_final, total, m)
    if state == "done":
        _cleanup_segment_parts(dest_part)
        return dest_final.stat().st_size

    if state == "download":
        log(t("log_split", m=m, mb=total / 1048576))
        _run_segments(
            session, url_provider, part_paths, bounds, total,
            on_progress=on_progress, on_rate_bytes=on_rate_bytes,
            on_segments=on_segments, on_segment_progress=on_segment_progress,
            should_stop=should_stop, should_pause=should_pause,
            log=log, thread_factory=thread_factory,
        )

    # All segments are now full -> concatenate, promote, clean up.
    return _concatenate(dest_part, dest_final, total, m)


def _run_segments(session, url_provider, part_paths, bounds, total, *,
                  on_progress, on_rate_bytes, on_segments, on_segment_progress,
                  should_stop, should_pause, log, thread_factory) -> None:
    """Download every not-yet-complete segment concurrently; raise on stop/error."""
    m = len(part_paths)
    # Seed each segment's accounting with its already-on-disk size so resumed
    # bytes are NOT counted as freshly downloaded by the rate meter.
    seg_done = [p.stat().st_size if p.exists() else 0 for p in part_paths]
    agg_lock = threading.Lock()
    exc: list[BaseException | None] = [None] * m

    def report(k: int, done: int) -> None:
        with agg_lock:
            delta = done - seg_done[k]
            if delta < 0:
                delta = 0
            seg_done[k] = done
            total_done = sum(seg_done)
        if delta and on_rate_bytes:
            on_rate_bytes(delta)
        if on_segment_progress:
            on_segment_progress(k, done)
        if on_progress:
            on_progress(total_done, total)

    # Tell the UI the block layout, then seed each block's resumed bytes, then
    # the aggregate total — all up front, before any worker thread starts.
    if on_segments:
        on_segments([end - start + 1 for (start, end) in bounds])
    if on_segment_progress:
        for k in range(m):
            on_segment_progress(k, seg_done[k])
    if on_progress:
        with agg_lock:
            on_progress(sum(seg_done), total)

    def worker(k: int) -> None:
        start, end = bounds[k]
        try:
            downloader.download_file(
                session, url_provider, part_paths[k], part_paths[k],
                on_progress=lambda done, _t: report(k, done),
                should_stop=should_stop, log=log,
                range_start=start, range_end=end, rename_on_done=False,
            )
        except BaseException as e:  # noqa: BLE001 - captured & re-raised by orchestrator
            exc[k] = e

    threads = []
    for k in range(m):
        # Skip segments already complete on disk (resume after partial run).
        start, end = bounds[k]
        if part_paths[k].exists() and part_paths[k].stat().st_size == (end - start + 1):
            continue
        th = thread_factory(target=worker, args=(k,), name=f"seg-{k}", daemon=True)
        threads.append(th)
        th.start()
    for th in threads:
        th.join()

    errors = [e for e in exc if e is not None]
    real = [e for e in errors if not isinstance(e, downloader.StoppedError)]
    if real:
        raise real[0]
    if any(isinstance(e, downloader.StoppedError) for e in errors):
        if should_pause and should_pause():
            raise downloader.PausedError()
        raise downloader.StoppedError()
