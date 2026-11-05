"""Uploads must be parsed before persistence/OCR; signatures alone are insufficient."""
from __future__ import annotations

import asyncio
import io
import struct
import threading
import zlib

from fastapi import HTTPException
from PIL import Image
import pymupdf as fitz
import pytest

from backend.tools import file_processing, image_evidence_worker
from backend.tools.media_validation import validate_media_content


def image_bytes(format="PNG", **options):
    with Image.new("RGB", (12, 8), (20, 40, 60)) as image, io.BytesIO() as output:
        image.save(output, format=format, **options)
        return output.getvalue()


def pdf_bytes(pages=1, encrypted=False):
    with fitz.open() as document:
        for _ in range(pages):
            document.new_page()
        return document.tobytes(**({"encryption": fitz.PDF_ENCRYPT_AES_256, "owner_pw": "owner", "user_pw": "user"} if encrypted else {}))


@pytest.mark.asyncio
@pytest.mark.parametrize("format,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp"), ("BMP", "image/bmp"), ("TIFF", "image/tiff")])
async def test_decodes_each_supported_image_format(format, mime):
    assert await validate_media_content(image_bytes(format), mime) == {"content_type": mime, "width": 12, "height": 8}


@pytest.mark.asyncio
@pytest.mark.parametrize("body,mime,code", [
    (b"%PDF-1.7\nnot a PDF", "application/pdf", "pdf_invalid"),
    (b"\x89PNG\r\n\x1a\n" + bytes(32), "image/png", "image_invalid"),
    (b"\xff\xd8\xff", "image/jpeg", "image_invalid"),
])
async def test_signature_only_is_not_valid_media(body, mime, code):
    with pytest.raises(HTTPException) as caught:
        await validate_media_content(body, mime)
    assert caught.value.status_code == 400
    assert caught.value.detail == {"code": code}


@pytest.mark.asyncio
async def test_actual_decoder_format_must_match_candidate():
    for body, mime, code in [(image_bytes(), "application/pdf", "pdf_invalid"), (image_bytes("JPEG"), "image/png", "image_format_mismatch")]:
        with pytest.raises(HTTPException) as caught:
            await validate_media_content(body, mime)
        assert caught.value.detail == {"code": code}


@pytest.mark.asyncio
async def test_pdf_validity_encryption_and_per_caller_page_limit():
    body = pdf_bytes(101)
    assert await validate_media_content(body, "application/pdf") == {"content_type": "application/pdf", "page_count": 101}
    with pytest.raises(HTTPException) as caught:
        await validate_media_content(body, "application/pdf", max_pdf_pages=100)
    assert caught.value.status_code == 413
    assert caught.value.detail == {"code": "pdf_page_limit_exceeded"}
    with pytest.raises(HTTPException) as caught:
        await validate_media_content(pdf_bytes(encrypted=True), "application/pdf")
    assert caught.value.detail == {"code": "pdf_encrypted"}
    assert (await validate_media_content(b"preamble\n" + pdf_bytes(), "application/pdf"))["page_count"] == 1


@pytest.mark.asyncio
async def test_fully_decodes_and_rejects_truncated_pixels():
    body = image_bytes("JPEG")
    with pytest.raises(HTTPException) as caught:
        await validate_media_content(body[:-10], "image/jpeg")
    assert caught.value.detail == {"code": "image_invalid"}


def test_inspection_reuses_decoder_without_pixel_conversion_or_encoding(monkeypatch):
    monkeypatch.setattr(image_evidence_worker, "png_payload", lambda *_args, **_kwargs: pytest.fail("validation must not encode PNG"))
    monkeypatch.setattr(image_evidence_worker, "_clean_rgb", lambda *_args, **_kwargs: pytest.fail("validation must not copy pixels"))
    assert image_evidence_worker.inspect_image(image_bytes(), "image/png") == {"content_type": "image/png", "width": 12, "height": 8}


def test_pixel_limit_precedes_decoding(monkeypatch):
    body = bytearray(image_bytes())
    body[16:24] = struct.pack(">II", 8193, 8)
    body[29:33] = struct.pack(">I", zlib.crc32(body[12:29]) & 0xFFFFFFFF)
    from PIL import PngImagePlugin
    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", lambda *_args: pytest.fail("oversized image must fail before load"))
    with pytest.raises(image_evidence_worker.ImageFailure) as caught:
        image_evidence_worker.inspect_image(bytes(body), "image/png")
    assert caught.value.code == "image_pixel_limit_exceeded"


@pytest.mark.asyncio
async def test_rejects_multiframe_image():
    with Image.new("RGB", (12, 8), "white") as first, Image.new("RGB", (12, 8), "black") as second, io.BytesIO() as output:
        first.save(output, format="TIFF", save_all=True, append_images=[second])
        with pytest.raises(HTTPException) as caught:
            await validate_media_content(output.getvalue(), "image/tiff")
    assert caught.value.detail == {"code": "image_multiframe_unsupported"}


@pytest.mark.asyncio
async def test_timeout_reaps_worker_and_releases_shared_slot(monkeypatch, tmp_path):
    worker = tmp_path / "slow_worker.py"
    worker.write_text("import time\ntime.sleep(60)\n")
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(file_processing, "_PDF_WORKER_PATH", worker)
    monkeypatch.setattr(file_processing, "_PDF_EXTRACTION_SLOTS", slots)
    processes = []
    original = asyncio.create_subprocess_exec
    async def launch(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    with pytest.raises(HTTPException) as caught:
        await validate_media_content(b"%PDF-1.7", "application/pdf", timeout_seconds=0.05)
    assert caught.value.status_code == 408
    assert caught.value.detail == {"code": "media_inspection_timeout"}
    assert processes and all(process.returncode is not None for process in processes)
    assert slots.acquire(blocking=False)
    slots.release()


@pytest.mark.asyncio
async def test_cancellation_during_launch_reaps_before_releasing_slot(monkeypatch, tmp_path):
    worker = tmp_path / "slow_worker.py"
    worker.write_text("import time\ntime.sleep(60)\n")
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(file_processing, "_PDF_WORKER_PATH", worker)
    monkeypatch.setattr(file_processing, "_PDF_EXTRACTION_SLOTS", slots)
    started = asyncio.Event()
    release = asyncio.Event()
    processes = []
    original = asyncio.create_subprocess_exec
    async def launch(*args, **kwargs):
        started.set()
        await release.wait()
        process = await original(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(validate_media_content(b"%PDF-1.7", "application/pdf"))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not slots.acquire(blocking=False)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes and all(process.returncode is not None for process in processes)
    assert slots.acquire(blocking=False)
    slots.release()


@pytest.mark.asyncio
async def test_worker_protocol_is_bounded_and_does_not_echo_content(monkeypatch, tmp_path):
    worker = tmp_path / "bad_worker.py"
    worker.write_text('import sys\nsys.stdout.write("PRIVATE" * 1000)\n')
    monkeypatch.setattr(file_processing, "_PDF_WORKER_PATH", worker)
    with pytest.raises(HTTPException) as caught:
        await validate_media_content(b"%PDF-1.7", "application/pdf")
    assert caught.value.detail == {"code": "media_inspection_failed"}


@pytest.mark.asyncio
async def test_busy_and_missing_parser_are_clear_infrastructure_errors(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(file_processing, "_PDF_EXTRACTION_SLOTS", slots)
    with pytest.raises(HTTPException) as caught:
        await validate_media_content(b"%PDF-1.7", "application/pdf")
    assert caught.value.status_code == 429
    assert caught.value.detail == {"code": "media_inspection_busy"}
    monkeypatch.setattr(file_processing, "fitz", None)
    with pytest.raises(HTTPException) as caught:
        await validate_media_content(b"%PDF-1.7", "application/pdf")
    assert caught.value.status_code == 503
    assert caught.value.detail == {"code": "media_inspection_unavailable"}
