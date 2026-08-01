from __future__ import annotations

import io
import struct
import zipfile
import zlib

import pytest
from fastapi import HTTPException

from backend.tools import file_processing
from backend.skills.ocr_ingest import OCRResult
from backend.tools.file_processing import extract_files_from_archive, extract_text_from_upload

try:
    import fitz
except ImportError:
    fitz = None


class FakeOCRSkill:
    def __init__(self, text: str = "OCR text"):
        self.text = text
        self.calls = []

    async def recognize_images(self, images, purpose):
        self.calls.append({"images": images, "purpose": purpose})
        return OCRResult(text=self.text, provider="fake:ocr")


def _stored_zip(raw_name: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(body) & 0xFFFFFFFF
    local = struct.pack(
        "<IHHHHHIIIHH",
        0x04034B50,
        20,
        0,
        0,
        0,
        0,
        crc,
        len(body),
        len(body),
        len(raw_name),
        0,
    )
    local_block = local + raw_name + body
    central = struct.pack(
        "<IHHHHHHIIIHHHHHII",
        0x02014B50,
        20,
        20,
        0,
        0,
        0,
        0,
        crc,
        len(body),
        len(body),
        len(raw_name),
        0,
        0,
        0,
        0,
        0,
        0,
    )
    central_block = central + raw_name
    end = struct.pack(
        "<IHHHHIIH",
        0x06054B50,
        0,
        0,
        1,
        1,
        len(central_block),
        len(local_block),
        0,
    )
    return local_block + central_block + end


def _pdf_with_text(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 72), text)
    return doc.tobytes()


def _blank_pdf(pages: int = 1) -> bytes:
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page(width=300, height=200)
    return doc.tobytes()


def _zip_bytes(items: dict[str, bytes]) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as zf:
        for name, body in items.items():
            zf.writestr(name, body)
    return bio.getvalue()


@pytest.mark.asyncio
async def test_extract_zip_repairs_gbk_name_decoded_as_cp437():
    raw_name = "PB20241669_卫六_作业2.txt".encode("gbk")
    archive = _stored_zip(raw_name, "姓名：卫六\n答案：A\n".encode("utf-8"))

    files = await extract_files_from_archive(archive, "students.zip")

    assert files == [
        {
            "filename": "PB20241669_卫六_作业2.txt",
            "content": "姓名：卫六\n答案：A\n",
        }
    ]


@pytest.mark.asyncio
async def test_extract_text_upload_txt_skips_ocr():
    ocr = FakeOCRSkill()

    text = await extract_text_from_upload(
        "hello\n".encode("utf-8"),
        "answer.txt",
        ocr_skill=ocr,
    )

    assert text == "hello\n"
    assert ocr.calls == []


@pytest.mark.asyncio
@pytest.mark.skipif(fitz is None, reason="PyMuPDF is not installed")
async def test_extract_text_upload_native_pdf_skips_ocr():
    ocr = FakeOCRSkill()
    body = _pdf_with_text("This native PDF has enough selectable text. " * 5)

    text = await extract_text_from_upload(
        body,
        "problems.pdf",
        ocr_skill=ocr,
        purpose="problems",
    )

    assert "native PDF" in text
    assert ocr.calls == []


@pytest.mark.asyncio
@pytest.mark.skipif(fitz is None, reason="PyMuPDF is not installed")
async def test_extract_text_upload_scanned_pdf_uses_ocr():
    ocr = FakeOCRSkill("OCR from scanned PDF")

    text = await extract_text_from_upload(
        _blank_pdf(),
        "scan.pdf",
        ocr_skill=ocr,
        purpose="problems",
    )

    assert text == "OCR from scanned PDF"
    assert len(ocr.calls) == 1
    assert ocr.calls[0]["purpose"] == "problems"
    assert ocr.calls[0]["images"][0].media_type == "image/png"
    assert ocr.calls[0]["images"][0].label == "scan.pdf page 1"


@pytest.mark.asyncio
async def test_extract_text_upload_image_uses_ocr():
    ocr = FakeOCRSkill("OCR from image")

    text = await extract_text_from_upload(
        b"not a real image but passed to fake OCR",
        "student.png",
        ocr_skill=ocr,
        purpose="submissions",
    )

    assert text == "OCR from image"
    assert len(ocr.calls) == 1
    assert ocr.calls[0]["purpose"] == "submissions"
    assert ocr.calls[0]["images"][0].media_type == "image/png"
    assert ocr.calls[0]["images"][0].label == "student.png"


@pytest.mark.asyncio
async def test_extract_archive_mixed_text_and_image_uses_same_pipeline():
    ocr = FakeOCRSkill("OCR page")
    archive = _zip_bytes({
        "student_001/answer.txt": "plain answer".encode("utf-8"),
        "student_001/page.png": b"fake image",
    })

    files = await extract_files_from_archive(
        archive,
        "students.zip",
        ocr_skill=ocr,
        purpose="submissions",
    )

    assert files == [
        {"filename": "student_001/answer.txt", "content": "plain answer"},
        {"filename": "student_001/page.png", "content": "OCR page"},
    ]
    assert len(ocr.calls) == 1


@pytest.mark.asyncio
async def test_image_without_ocr_skill_returns_clear_error():
    with pytest.raises(HTTPException) as exc:
        await extract_text_from_upload(b"fake image", "student.png")

    assert exc.value.status_code == 503
    assert "requires OCR" in exc.value.detail


@pytest.mark.asyncio
async def test_unsupported_single_file_returns_clear_error():
    with pytest.raises(HTTPException) as exc:
        await extract_files_from_archive(b"binary", "answers.xlsx")

    assert exc.value.status_code == 415
    assert "Unsupported file type" in exc.value.detail


@pytest.mark.asyncio
async def test_extract_7z_with_current_py7zr_api(tmp_path):
    py7zr = pytest.importorskip("py7zr")
    archive_path = tmp_path / "students.7z"
    with py7zr.SevenZipFile(archive_path, "w") as archive:
        archive.writestr("姓名：卫六\n答案：A\n", "PB20241669_卫六.txt")

    files = await extract_files_from_archive(
        archive_path.read_bytes(),
        archive_path.name,
    )

    assert files == [{
        "filename": "PB20241669_卫六.txt",
        "content": "姓名：卫六\n答案：A\n",
    }]


@pytest.mark.asyncio
async def test_archive_rejects_parent_path_member():
    archive = _zip_bytes({"../escape.txt": b"nope"})

    with pytest.raises(ValueError, match="Unsafe path"):
        await extract_files_from_archive(archive, "students.zip")


@pytest.mark.asyncio
async def test_archive_rejects_oversized_member(monkeypatch):
    monkeypatch.setattr(file_processing, "SUBMISSION_ARCHIVE_MAX_MEMBER_BYTES", 3)
    archive = _zip_bytes({"answer.txt": b"four"})

    with pytest.raises(ValueError, match="oversized"):
        await extract_files_from_archive(archive, "students.zip")
