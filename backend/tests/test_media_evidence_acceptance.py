"""Independent pixel acceptance for local preparation, not OCR accuracy claims."""
from __future__ import annotations

import hashlib
import io

from PIL import Image, PngImagePlugin
import pymupdf as fitz
import pytest

from backend.tools.pdf_evidence import (
    ImagePrepareRequest, PdfContactSheetRequest, decode_image_payload,
    decode_pdf_payload, read_image_evidence, read_pdf_evidence,
)


COLORS = ((250, 10, 20), (20, 240, 30), (30, 40, 230),
          (240, 230, 20), (220, 20, 210), (10, 210, 220))


def corner_source(orientation):
    image = Image.new("RGB", (60, 40), "white")
    for y in range(40):
        for x in range(60):
            image.putpixel((x, y), COLORS[(y // 20) * 3 + x // 20])
    exif = Image.Exif()
    exif[274] = orientation
    exif[270] = "PRIVATE_IMAGE_DESCRIPTION"
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "PRIVATE_IMAGE_COMMENT")
    with io.BytesIO() as output:
        image.save(output, format="PNG", exif=exif, pnginfo=metadata)
        body = output.getvalue()
    image.close()
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize("orientation,corners", [
    (1, (0, 2, 3, 5)), (2, (2, 0, 5, 3)),
    (3, (5, 3, 2, 0)), (4, (3, 5, 0, 2)),
    (5, (0, 3, 2, 5)), (6, (3, 0, 5, 2)),
    (7, (5, 2, 3, 0)), (8, (2, 5, 0, 3)),
])
async def test_all_orientation_pixels_match_literal_ground_truth(orientation, corners):
    body = corner_source(orientation)
    result = await read_image_evidence(body, ImagePrepareRequest(content_type="image/png"))
    png = decode_image_payload(result)
    assert result.source_sha256 == hashlib.sha256(body).hexdigest()
    assert result.source_width == 60 and result.source_height == 40
    assert result.exif_orientation == orientation
    assert result.resampled is False and result.metadata_stripped is True
    assert b"PRIVATE_IMAGE" not in png
    with Image.open(io.BytesIO(png)) as actual:
        width, height = actual.size
        assert actual.size == ((40, 60) if orientation >= 5 else (60, 40))
        assert [actual.getpixel(point) for point in
                ((2, 2), (width - 3, 2), (2, height - 3), (width - 3, height - 3))] == [COLORS[i] for i in corners]
        assert not actual.getexif() and not actual.info
        # Independently map source pixel centers using the recorded edge transform.
        mx, my = result.source_to_oriented_matrix
        for index, (x, y) in enumerate(((10, 10), (30, 10), (50, 10), (10, 30), (30, 30), (50, 30))):
            ox = int(mx[0] * (x + 0.5) + mx[1] * (y + 0.5) + mx[2])
            oy = int(my[0] * (x + 0.5) + my[1] * (y + 0.5) + my[2])
            assert actual.getpixel((ox, oy)) == COLORS[index]


@pytest.mark.asyncio
async def test_fractional_crop_preserves_every_pixel_and_small_marks():
    width, height = 61, 43
    pixels = [(255, 255, 255)] * (width * height)
    # A one-pixel minus, decimal dot and exponent-like stroke near crop edges.
    for x, y in [(x, 12) for x in range(17, 25)] + [(38, 28), (45, 11), (45, 12)]:
        pixels[y * width + x] = (0, 0, 0)
    with Image.new("RGB", (width, height)) as image, io.BytesIO() as buffer:
        image.putdata(pixels)
        image.save(buffer, format="PNG")
        body = buffer.getvalue()
    result = await read_image_evidence(body, ImagePrepareRequest(
        content_type="image/png", region=(0.251, 0.241, 0.752, 0.764),
    ))
    assert result.crop_box_pixels == (15, 10, 46, 33)
    assert result.effective_region == pytest.approx((15 / 61, 10 / 43, 46 / 61, 33 / 43))
    assert result.width == 31 and result.height == 23
    with Image.open(io.BytesIO(decode_image_payload(result))) as output:
        expected = [pixels[y * width + x] for y in range(10, 33) for x in range(15, 46)]
        assert list(output.getdata()) == expected
        assert sum(pixel == (0, 0, 0) for pixel in output.getdata()) == 11


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,transparent,opaque", [
    ("RGB", (255, 0, 0), (0, 0, 0)), ("L", 0, 128),
])
async def test_png_color_key_transparency_uses_visible_white_background(mode, transparent, opaque):
    with Image.new(mode, (8, 4), transparent) as image, io.BytesIO() as buffer:
        image.paste(opaque, (4, 0, 8, 4))
        image.save(buffer, format="PNG", transparency=transparent)
        body = buffer.getvalue()
    result = await read_image_evidence(body, ImagePrepareRequest(content_type="image/png"))
    with Image.open(io.BytesIO(decode_image_payload(result))) as output:
        assert output.getpixel((1, 1)) == (255, 255, 255)
        assert output.getpixel((6, 1)) == ((128, 128, 128) if mode == "L" else (0, 0, 0))


@pytest.mark.asyncio
async def test_contact_sheet_global_pages_and_rotation_match_pixels():
    numbers = [2, 5, 8, 10]
    rotations = [0, 90, 180, 270]
    with fitz.open() as document:
        for number in range(1, 11):
            page = document.new_page(width=200, height=100)
            color = (number / 10, 0.2, 0.4)
            page.draw_rect(page.rect, fill=color, color=None)
            page.draw_rect(fitz.Rect(0, 0, 200, 50), fill=(0, 0, 0), color=None)
            page.set_rotation(rotations[numbers.index(number)] if number in numbers else 0)
        body = document.tobytes()
    result = await read_pdf_evidence(body, PdfContactSheetRequest(pages=numbers))
    assert result.page_numbers == numbers and result.total_pages == 10
    assert [tile.page_number for tile in result.tiles] == numbers
    assert [tile.rotation for tile in result.tiles] == rotations
    with Image.open(io.BytesIO(decode_pdf_payload(result))) as sheet:
        assert sheet.size == (result.width, result.height)
        for number, rotation, tile in zip(numbers, rotations, result.tiles):
            x0, y0, x1, y1 = tile.page_bbox_pixels
            assert y0 >= tile.tile_bbox_pixels[1] + 24
            x, y, w, h = x0, y0, x1 - x0, y1 - y0
            if rotation == 0:
                black, colored = (x + w // 2, y + h // 4), (x + w // 2, y + 3 * h // 4)
            elif rotation == 90:
                black, colored = (x + 3 * w // 4, y + h // 2), (x + w // 4, y + h // 2)
            elif rotation == 180:
                black, colored = (x + w // 2, y + 3 * h // 4), (x + w // 2, y + h // 4)
            else:
                black, colored = (x + w // 4, y + h // 2), (x + 3 * w // 4, y + h // 2)
            assert sheet.getpixel(black) == (0, 0, 0)
            assert sheet.getpixel(colored) == pytest.approx((number * 25.5, 51, 102), abs=1)
