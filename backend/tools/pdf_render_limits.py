"""PDF raster limits shared by recognition and the isolated renderer."""
import math

MAX_RENDER_PIXELS = 16_000_000
MAX_RENDER_SIDE = 8192


def fit_full_page_scale(width: float, height: float, preferred: float) -> float:
    pixels_w, pixels_h = math.ceil(width * preferred), math.ceil(height * preferred)
    if max(pixels_w, pixels_h) <= MAX_RENDER_SIDE and pixels_w * pixels_h <= MAX_RENDER_PIXELS:
        return preferred
    fit = min(MAX_RENDER_SIDE / max(width, height), math.sqrt(MAX_RENDER_PIXELS / (width * height)))
    # Round the pixel box down. A subpixel margin keeps MuPDF's float matrix
    # from rounding the image one row or column past that box.
    return min((max(1, math.floor(edge * fit)) - 0.01) / edge for edge in (width, height))
