"""Synthetic archive regressions; no uploaded student data or model calls."""
from __future__ import annotations

import io
import bz2
import struct
import tarfile
import zipfile
import zlib

import pytest

from backend.tools import file_processing as files


PDF = b"%PDF-1.7\nsynthetic archive member\n%%EOF\n"
FORMATS = ("zip", "zip-deflate", "rar", "7z", "tar", "tar.gz", "tgz", "tar.bz2", "tbz2")


@pytest.mark.parametrize("level", range(1, 10))
@pytest.mark.parametrize("suffix", [b"", b" is plain text\n", b"1AY&SY describes a block header\n"])
def test_submission_bzip_header_text_remains_a_single_text_source(level, suffix):
    body = f"BZh{level}".encode() + suffix
    sources = files.extract_raw_files_from_archive(body, "answer.txt", content_type="text/plain")
    assert len(sources) == 1
    assert sources[0].content_type == "text/plain"
    assert sources[0].content == body
    assert sources[0].pre_error_code is None


@pytest.mark.parametrize("level", range(1, 10))
@pytest.mark.parametrize("body", [b"", b"answer" * 1000])
def test_real_bzip_streams_are_detected_even_with_misleading_names(level, body):
    compressed = bz2.compress(body, compresslevel=level)
    assert files.inspect_upload_content(compressed, "upload.tar.bz2").content_type == "application/x-bzip2"
    disguised = files.inspect_upload_content(compressed, "answer.txt", "text/plain")
    assert disguised.content_type == "application/x-bzip2" and disguised.mismatch


def test_large_bzip_stream_is_detected_from_a_partial_probe():
    import random
    compressed = bz2.compress(random.Random(282).randbytes(100_000))
    assert len(compressed) > 4096
    assert files.inspect_upload_content(compressed, "upload.tbz2").content_type == "application/x-bzip2"


def test_invalid_bzip_binary_prefix_does_not_count_as_a_valid_signature():
    inspection = files.inspect_upload_content(b"BZh9\0invalid compressed stream", "upload.tbz2")
    assert inspection.content_type == "application/octet-stream" and inspection.mismatch


def archive_bytes(kind, entries):
    """Use actual archive encoders/parsers, including a tiny stored RAR4 writer."""
    buffer = io.BytesIO()
    if kind.startswith("zip"):
        compression = zipfile.ZIP_DEFLATED if kind == "zip-deflate" else zipfile.ZIP_STORED
        # DEFLATE with stored blocks reproduces literal PDF bytes in a compressed
        # entry, as observed in the original incident. It is still method 8.
        with zipfile.ZipFile(buffer, "w", compression=compression, compresslevel=0) as archive:
            for name, body in entries.items():
                archive.writestr(name, body)
    elif kind == "rar":
        def block(tag, flags, payload):
            header = struct.pack("<BHH", tag, flags, 7 + len(payload)) + payload
            return struct.pack("<H", zlib.crc32(header) & 0xFFFF) + header
        buffer.write(b"Rar!\x1a\x07\x00" + block(0x73, 0, b"\0" * 6))
        for name, body in entries.items():
            encoded = name.encode("ascii")
            directory = name.endswith("/")
            header = struct.pack("<IIBIIBBHI", len(body), len(body), 2,
                zlib.crc32(body), 0x50210000, 20, 0x30, len(encoded), 0x10 if directory else 0x20)
            buffer.write(block(0x74, 0x8000 | (0xE0 if directory else 0), header + encoded) + body)
        buffer.write(block(0x7B, 0, b""))
    elif kind == "7z":
        py7zr = pytest.importorskip("py7zr")
        with py7zr.SevenZipFile(buffer, "w", filters=[{"id": py7zr.FILTER_COPY}]) as archive:
            for name, body in entries.items():
                archive.writestr(body, name)
    else:
        mode = "w:gz" if kind in {"tar.gz", "tgz"} else "w:bz2" if kind in {"tar.bz2", "tbz2"} else "w"
        with tarfile.open(fileobj=buffer, mode=mode, format=tarfile.USTAR_FORMAT) as archive:
            for name, body in entries.items():
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE if name.endswith("/") else tarfile.REGTYPE
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue(), "submissions." + ("zip" if kind.startswith("zip") else kind)


@pytest.mark.parametrize("kind", FORMATS)
@pytest.mark.parametrize("depth", [0, 1, 3, 12])
def test_supported_archives_preserve_pdf_bytes_and_ordinary_directories(kind, depth):
    member = "folder/" * depth + "answer.pdf"
    body, name = archive_bytes(kind, {member: PDF})
    inspection = files.inspect_upload_content(body, name)
    assert inspection.content_type in files.ARCHIVE_CONTENT_TYPES
    assert not inspection.mismatch
    sources = files.extract_raw_files_from_archive(body, name)
    assert len(sources) == 1
    assert sources[0].filename == member
    assert sources[0].content == PDF
    assert sources[0].content_type == "application/pdf"
    assert sources[0].pre_error_code is None


@pytest.mark.parametrize("kind", ["zip", "zip-deflate", "rar", "7z", "tar"])
def test_container_signature_wins_over_literal_pdf_marker(kind):
    body, name = archive_bytes(kind, {"answer.pdf": PDF})
    assert b"%PDF-" in body[:1024]
    assert files.inspect_upload_content(body, name).content_type != "application/pdf"
    # Filename/MIME are cross-checks, not a way to disguise an archive as PDF.
    assert files.inspect_upload_content(body, "disguised.pdf", "application/pdf").mismatch


@pytest.mark.parametrize("outer", FORMATS)
@pytest.mark.parametrize("inner", ["zip", "rar", "7z", "tar.gz", "tar.bz2", "tar"])
def test_nested_archives_have_specific_failure_and_keep_healthy_siblings(outer, inner):
    nested, nested_name = archive_bytes(inner, {"inside.txt": b"must not be unpacked"})
    body, name = archive_bytes(outer, {"folder/" + nested_name: nested, "healthy.txt": b"answer"})
    sources = files.extract_raw_files_from_archive(body, name)
    assert len(sources) == 2
    failed, healthy = sources
    assert failed.filename == "folder/" + nested_name
    assert failed.content == nested
    assert failed.pre_error_code == "submission_archive_nested_unsupported"
    assert failed.failure_phase == "archive" and not failed.retryable
    assert healthy.filename == "healthy.txt" and healthy.content == b"answer"
    assert healthy.pre_error_code is None


@pytest.mark.parametrize("kind", [kind for kind in FORMATS if kind != "7z"])
def test_directories_only_are_distinct_from_empty_archives(kind):
    body, name = archive_bytes(kind, {"folder/": b"", "folder/deeper/": b""})
    with pytest.raises(RuntimeError, match="^submission_archive_directories_only$"):
        files.extract_raw_files_from_archive(body, name)
    empty, _ = archive_bytes(kind, {})
    assert files.extract_raw_files_from_archive(empty, name) == []


def test_7z_directory_metadata_does_not_become_a_student_file(tmp_path):
    py7zr = pytest.importorskip("py7zr")
    folder = tmp_path / "empty"
    folder.mkdir()
    buffer = io.BytesIO()
    with py7zr.SevenZipFile(buffer, "w") as archive:
        archive.write(folder, "empty")
    with pytest.raises(RuntimeError, match="^submission_archive_directories_only$"):
        files.extract_raw_files_from_archive(buffer.getvalue(), "empty.7z")


@pytest.mark.parametrize("kind", FORMATS)
def test_existing_archive_safety_limits_still_apply_with_directories(kind, monkeypatch):
    body, name = archive_bytes(kind, {"folder/answer.txt": b"answer", "healthy.txt": b"ok"})
    monkeypatch.setattr(files, "SUBMISSION_ARCHIVE_MAX_MEMBER_BYTES", 3)
    sources = files.extract_raw_files_from_archive(body, name)
    assert sources[0].pre_error_code == "submission_archive_member_too_large"
    assert sources[1].content == b"ok"
    monkeypatch.setattr(files, "SUBMISSION_ARCHIVE_MAX_FILES", 1)
    with pytest.raises(RuntimeError, match="submission_archive_limit_exceeded"):
        files.extract_raw_files_from_archive(body, name)


@pytest.mark.parametrize("header,filename,mime", [
    (b"\x89PNG\r\n\x1a\n", "image.png", "image/png"),
    (b"\xff\xd8\xff", "image.jpg", "image/jpeg"),
    (b"BM" + b"\0" * 24, "image.bmp", "image/bmp"),
    (b"II*\0", "image.tif", "image/tiff"),
    (b"RIFF\0\0\0\0WEBP", "image.webp", "image/webp"),
    (b"\x1f\x8b", "bundle.tgz", "application/gzip"),
    (bz2.compress(b"synthetic archive data"), "bundle.tbz2", "application/x-bzip2"),
])
def test_other_fixed_signatures_win_over_embedded_pdf_markers(header, filename, mime):
    inspection = files.inspect_upload_content(header + b"metadata %PDF-1.7\n", filename)
    assert inspection.content_type == mime and not inspection.mismatch


@pytest.mark.parametrize("prefix", [b"", b"\xef\xbb\xbf", b"\0 \t\r\n"])
def test_pdf_header_and_supported_preamble_still_detected(prefix):
    assert files.inspect_upload_content(prefix + PDF, "source.pdf", "application/x-pdf").content_type == "application/pdf"
    assert not files.inspect_upload_content(prefix + PDF, "source.pdf", "application/x-pdf").mismatch


@pytest.mark.asyncio
@pytest.mark.parametrize("name,mime,purpose", [("notes.txt", "text/plain", "submissions"), ("notes.md", "text/plain", "submissions"), ("notes.markdown", "text/markdown", "submissions"), ("tests.csv", "text/plain", "test_cases")])
async def test_text_quoting_pdf_marker_is_read_as_text(name, mime, purpose):
    # CSV remains a supported test material, but is no longer a submission format.
    body = b"Example: %PDF-1.7 is a PDF header.\n"
    assert await files.extract_text_from_upload(body, name, content_type=mime, purpose=purpose) == body.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("mime", ["application/json", "text/json", "text/plain", "application/octet-stream"])
async def test_existing_json_test_material_support_reads_text(mime):
    body = b'{"input": "%PDF-1.7", "expected_output": "PDF"}'
    assert await files.extract_text_from_upload(body, "tests.json", content_type=mime, purpose="test_cases") == body.decode()


@pytest.mark.asyncio
async def test_json_support_is_not_added_to_other_upload_purposes():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as caught:
        await files.extract_text_from_upload(b'{"a": 1}', "answer.json", purpose="submissions")
    assert caught.value.detail == {"code": "submission_source_unsupported"}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"BMP is a bitmap format.\n", b"BZh is a bzip2 signature.\n", b"x" * 257 + b"ustar" + b"x" * 300])
async def test_short_or_embedded_magic_words_do_not_turn_plain_text_into_binary(body):
    assert await files.extract_text_from_upload(body, "notes.txt", content_type="text/plain") == body.decode()


@pytest.mark.asyncio
async def test_actual_bmp_still_passes_detection_and_image_parser():
    from PIL import Image
    from backend.tools.pdf_evidence import ImagePrepareRequest, read_image_evidence
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(buffer, format="BMP")
    body = buffer.getvalue()
    inspection = files.inspect_upload_content(body, "image.bmp")
    assert inspection.content_type == "image/bmp" and not inspection.mismatch
    result = await read_image_evidence(body, ImagePrepareRequest(content_type="image/bmp"))
    assert result.width == 16 and result.height == 16
