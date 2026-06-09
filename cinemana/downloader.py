"""Resumable download engine.

The core is :func:`download_file`, which downloads a single MP4 to a ``.part``
file and survives:

* network drops      -> exponential backoff + ``Range`` continuation
* expired signed URL -> ``url_provider()`` mints a fresh URL and we re-Range
* power loss         -> the ``.part`` on disk *is* the progress; re-run resumes
* server ignoring Range (200) -> restart that file from zero

Only when the full ``Content-Length`` is received do we ``os.replace`` the
``.part`` into the final ``.mp4`` — so a partial file is never mistaken for a
finished one.
"""

from __future__ import annotations

import errno
import os
import random
import time
from pathlib import Path
from typing import Callable

import requests

from .i18n import t
from .model import QualityVariant, SubtitleFile

CHUNK = 1 << 20            # 1 MiB
MAX_ATTEMPTS = 8
BACKOFF_BASE = 2.0
BACKOFF_CAP = 60.0
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60


class DownloadError(Exception):
    pass


class DiskFullError(DownloadError):
    pass


class ExpiredURLError(DownloadError):
    """Raised internally when the signed URL is rejected (401/403)."""


class StoppedError(DownloadError):
    """Raised when the caller requested a graceful stop mid-download."""


class PausedError(StoppedError):
    """Raised when the caller requested a graceful *pause* (vs. a full stop).

    A subclass of :class:`StoppedError` so existing ``except StoppedError``
    handlers still flush and stop; the service distinguishes the two by
    inspecting the episode's control flags (paused vs. stopped vs. cancelled).
    """


# -- quality selection --------------------------------------------------------

def select_variant(variants: list[QualityVariant], target_height: int) -> QualityVariant:
    """Pick the chosen quality, else the next-highest below it, else the best.

    Logs the fallback decision via the returned variant's height (caller
    compares against ``target_height`` to emit a warning).
    """
    avail = sorted(variants, key=lambda v: v.height, reverse=True)
    if not avail:
        raise DownloadError(t("err_no_quality"))
    for v in avail:
        if v.height == target_height:
            return v
    below = [v for v in avail if v.height < target_height]
    # avail is sorted high->low, so avail[0] is the highest available quality.
    return below[0] if below else avail[0]


def backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped at BACKOFF_CAP seconds."""
    base = min(BACKOFF_CAP, BACKOFF_BASE ** (attempt - 1))
    return base * (0.5 + random.random())


def _content_length(resp) -> int | None:
    cl = resp.headers.get("Content-Length")
    try:
        return int(cl) if cl is not None else None
    except (TypeError, ValueError):
        return None


def _total_from_content_range(resp) -> int | None:
    """Parse the total size from a ``Content-Range: bytes 100-199/12345`` header."""
    cr = resp.headers.get("Content-Range")
    if not cr or "/" not in cr:
        return None
    total = cr.rsplit("/", 1)[-1].strip()
    try:
        return int(total)
    except ValueError:
        return None


def _range_start(resp) -> int | None:
    cr = resp.headers.get("Content-Range")
    if not cr or " " not in cr or "-" not in cr:
        return None
    try:
        span = cr.split(" ", 1)[1].split("/", 1)[0]
        return int(span.split("-", 1)[0])
    except (ValueError, IndexError):
        return None


# -- core download ------------------------------------------------------------

def download_file(
    session: requests.Session,
    url_provider: Callable[[], str],
    dest_part: Path,
    dest_final: Path,
    *,
    on_progress: Callable[[int, int | None], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    log: Callable[[str], None] | None = None,
    range_start: int = 0,
    range_end: int | None = None,
    rename_on_done: bool = True,
) -> int:
    """Download to ``dest_part`` with resume, then atomically rename to final.

    ``url_provider`` returns a *fresh* signed URL each call. ``should_stop``
    lets the caller request a graceful stop (raises :class:`StoppedError`).
    Returns the final file size in bytes.

    Optional **byte window** (used by the segmented engine — defaults reproduce
    the original whole-file behaviour exactly):

    * ``range_start`` / ``range_end`` — absolute, inclusive remote byte range
      this ``.part`` maps to. With ``range_end`` set, the request uses a *closed*
      ``Range: bytes=start-end`` and the ``.part`` target size is the window
      length (independent of the server's reported total). The ``.part`` still
      holds only this window's bytes, so its on-disk size *is* this window's
      progress — the same "size-is-progress" invariant, per segment.
    * ``rename_on_done`` — when ``False`` (segments), skip the final
      ``os.replace``; the caller concatenates the parts itself.
    """
    dest_part.parent.mkdir(parents=True, exist_ok=True)
    log = log or (lambda *_: None)
    windowed = range_end is not None
    expected_len = (range_end - range_start + 1) if windowed else None
    # ``total`` is the target size of the *local* .part file. For a window it is
    # known up front; for the whole file it is learned from the server.
    total: int | None = expected_len
    attempts = 0
    logged_resume = False

    while True:
        attempts += 1
        existing = dest_part.stat().st_size if dest_part.exists() else 0
        if existing > 0 and not logged_resume:
            log(t("log_resume_offset", offset=range_start + existing))
            logged_resume = True

        # A window whose .part is already full (or overshot) needs no request.
        if windowed:
            if existing == expected_len:
                break
            if existing > expected_len:
                dest_part.unlink(missing_ok=True)
                existing = 0

        try:
            url = url_provider()  # fresh signed URL every attempt
            abs_start = range_start + existing
            if windowed:
                headers = {"Range": f"bytes={abs_start}-{range_end}"}
            elif existing > 0:
                headers = {"Range": f"bytes={abs_start}-"}
            else:
                headers = {}
            resp = session.get(
                url, headers=headers, stream=True,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), allow_redirects=True,
            )

            if resp.status_code in (401, 403):
                resp.close()
                raise ExpiredURLError()
            if resp.status_code == 416:
                # Requested range past end -> .part is already complete (or too long).
                remote_total = _total_from_content_range(resp)
                resp.close()
                if windowed:
                    if existing > expected_len:
                        dest_part.unlink(missing_ok=True)
                        continue
                    total = expected_len
                    break
                total = remote_total or existing
                if existing > total:
                    dest_part.unlink(missing_ok=True)
                    continue
                break
            resp.raise_for_status()

            if resp.status_code == 200:
                if windowed:
                    # The server ignored Range and is streaming the WHOLE file,
                    # not our window — a segment cannot use this body.
                    resp.close()
                    raise DownloadError(t("err_server_no_segments"))
                if existing > 0:
                    log(t("log_server_ignored_resume"))
                mode, existing = "wb", 0
                total = _content_length(resp)
            else:  # 206 Partial Content
                start = _range_start(resp)
                if start is not None and start != abs_start:
                    log(t("log_resume_mismatch"))
                    dest_part.unlink(missing_ok=True)
                    resp.close()
                    continue
                mode = "ab"
                if windowed:
                    total = expected_len
                else:
                    total = _total_from_content_range(resp)
                    if total is None:
                        cl = _content_length(resp)
                        total = existing + cl if cl is not None else None

            with resp, open(dest_part, mode) as f:
                if on_progress:
                    on_progress(existing, total)
                for chunk in resp.iter_content(CHUNK):
                    if should_stop and should_stop():
                        f.flush()
                        os.fsync(f.fileno())
                        raise StoppedError()
                    if not chunk:
                        continue
                    f.write(chunk)
                    existing += len(chunk)
                    if on_progress:
                        on_progress(existing, total)
                f.flush()
                os.fsync(f.fileno())

            if total is not None and existing > total:
                # Overshot somehow -> corrupt, restart clean.
                dest_part.unlink(missing_ok=True)
                if attempts >= MAX_ATTEMPTS:
                    raise DownloadError(t("err_size_mismatch_retries"))
                continue
            if total is not None and existing < total:
                # Stream ended early -> retry, resume from new .part size.
                if attempts >= MAX_ATTEMPTS:
                    raise DownloadError(t("err_partial_only", done=existing, total=total))
                time.sleep(backoff_delay(attempts))
                continue
            break  # success (total known & matched, or total unknown & stream ended)

        except StoppedError:
            raise
        except ExpiredURLError:
            log(t("log_url_expired"))
            if attempts >= MAX_ATTEMPTS:
                raise DownloadError(t("err_url_refresh_failed"))
            continue  # immediate retry, no backoff
        except requests.RequestException as exc:
            # NOTE: some of these (e.g. ChunkedEncodingError) are *also* OSError,
            # so this clause MUST come before the OSError handler below.
            if attempts >= MAX_ATTEMPTS:
                raise DownloadError(t("err_download_failed_attempts", n=attempts, err=exc)) from exc
            log(t("log_network_retry", etype=type(exc).__name__, n=attempts))
            time.sleep(backoff_delay(attempts))
            continue
        except OSError as exc:
            if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
                raise DiskFullError(t("err_disk_full")) from exc
            raise

    final_size = dest_part.stat().st_size if dest_part.exists() else 0
    if total is not None and final_size != total:
        raise DownloadError(t("err_final_size_mismatch", size=final_size, total=total))
    if not rename_on_done:
        # Segment finished: leave the .part in place; the caller concatenates.
        return final_size
    dest_final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(dest_part, dest_final)
    return final_size


# -- subtitles ----------------------------------------------------------------

def download_subtitle(session: requests.Session, sub: SubtitleFile, dest: Path,
                      timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) -> bool:
    """Download one small subtitle file. Returns True on success.

    Failures are non-fatal (caller should warn and continue).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    for attempt in range(1, 4):
        try:
            resp = session.get(sub.url, timeout=timeout)
            resp.raise_for_status()
            with open(tmp, "wb") as f:
                f.write(resp.content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dest)
            return True
        except (requests.RequestException, OSError):
            if attempt < 3:
                time.sleep(backoff_delay(attempt))
            continue
    tmp.unlink(missing_ok=True)
    return False
