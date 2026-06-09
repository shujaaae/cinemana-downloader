# PyInstaller spec for the Cinemana Downloader (Aurora pywebview UI), onedir.
# Build:  build_exe.bat   (or: python -m PyInstaller cinemana.spec --clean --noconfirm)
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas, binaries, hiddenimports = [], [], []

# (a) pywebview + CLR: collect EVERYTHING. The stock webview hook grabs only
#     webview/lib, but pywebview also globs webview/js/**/*.js at runtime for its
#     window.pywebview.api bridge — missing those => a dead UI. clr_loader/
#     pythonnet carry the native CLR loader + Python.Runtime DLLs.
for pkg in ("webview", "clr_loader", "pythonnet"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

# (b) The app's own web front-end — data files, not Python modules. Destination
#     mirrors the source tree so webview_host.run()'s
#     Path(__file__).parent / "index.html" resolves inside the bundle.
datas += collect_data_files("cinemana.gui_web",
                            includes=["*.html", "*.css", "*.js", "assets/*"])

# (c) Backends pywebview imports dynamically on Windows (EdgeChromium/WinForms).
hiddenimports += ["clr", "webview.platforms.edgechromium", "webview.platforms.winforms"]

ICON = "cinemana.ico" if Path("cinemana.ico").exists() else None

a = Analysis(
    ["app.py"], pathex=[], binaries=binaries, datas=datas,
    hiddenimports=hiddenimports,
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "cefpython3"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
          name="Cinemana Downloader", console=False, icon=ICON)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
               name="Cinemana Downloader")
