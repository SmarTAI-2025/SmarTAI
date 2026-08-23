from __future__ import annotations

import io
import struct
import unicodedata
import zipfile
import zlib

import pytest
from fastapi import HTTPException

from backend.tools import file_processing
from backend.skills.ocr_ingest import OCRResult
from backend.tools.file_processing import (
    extract_files_from_archive,
    extract_raw_files_from_archive,
    extract_text_from_upload,
)

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


def _valid_png_1x1() -> bytes:
    def chunk(name: bytes, payload: bytes) -> bytes:
        body = name + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
    )


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
@pytest.mark.parametrize(
    "member_name",
    [
        "2025105468_张三_1401.txt",
        "2025105468_李四_1401.txt",
        "课程/日本語_答案.txt",
        "김민수_답안.txt",
        "résumé.txt",
    ],
)
async def test_extract_zip_repairs_utf8_name_without_utf8_flag(member_name):
    archive = _stored_zip(member_name.encode("utf-8"), b"answer: A\n")

    files = await extract_files_from_archive(archive, "students.zip")

    assert files == [
        {
            "filename": member_name,
            "content": "answer: A\n",
        }
    ]


def test_zip_name_repair_covers_every_assigned_cjk_ideograph():
    ranges = (
        (0x3400, 0xFB00),
        (0x20000, 0x2FA20),
        (0x30000, 0x323B0),
    )
    checked = 0

    for start, end in ranges:
        for codepoint in range(start, end):
            character = chr(codepoint)
            unicode_name = unicodedata.name(character, "")
            if not unicode_name.startswith(
                ("CJK UNIFIED IDEOGRAPH-", "CJK COMPATIBILITY IDEOGRAPH-")
            ):
                continue
            member_name = f"学生_{character}.txt"
            info = zipfile.ZipInfo(member_name.encode("utf-8").decode("cp437"))
            info.flag_bits = 0

            assert file_processing._repair_zip_member_name(info) == member_name
            checked += 1

    assert checked > 98_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "member_name",
    ["answer_1401.txt", "café.txt", "Müller.txt", "éö.txt"],
)
async def test_extract_zip_preserves_ascii_and_legacy_cp437_names(member_name):
    archive = _stored_zip(member_name.encode("cp437"), b"answer: A\n")

    files = await extract_files_from_archive(archive, "students.zip")

    assert files == [{"filename": member_name, "content": "answer: A\n"}]


@pytest.mark.asyncio
async def test_extract_zip_preserves_flagged_utf8_name():
    archive = _zip_bytes({"班级/李四_答案.txt": b"answer: A\n"})

    files = await extract_files_from_archive(archive, "students.zip")

    assert files == [{"filename": "班级/李四_答案.txt", "content": "answer: A\n"}]


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
async def test_extract_text_upload_fragmented_math_pdf_uses_vision_ocr(monkeypatch):
    ocr = FakeOCRSkill("$v = \\sqrt{2as}$")
    fragmented = """Q1 — Calculus
I = 1
2
Z 1
0
e^u du = 1
2
Q2 — Mechanics
v =
p
2as
sin 30 = 0.5
"""

    async def fake_payload(*_args, **_kwargs):
        return fragmented, 1

    monkeypatch.setattr(file_processing, "_extract_pdf_payload", fake_payload)
    text = await extract_text_from_upload(
        _blank_pdf(),
        "math-layout.pdf",
        ocr_skill=ocr,
        purpose="submissions",
    )

    assert text == "$v = \\sqrt{2as}$"
    assert len(ocr.calls) == 1
    assert ocr.calls[0]["purpose"] == "submissions"


@pytest.mark.asyncio
@pytest.mark.skipif(fitz is None, reason="PyMuPDF is not installed")
async def test_fragmented_native_pdf_without_vision_keeps_selectable_text(monkeypatch):
    fragmented = (
        "Q1\nI = 1\n2\nZ 1\n0\nQ2\nv =\np\n2as\nsin 30 = 0.5\n"
        + "Selectable explanatory prose remains available. " * 5
    )

    async def fake_payload(*_args, **_kwargs):
        return fragmented, 1

    monkeypatch.setattr(file_processing, "_extract_pdf_payload", fake_payload)
    text = await extract_text_from_upload(
        _blank_pdf(),
        "math-layout.pdf",
        purpose="submissions",
    )

    assert text == fragmented


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
        b"\x89PNG\r\n\x1a\nfake-image-payload",
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
        "student_001/page.png": b"\x89PNG\r\n\x1a\nfake-image-payload",
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
        await extract_text_from_upload(
            b"\x89PNG\r\n\x1a\nfake-image-payload",
            "student.png",
        )

    assert exc.value.status_code == 422
    assert exc.value.detail == {"code": "vision_provider_required"}


@pytest.mark.asyncio
async def test_unsupported_single_file_returns_clear_error():
    with pytest.raises(HTTPException) as exc:
        await extract_files_from_archive(b"binary", "answers.xlsx")

    assert exc.value.status_code == 415
    assert exc.value.detail == {"code": "submission_source_content_type_mismatch"}


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


def test_raw_archive_preserves_healthy_member_when_sibling_is_oversized(monkeypatch):
    monkeypatch.setattr(file_processing, "SUBMISSION_ARCHIVE_MAX_MEMBER_BYTES", 3)
    archive = _zip_bytes({"healthy.txt": b"ok", "oversized.txt": b"four"})

    sources = extract_raw_files_from_archive(archive, "students.zip")

    assert len(sources) == 2
    assert sources[0].filename == "healthy.txt"
    assert sources[0].content == b"ok"
    assert sources[0].pre_error_code is None
    assert sources[1].filename == "oversized.txt"
    assert sources[1].content is None
    assert sources[1].pre_error_code == "submission_archive_member_too_large"
    assert sources[1].failure_phase == "archive"
    assert sources[1].retryable is False


def test_empty_single_file_has_exact_source_read_failure():
    sources = extract_raw_files_from_archive(
        b"",
        "empty.txt",
        content_type="text/plain",
    )

    assert len(sources) == 1
    assert sources[0].content == b""
    assert sources[0].content_type == "application/octet-stream"
    assert sources[0].pre_error_code == "submission_source_empty"
    assert sources[0].failure_phase == "source_read"
    assert sources[0].retryable is False


def test_empty_archive_has_exact_failure_code():
    with pytest.raises(RuntimeError, match="submission_source_empty"):
        extract_raw_files_from_archive(
            b"",
            "empty.zip",
            content_type="application/zip",
        )


def test_raw_archive_enforces_actual_cumulative_expansion_limit(monkeypatch):
    monkeypatch.setattr(file_processing, "SUBMISSION_ARCHIVE_MAX_EXPANDED_BYTES", 3)
    archive = _zip_bytes({"first.txt": b"ok", "second.txt": b"ok"})

    with pytest.raises(RuntimeError, match="submission_archive_limit_exceeded"):
        extract_raw_files_from_archive(archive, "students.zip")


def test_raw_archive_preserves_healthy_member_when_sibling_path_is_unsafe():
    archive = _zip_bytes({"healthy.txt": b"ok", "../escape.txt": b"nope"})

    sources = extract_raw_files_from_archive(archive, "students.zip")

    assert len(sources) == 2
    assert sources[0].filename == "healthy.txt"
    assert sources[0].content == b"ok"
    assert sources[0].pre_error_code is None
    assert sources[1].filename == "escape.txt"
    assert sources[1].content is None
    assert sources[1].pre_error_code == "submission_archive_member_unsafe_path"
    assert sources[1].failure_phase == "archive"


def test_raw_archive_preserves_healthy_member_when_sibling_read_fails(monkeypatch):
    archive = _zip_bytes({"healthy.txt": b"ok", "broken.txt": b"unreadable"})
    original_open = zipfile.ZipFile.open

    def fail_one_member(self, name, mode="r", *args, **kwargs):
        member_name = name.filename if isinstance(name, zipfile.ZipInfo) else name
        if member_name == "broken.txt" and mode == "r":
            raise zipfile.BadZipFile("injected member read failure")
        return original_open(self, name, mode, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", fail_one_member)

    sources = extract_raw_files_from_archive(archive, "students.zip")

    assert len(sources) == 2
    assert sources[0].filename == "healthy.txt"
    assert sources[0].content == b"ok"
    assert sources[0].pre_error_code is None
    assert sources[1].filename == "broken.txt"
    assert sources[1].content is None
    assert sources[1].pre_error_code == "submission_archive_member_unreadable"
    assert sources[1].failure_phase == "archive"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "filename", "content_type"),
    [
        (b"plain text pretending to be an image", "student.png", "image/png"),
        (b"%PDF-1.4\n", "answer.txt", "text/plain"),
        (_valid_png_1x1(), "student.jpg", "image/png"),
    ],
)
async def test_upload_rejects_extension_or_declared_type_that_disagrees_with_bytes(
    body,
    filename,
    content_type,
):
    ocr = FakeOCRSkill()

    with pytest.raises(HTTPException) as exc:
        await extract_text_from_upload(
            body,
            filename,
            content_type=content_type,
            ocr_skill=ocr,
        )

    assert exc.value.status_code == 415
    assert exc.value.detail == {"code": "submission_source_content_type_mismatch"}
    assert ocr.calls == []


@pytest.mark.asyncio
async def test_octet_stream_with_real_png_uses_detected_image_type():
    ocr = FakeOCRSkill("OCR from content-sniffed image")

    text = await extract_text_from_upload(
        _valid_png_1x1(),
        "upload.bin",
        content_type="application/octet-stream",
        ocr_skill=ocr,
    )

    assert text == "OCR from content-sniffed image"
    assert len(ocr.calls) == 1
    assert ocr.calls[0]["images"][0].media_type == "image/png"
    assert ocr.calls[0]["images"][0].label == "upload.bin"
