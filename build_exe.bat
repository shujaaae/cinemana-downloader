@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Generating icon ===
python make_icon.py
echo === Building (PyInstaller, onedir) ===
python -m PyInstaller cinemana.spec --clean --noconfirm
echo.
echo Done. Launch: "dist\Cinemana Downloader\Cinemana Downloader.exe"
pause
