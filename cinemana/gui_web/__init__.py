"""Web (pywebview) front-end for the Cinemana downloader.

This package replaces the legacy Tkinter :mod:`cinemana.gui` with an HTML/CSS/JS
window (the "Aurora" design) hosted by `pywebview`. It is a **GUI replacement
only** — every download behaviour still lives in the untouched engine
(:mod:`cinemana.service` and friends). :mod:`cinemana.gui_web.webview_host`
bridges the JS front-end to :class:`cinemana.service.DownloadService`.
"""

from .webview_host import run

__all__ = ["run"]
