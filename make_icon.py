"""Generate cinemana.ico from the app logo (run by build_exe.bat). Best-effort."""
from pathlib import Path
from PIL import Image, ImageFile  # Pillow 10.4 already installed

# The shipped logo PNG has a slightly truncated final IDAT (browsers tolerate it;
# the cost is ~the last pixel row, invisible at icon sizes). Let Pillow load it.
ImageFile.LOAD_TRUNCATED_IMAGES = True

src = Path("cinemana/gui_web/assets/cinemana-logo.png")
out = Path("cinemana.ico")
Image.open(src).convert("RGBA").save(
    out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
)
print("wrote", out)
