"""Image decoding/preparation used only inside the isolated media worker."""
from __future__ import annotations

import base64
import hashlib
import io
import math
import warnings

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_SIDE = 8192
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


class ImageFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _BoundedBuffer(io.BytesIO):
    def __init__(self, limit: int, code: str):
        super().__init__()
        self.limit, self.code = limit, code

    def write(self, value):
        if self.tell() + len(value) > self.limit:
            raise ImageFailure(self.code)
        return super().write(value)


def png_payload(image, *, limit: int, code: str) -> str:
    # Callers provide a fresh RGB image, never a decoder-owned metadata container.
    if image.mode != "RGB" or image.info:
        raise ImageFailure(code)
    with _BoundedBuffer(limit * 3 // 4, code) as output:
        image.save(output, format="PNG")
        return base64.b64encode(output.getvalue()).decode("ascii")


def _clean_rgb(image):
    from PIL import Image

    clean = Image.new("RGB", image.size, "white")
    converted = mask = None
    try:
        if image.mode not in {"RGBA", "LA"} and "transparency" in image.info:
            converted = image.convert("RGBA")
            image = converted
        if image.mode in {"RGBA", "LA"}:
            mask = image.getchannel("A")
        clean.paste(image, (0, 0), mask)
        return clean
    except BaseException:
        clean.close()
        raise
    finally:
        if mask is not None:
            mask.close()
        if converted is not None:
            converted.close()


def _pixel_limit(width: int, height: int):
    if min(width, height) < 1 or max(width, height) > MAX_SIDE or width * height > MAX_PIXELS:
        raise ImageFailure("image_pixel_limit_exceeded")


def _orientation_matrix(orientation: int, width: int, height: int) -> list[list[int]]:
    # Pixel-edge coordinates: [visual_x, visual_y] = matrix @ [raw_x, raw_y, 1].
    return {
        1: [[1, 0, 0], [0, 1, 0]], 2: [[-1, 0, width], [0, 1, 0]],
        3: [[-1, 0, width], [0, -1, height]], 4: [[1, 0, 0], [0, -1, height]],
        5: [[0, 1, 0], [1, 0, 0]], 6: [[0, -1, height], [1, 0, 0]],
        7: [[0, -1, height], [-1, 0, width]], 8: [[0, 1, 0], [-1, 0, width]],
    }[orientation]


def prepare_image(body: bytes, request: dict) -> dict:
    from PIL import Image, ImageFile, ImageOps

    if not body:
        raise ImageFailure("image_invalid")
    if len(body) > MAX_INPUT_BYTES:
        raise ImageFailure("image_input_too_large")
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(body)) as probe:
                if probe.format != FORMATS[request["content_type"]]:
                    raise ImageFailure("image_format_mismatch")
                if getattr(probe, "n_frames", 1) != 1:
                    raise ImageFailure("image_multiframe_unsupported")
                _pixel_limit(*probe.size)
                probe.verify()
            with Image.open(io.BytesIO(body)) as original:
                raw_width, raw_height = original.size
                source_mode = original.mode
                if original.mode not in {"1", "L", "LA", "P", "RGB", "RGBA", "CMYK"}:
                    raise ImageFailure("image_mode_unsupported")
                orientation = original.getexif().get(274, 1)
                if type(orientation) is not int or orientation not in range(1, 9):
                    raise ImageFailure("image_orientation_invalid")
                original.load()
                oriented = ImageOps.exif_transpose(original) if orientation != 1 else original
                if oriented is not original:
                    original.close()
                try:
                    width, height = oriented.size
                    region = request["region"]
                    crop_box = [math.floor(region[0] * width), math.floor(region[1] * height),
                                math.ceil(region[2] * width), math.ceil(region[3] * height)]
                    _pixel_limit(crop_box[2] - crop_box[0], crop_box[3] - crop_box[1])
                    cropped = oriented if crop_box == [0, 0, width, height] else oriented.crop(crop_box)
                    if cropped is not oriented:
                        oriented.close()
                    try:
                        flattened = _clean_rgb(cropped)
                        cropped.close()
                        try:
                            payload = png_payload(flattened, limit=MAX_RESPONSE_BYTES,
                                                  code="image_response_too_large")
                        finally:
                            flattened.close()
                    finally:
                        cropped.close()
                    return {
                        "source_sha256": hashlib.sha256(body).hexdigest(),
                        "source_content_type": request["content_type"], "source_mode": source_mode,
                        "source_width": raw_width, "source_height": raw_height,
                        "exif_orientation": orientation,
                        "source_to_oriented_matrix": _orientation_matrix(orientation, raw_width, raw_height),
                        "oriented_width": width, "oriented_height": height,
                        "region": region, "effective_region": [crop_box[0] / width, crop_box[1] / height,
                                                                 crop_box[2] / width, crop_box[3] / height],
                        "crop_box_pixels": crop_box, "width": crop_box[2] - crop_box[0],
                        "height": crop_box[3] - crop_box[1], "content_type": "image/png",
                        "payload_b64": payload, "alpha_background": "white", "resampled": False,
                        "metadata_stripped": True,
                    }
                finally:
                    oriented.close()
    except ImageFailure:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageFailure("image_pixel_limit_exceeded") from None
    except (MemoryError, OverflowError):
        raise ImageFailure("image_pixel_limit_exceeded") from None
    except Exception:
        raise ImageFailure("image_invalid") from None
