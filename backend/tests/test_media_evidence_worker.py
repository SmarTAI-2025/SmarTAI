from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys

from PIL import Image, ImageFile, PngImagePlugin
import pymupdf as fitz
import pytest

from backend.tools import image_evidence_worker as image_worker
from backend.tools import pdf_evidence_worker as worker


@pytest.fixture(autouse=True)
def restore_pillow_limits(monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", Image.MAX_IMAGE_PIXELS)
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", ImageFile.LOAD_TRUNCATED_IMAGES)


def encoded_image(mode="RGB", color=(20, 40, 60), size=(12, 8), format="PNG", **options):
    with Image.new(mode, size, color) as image, io.BytesIO() as output:
        image.save(output, format=format, **options)
        return output.getvalue()


def prepare(body, content_type="image/png", **fields):
    return json.loads(worker.response_bytes({"operation": "image_prepare", "content_type": content_type, **fields}, body))


def assert_image_error(value, code):
    assert value == {"contract": "smartai.image.evidence", "schema_version": 1,
                     "status": "error", "operation": "image_prepare", "code": code}


@pytest.mark.parametrize("orientation", range(1, 9))
def test_exif_orientation_has_explicit_raw_geometry_and_removes_metadata(orientation):
    exif = Image.Exif()
    exif[274], exif[270] = orientation, "PRIVATE_EXIF_DESCRIPTION"
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "PRIVATE_PNG_COMMENT")
    body = encoded_image(exif=exif, pnginfo=metadata, icc_profile=b"PRIVATE_ICC")
    value = prepare(body)
    assert value["status"] == "ok"
    assert value["source_sha256"] == hashlib.sha256(body).hexdigest()
    assert value["source_width"] == 12 and value["source_height"] == 8
    assert value["exif_orientation"] == orientation
    expected_size = (8, 12) if orientation >= 5 else (12, 8)
    assert (value["width"], value["height"]) == expected_size
    assert value["crop_box_pixels"] == [0, 0, *expected_size]
    assert value["region"] == value["effective_region"] == [0, 0, 1, 1]
    assert value["metadata_stripped"] is True and value["resampled"] is False
    result = base64.b64decode(value["payload_b64"])
    assert b"PRIVATE" not in result
    with Image.open(io.BytesIO(result)) as image:
        assert image.mode == "RGB" and image.info == {} and len(image.getexif()) == 0
        assert image.getpixel((0, 0)) == (20, 40, 60)


def test_orientation_one_avoids_full_exif_copy_and_crops_before_color_conversion(monkeypatch):
    from PIL import ImageOps

    monkeypatch.setattr(ImageOps, "exif_transpose", lambda *_: pytest.fail("orientation 1 needs no full transpose copy"))
    sizes = []
    clean_rgb = image_worker._clean_rgb

    def checked(image):
        sizes.append((image.size, image.mode))
        return clean_rgb(image)

    monkeypatch.setattr(image_worker, "_clean_rgb", checked)
    result = prepare(encoded_image(size=(100, 80)), region=[0.1, 0.2, 0.3, 0.5])
    assert result["status"] == "ok"
    assert sizes == [((20, 24), "RGB")]


@pytest.mark.parametrize("mode,color,expected", [
    ("RGBA", (255, 0, 0, 0), (255, 255, 255)),
    ("RGBA", (255, 0, 0, 128), (255, 127, 127)),
    ("LA", (0, 128), (127, 127, 127)),
    ("L", 45, (45, 45, 45)),
    ("1", 0, (0, 0, 0)),
])
def test_alpha_uses_white_visible_background_without_resampling(mode, color, expected):
    result = prepare(encoded_image(mode=mode, color=color))
    assert result["status"] == "ok" and result["alpha_background"] == "white"
    with Image.open(io.BytesIO(base64.b64decode(result["payload_b64"]))) as image:
        assert image.getpixel((0, 0)) == expected


def test_palette_transparency_is_flattened_without_losing_visible_palette_color():
    with Image.new("P", (2, 1)) as image, io.BytesIO() as output:
        image.putpalette([255, 0, 0, 0, 0, 255] + [0] * 762)
        image.putdata([0, 1])
        image.save(output, format="PNG", transparency=0)
        result = prepare(output.getvalue())
    with Image.open(io.BytesIO(base64.b64decode(result["payload_b64"]))) as image:
        assert list(image.getdata()) == [(255, 255, 255), (0, 0, 255)]


def test_cmyk_jpeg_is_explicit_rgb_conversion_not_lossy_reencoding():
    body = encoded_image(mode="CMYK", color=(0, 255, 255, 0), format="JPEG", quality=100)
    result = prepare(body, "image/jpeg")
    assert result["source_mode"] == "CMYK" and result["resampled"] is False
    with Image.open(io.BytesIO(base64.b64decode(result["payload_b64"]))) as image:
        assert image.format == "PNG" and image.getpixel((0, 0)) == (255, 0, 0)


def test_webp_source_can_produce_lossless_png():
    body = encoded_image(format="WEBP", lossless=True)
    result = prepare(body, "image/webp")
    with Image.open(io.BytesIO(base64.b64decode(result["payload_b64"]))) as image:
        assert image.getpixel((0, 0)) == (20, 40, 60)


def test_fractional_crop_reports_effective_pixel_edges():
    result = prepare(encoded_image(size=(11, 7)), region=[0.11, 0.21, 0.61, 0.81])
    assert result["crop_box_pixels"] == [1, 1, 7, 6]
    assert result["effective_region"] == pytest.approx([1 / 11, 1 / 7, 7 / 11, 6 / 7])
    assert (result["width"], result["height"]) == (6, 5)


@pytest.mark.parametrize("orientation", [0, 9, 65535])
def test_invalid_exif_orientation_fails_instead_of_guessing(orientation):
    exif = Image.Exif()
    exif[274] = orientation
    assert_image_error(prepare(encoded_image(exif=exif)), "image_orientation_invalid")


@pytest.mark.parametrize("body", [b"", b"PRIVATE_INVALID_IMAGE", b"\x89PNG\r\n\x1a\nPRIVATE"])
def test_invalid_image_has_only_safe_envelope(body):
    assert_image_error(prepare(body), "image_invalid")


def test_format_spoof_and_truncated_image_are_rejected():
    body = encoded_image()
    assert_image_error(prepare(body, "image/jpeg"), "image_format_mismatch")
    assert_image_error(prepare(body[:-12]), "image_invalid")
    jpeg = encoded_image(size=(100, 100), format="JPEG")
    assert_image_error(prepare(jpeg[:len(jpeg) // 2], "image/jpeg"), "image_invalid")


@pytest.mark.parametrize("format,mime", [("PNG", "image/png"), ("WEBP", "image/webp")])
def test_multiple_frames_cannot_silently_become_first_frame(format, mime):
    with Image.new("RGB", (12, 8), "red") as first, Image.new("RGB", (12, 8), "blue") as second, io.BytesIO() as output:
        first.save(output, format=format, save_all=True, append_images=[second], duration=100, loop=0)
        assert_image_error(prepare(output.getvalue(), mime), "image_multiframe_unsupported")


def test_sixteen_bit_grayscale_is_not_silently_reduced_to_eight_bits():
    assert_image_error(prepare(encoded_image(mode="I;16", color=500)), "image_mode_unsupported")


@pytest.mark.parametrize("constant,limit,code", [
    ("MAX_INPUT_BYTES", 5, "image_input_too_large"),
    ("MAX_PIXELS", 30, "image_pixel_limit_exceeded"),
    ("MAX_SIDE", 10, "image_pixel_limit_exceeded"),
    ("MAX_RESPONSE_BYTES", 40, "image_response_too_large"),
])
def test_image_resource_caps_never_return_partial_output(monkeypatch, constant, limit, code):
    body = encoded_image()
    monkeypatch.setattr(image_worker, constant, limit)
    assert_image_error(prepare(body), code)


def test_whole_envelope_limit_includes_base64_and_metadata(monkeypatch):
    monkeypatch.setattr(worker, "MAX_RESPONSE_BYTES", 200)
    assert_image_error(prepare(encoded_image()), "image_response_too_large")


def test_pixel_limit_happens_before_full_decode(monkeypatch):
    body = encoded_image(size=(100, 100))
    monkeypatch.setattr(image_worker, "MAX_PIXELS", 100)
    monkeypatch.setattr(Image.Image, "load", lambda *_: pytest.fail("oversized input cannot be decoded"))
    assert_image_error(prepare(body), "image_pixel_limit_exceeded")


def sheet_pdf():
    with fitz.open() as document:
        for number in range(1, 10):
            page = document.new_page(width=200, height=100)
            page.draw_rect(page.rect, fill=(number / 10, 0, 0), color=None)
            page.set_rotation(((number - 1) % 4) * 90)
        return document.tobytes()


def test_eight_page_sheet_has_exact_global_mapping_and_separate_page_boxes():
    result = json.loads(worker.response_bytes({"operation": "contact_sheet", "pages": list(range(2, 10)),
                                               "tile_long_edge": 128}, sheet_pdf()))
    assert result["status"] == "ok" and result["page_numbers"] == list(range(2, 10))
    assert len(result["tiles"]) == 8
    with Image.open(io.BytesIO(base64.b64decode(result["payload_b64"]))) as sheet:
        assert sheet.size == (result["width"], result["height"])
        for number, tile in zip(range(2, 10), result["tiles"]):
            assert tile["page_number"] == number and tile["rotation"] == ((number - 1) % 4) * 90
            frame, page = tile["tile_bbox_pixels"], tile["page_bbox_pixels"]
            assert page[1] >= frame[1] + 24
            assert tile["render_width"] == page[2] - page[0]
            assert tile["render_height"] == page[3] - page[1]
            center = sheet.getpixel(((page[0] + page[2]) // 2, (page[1] + page[3]) // 2))
            assert center[0] == pytest.approx(number / 10 * 255, abs=1) and center[1:] == (0, 0)


@pytest.mark.parametrize("fields", [{"pages": list(range(1, 10))}, {"pages": [2, 1]},
                                     {"pages": [1], "tile_long_edge": 1025}, {"pages": [1], "tile_long_edge": True}])
def test_contact_sheet_request_is_bounded(fields):
    result = json.loads(worker.response_bytes({"operation": "contact_sheet", **fields}, sheet_pdf()))
    assert result["status"] == "error" and result["code"] == "pdf_invalid_request"


def test_pdf_only_worker_does_not_import_pillow(tmp_path):
    directory = Path(worker.__file__).parent
    script = """
import builtins, json, sys
import pymupdf as fitz
sys.path.insert(0, sys.argv[1])
real_import = builtins.__import__
def restricted(name, *args, **kwargs):
    if name == 'PIL' or name.startswith('PIL.'):
        raise ImportError('Pillow deliberately absent for PDF-only operation')
    return real_import(name, *args, **kwargs)
builtins.__import__ = restricted
import pdf_evidence_worker
with fitz.open() as doc:
    doc.new_page()
    print(pdf_evidence_worker.response_bytes({'operation':'index'}, doc.tobytes()).decode())
"""
    process = subprocess.run([sys.executable, "-B", "-c", script, str(directory)], cwd=tmp_path,
                             capture_output=True, timeout=20)
    assert process.returncode == 0
    assert json.loads(process.stdout)["status"] == "ok"
