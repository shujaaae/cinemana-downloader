"""Entry point for the Cinemana downloader GUI.

Run with:  python app.py
(or double-click تشغيل.bat which uses pythonw to hide the console)

By default this launches the new "Aurora" web UI (pywebview). The original
Tkinter interface is still available with::

    python app.py --legacy-ui

The download engine and the CLI (``python -m cinemana ...``) are unchanged.
"""

import sys


def main() -> None:
    if "--legacy-ui" in sys.argv:
        from cinemana.gui import run
    else:
        from cinemana.gui_web import run
    run()


if __name__ == "__main__":
    main()
