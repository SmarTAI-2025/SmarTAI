"""
File processing tool: extract text files from archives (.zip, .rar, .7z, .tar.*)
or handle a single file.

Migrated from backend/utils.py with no behavior change — just relocated to
the tools/ namespace so it's discoverable as a "predefined tool" per docs §4.2.1.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import mimetypes
import posixpath
import re
import sys
import tarfile
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import List, Dict

try:
    import rarfile
except ImportError:
    rarfile = None

try:
    import py7zr
except ImportError:
    py7zr = None

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

from fastapi import HTTPException

from backend.config import settings
from backend.skills.ocr_ingest import OCRImage, OCRIngestSkill, OCRPurpose

logger = logging.getLogger(__name__)

PDF_MAX_PAGES = 100
PDF_MAX_CHARACTERS = 500_000
PDF_EXTRACTION_TIMEOUT_SECONDS = 10.0
PDF_EXTRACTION_MAX_WORKERS = 2
_PDF_EXTRACTION_SLOTS = threading.BoundedSemaphore(PDF_EXTRACTION_MAX_WORKERS)
_PDF_WORKER_PATH = Path(__file__).with_name("_pdf_worker.py")
BAIDU_OCR_MAX_PDF_PAGES = 500
BAIDU_OCR_MAX_IMAGE_SIDE = 8192
SUBMISSION_ARCHIVE_MAX_FILES = 500
SUBMISSION_ARCHIVE_MAX_EXPANDED_BYTES = 100 * 1024 * 1024
SUBMISSION_ARCHIVE_MAX_MEMBER_BYTES = 5 * 1024 * 1024
SUBMISSION_UPLOAD_MAX_BYTES = 100 * 1024 * 1024
MAX_SOURCE_FILENAME_CHARACTERS = 512

TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".rst"}
IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}
ARCHIVE_EXTENSIONS = (
    ".zip", ".rar", ".7z", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
)


class _ArchiveLimitExceeded(RuntimeError):
    pass


def _bounded_source_name(value: str) -> str:
    name = str(value or "upload.bin")
    if len(name) <= MAX_SOURCE_FILENAME_CHARACTERS:
        return name
    digest = hashlib.sha256(name.encode("utf-8", errors="replace")).hexdigest()[:16]
    suffix = PurePosixPath(name.replace("\\", "/")).suffix[:20]
    prefix_length = MAX_SOURCE_FILENAME_CHARACTERS - len(suffix) - len(digest) - 1
    return f"{name[:prefix_length]}~{digest}{suffix}"


@dataclass(frozen=True)
class RawUploadSource:
    """One original upload/member before text extraction or OCR."""

    filename: str
    content: bytes | None
    content_type: str
    pre_error_code: str | None = None
    failure_phase: str | None = None
    retryable: bool = False


@dataclass(frozen=True)
class ContentInspection:
    """Content-derived media type plus any declared-type conflict."""

    content_type: str
    declared_content_type: str
    mismatch: bool


_MIME_ALIASES = {
    "application/x-zip-compressed": "application/zip",
    "application/x-rar-compressed": "application/vnd.rar",
    "application/x-7z-compressed": "application/x-7z-compressed",
    "application/x-gzip": "application/gzip",
    "image/jpg": "image/jpeg",
    "text/x-markdown": "text/markdown",
}


def _normalized_content_type(value: str | None) -> str:
    normalized = (value or "").split(";", 1)[0].strip().lower()
    if not normalized:
        return "application/octet-stream"
    return _MIME_ALIASES.get(normalized, normalized)


def _looks_like_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("gbk")
        except UnicodeDecodeError:
            return False
    return all(character in "\t\n\r" or ord(character) >= 32 for character in text)


def _content_signature_type(data: bytes, filename: str) -> str:
    if not data:
        return "application/octet-stream"
    if b"%PDF-" in data[:1024]:
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "application/zip"
    if data.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")):
        return "application/vnd.rar"
    if data.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "application/x-7z-compressed"
    if data.startswith(b"\x1f\x8b"):
        return "application/gzip"
    if data.startswith(b"BZh"):
        return "application/x-bzip2"
    if len(data) > 262 and data[257:262] == b"ustar":
        return "application/x-tar"
    if _looks_like_text(data):
        guessed, _encoding = mimetypes.guess_type(filename)
        guessed_type = _normalized_content_type(guessed)
        return guessed_type if guessed_type.startswith("text/") else "text/plain"
    return "application/octet-stream"


def inspect_upload_content(
    content: bytes,
    filename: str,
    supplied: str | None = None,
) -> ContentInspection:
    """Inspect bytes; supplied MIME and filename are hints, never authority."""
    detected = _content_signature_type(content, filename)
    supplied_type = _normalized_content_type(supplied)
    guessed, _encoding = mimetypes.guess_type(filename)
    filename_type = _normalized_content_type(guessed)
    extension = _ext(filename)
    if extension in {".tar.gz", ".tgz"}:
        filename_type = "application/gzip"
    elif extension in {".tar.bz2", ".tbz2"}:
        filename_type = "application/x-bzip2"
    elif detected == "application/zip" and extension in {".docx", ".xlsx", ".pptx"}:
        # These formats are ZIP containers, but they are not submission
        # archives and remain unsupported until a dedicated parser exists.
        detected = filename_type
    declared = (
        supplied_type
        if supplied_type != "application/octet-stream"
        else filename_type
    )
    declared_hints = {
        value
        for value in (supplied_type, filename_type)
        if value != "application/octet-stream"
    }
    mismatch = any(value != detected for value in declared_hints)
    return ContentInspection(
        content_type=detected,
        declared_content_type=declared,
        mismatch=mismatch,
    )


def infer_upload_content_type(
    filename: str,
    supplied: str | None = None,
    content: bytes | None = None,
) -> str:
    if content is not None:
        return inspect_upload_content(content, filename, supplied).content_type
    if supplied and supplied.strip() and supplied != "application/octet-stream":
        return _normalized_content_type(supplied)
    guessed, _encoding = mimetypes.guess_type(filename)
    return _normalized_content_type(guessed)


async def decode_text_bytes(text_bytes: bytes) -> str:
    """Try UTF-8 then GBK; raise 400 if both fail."""
    try:
        return text_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return text_bytes.decode("gbk")
        except UnicodeDecodeError:
            raise HTTPException(
                status_code=400,
                detail={"code": "source_decode_failed"},
            )


async def _extract_pdf_payload(
    pdf_bytes: bytes,
    *,
    max_pages: int = PDF_MAX_PAGES,
    max_characters: int = PDF_MAX_CHARACTERS,
    timeout_seconds: float = PDF_EXTRACTION_TIMEOUT_SECONDS,
) -> tuple[str, int]:
    """Extract PDF text in-process with a timeout guard."""
    if fitz is None:
        raise HTTPException(
            status_code=501,
            detail={"code": "pdf_processing_unavailable"},
        )

    if not _PDF_EXTRACTION_SLOTS.acquire(blocking=False):
        raise HTTPException(status_code=429, detail={"code": "pdf_extraction_busy"})

    try:
        try:
            doc = await asyncio.wait_for(
                asyncio.to_thread(fitz.open, stream=pdf_bytes, filetype="pdf"),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("PDF extraction timed out during open")
            raise HTTPException(
                status_code=408,
                detail={"code": "pdf_extraction_timeout", "timeout_seconds": timeout_seconds},
            )

        try:
            if doc.page_count > max_pages:
                raise HTTPException(
                    status_code=413,
                    detail={"code": "pdf_page_limit_exceeded", "max_pages": max_pages},
                )

            parts: list[str] = []
            character_count = 0
            try:
                for page in doc:
                    text = await asyncio.wait_for(
                        asyncio.to_thread(page.get_text),
                        timeout=timeout_seconds,
                    )
                    character_count += len(text)
                    if character_count > max_characters:
                        raise HTTPException(
                            status_code=413,
                            detail={
                                "code": "pdf_character_limit_exceeded",
                                "max_characters": max_characters,
                            },
                        )
                    parts.append(text)
            except asyncio.TimeoutError:
                logger.warning("PDF extraction timed out during page read")
                raise HTTPException(
                    status_code=408,
                    detail={"code": "pdf_extraction_timeout", "timeout_seconds": timeout_seconds},
                )

            text = "".join(parts)
            return str(text), int(doc.page_count)
        finally:
            try:
                doc.close()
            except Exception:
                pass
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "PDF extraction failed; exception_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=400,
            detail={"code": "pdf_extraction_failed"},
        ) from exc
    finally:
        _PDF_EXTRACTION_SLOTS.release()


async def inspect_baidu_ocr_upload(
    file_bytes: bytes,
    filename: str,
    *,
    content_type: str | None = None,
    timeout_seconds: float = PDF_EXTRACTION_TIMEOUT_SECONDS,
) -> ContentInspection:
    """Inspect Baidu-bound PDF/image bytes in the existing killable worker.

    This performs only the provider contract's 500-page PDF and 8192-pixel
    image-edge checks. It never extracts untrusted content in the Web process.
    Other Baidu-supported task formats retain the Tool's fixed suffix/byte
    validation and are not expanded into a general document-format matrix.
    """
    safe_name = _bounded_source_name(filename)
    inspection = inspect_upload_content(file_bytes, safe_name, content_type)
    if inspection.mismatch:
        raise HTTPException(
            status_code=415,
            detail={"code": "submission_source_content_type_mismatch"},
        )
    suffix = _ext(safe_name)
    mode: str | None = None
    limit = 0
    if suffix == ".pdf":
        if inspection.content_type != "application/pdf":
            raise HTTPException(
                status_code=415,
                detail={"code": "ocr_input_invalid"},
            )
        mode = "inspect-pdf"
        limit = BAIDU_OCR_MAX_PDF_PAGES
    elif suffix in {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}:
        if not inspection.content_type.startswith("image/"):
            raise HTTPException(
                status_code=415,
                detail={"code": "ocr_input_invalid"},
            )
        mode = "inspect-image"
        limit = BAIDU_OCR_MAX_IMAGE_SIDE
    if mode is None:
        return inspection
    if fitz is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "media_inspection_unavailable"},
        )
    if not _PDF_EXTRACTION_SLOTS.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail={"code": "media_inspection_busy"},
        )
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(_PDF_WORKER_PATH),
            mode,
            str(limit),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(file_bytes),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise HTTPException(
                status_code=408,
                detail={"code": "media_inspection_timeout"},
            ) from exc
        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "media_inspection_failed"},
            ) from exc
        worker_status = payload.get("status")
        if worker_status == "page_limit":
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "pdf_page_limit_exceeded",
                    "max_pages": BAIDU_OCR_MAX_PDF_PAGES,
                },
            )
        if worker_status == "image_side_limit":
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "ocr_image_dimension_limit_exceeded",
                    "max_side": BAIDU_OCR_MAX_IMAGE_SIDE,
                },
            )
        if worker_status != "ok":
            raise HTTPException(
                status_code=400,
                detail={"code": "media_inspection_failed"},
            )
        return inspection
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        _PDF_EXTRACTION_SLOTS.release()


async def extract_text_from_pdf(
    pdf_bytes: bytes,
    *,
    max_pages: int = PDF_MAX_PAGES,
    max_characters: int = PDF_MAX_CHARACTERS,
    timeout_seconds: float = PDF_EXTRACTION_TIMEOUT_SECONDS,
) -> str:
    text, _ = await _extract_pdf_payload(
        pdf_bytes,
        max_pages=max_pages,
        max_characters=max_characters,
        timeout_seconds=timeout_seconds,
    )
    return text


def _ext(name: str) -> str:
    lower = name.lower()
    for suffix in (".tar.gz", ".tar.bz2"):
        if lower.endswith(suffix):
            return suffix
    return PurePosixPath(lower).suffix


def _is_likely_scanned_pdf(text: str, page_count: int) -> bool:
    compact_len = len("".join((text or "").split()))
    if compact_len < settings.ocr_text_min_chars:
        return True
    return page_count > 0 and (compact_len / page_count) < 30


_MATH_LAYOUT_SIGNAL_RE = re.compile(
    r"[=∫√Σ∥]|[A-Za-z0-9][²³ⁿ⁻⁺]|\b(?:sin|cos|tan|exp|sqrt|ker|rank)\b",
    re.IGNORECASE,
)
_SPLIT_CODE_MARKERS = frozenset({"import", "from", "def", "class", "function"})


def _is_likely_fragmented_math_pdf(text: str, purpose: OCRPurpose) -> bool:
    """Detect selectable PDFs whose visual formula/code layout was flattened.

    PyMuPDF is preferable for ordinary prose PDFs, but a TeX fraction, radical,
    integral, superscript, or syntax-highlighted code block can be emitted as
    several isolated text lines (for example ``v =`` / ``p`` / ``2as``).  That
    output is technically non-empty yet materially worse than vision OCR.  Keep
    this deliberately conservative and limited to math-bearing ingest purposes.
    """
    if purpose not in {"problems", "submissions", "reference"}:
        return False
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 6:
        return False
    very_short_lines = sum(len(line) <= 3 for line in lines)
    math_signals = len(_MATH_LAYOUT_SIGNAL_RE.findall(text or ""))
    split_code_markers = sum(line.lower() in _SPLIT_CODE_MARKERS for line in lines)
    return (
        math_signals >= 3
        and (very_short_lines >= 3 or split_code_markers >= 2)
    )


def _require_ocr_skill(ocr_skill: OCRIngestSkill | None, filename: str) -> OCRIngestSkill:
    if ocr_skill is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "vision_provider_required"},
        )
    return ocr_skill


def _check_image_size(data: bytes, filename: str) -> None:
    if len(data) > settings.ocr_max_image_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "submission_source_too_large",
                "max_bytes": settings.ocr_max_image_bytes,
            },
        )


async def _ocr_images(
    images: list[OCRImage],
    *,
    filename: str,
    purpose: OCRPurpose,
    ocr_skill: OCRIngestSkill | None,
    reporter=None,
) -> str:
    skill = _require_ocr_skill(ocr_skill, filename)
    if reporter:
        await reporter._emit_message(f"OCR recognizing {filename} ({len(images)} image/page(s))...")
    result = await skill.recognize_images(images, purpose)
    if reporter and result.warnings:
        for warning in result.warnings:
            await reporter._emit_message(f"OCR warning for {filename}: {warning}", level="warn")
    if not result.text.strip():
        raise HTTPException(status_code=422, detail={"code": "ocr_empty_result"})
    return result.text


def _render_pdf_pages_for_ocr(pdf_bytes: bytes, filename: str) -> list[OCRImage]:
    if fitz is None:
        raise HTTPException(
            status_code=501,
            detail={"code": "pdf_processing_unavailable"},
        )
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            page_count = len(doc)
            if page_count > settings.ocr_max_pdf_pages:
                raise HTTPException(
                    status_code=413,
                    detail={
                        "code": "pdf_page_limit_exceeded",
                        "max_pages": settings.ocr_max_pdf_pages,
                    },
                )
            scale = settings.ocr_render_dpi_scale
            matrix = fitz.Matrix(scale, scale)
            images: list[OCRImage] = []
            for idx, page in enumerate(doc):
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                data = pix.tobytes("png")
                _check_image_size(data, f"{filename} page {idx + 1}")
                images.append(OCRImage(
                    data=data,
                    media_type="image/png",
                    label=f"{filename} page {idx + 1}",
                ))
            return images
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "PDF rendering for OCR failed; exception_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=400,
            detail={"code": "pdf_ocr_render_failed"},
        ) from exc


async def extract_text_from_upload(
    file_bytes: bytes,
    filename: str,
    ocr_skill: OCRIngestSkill | None = None,
    purpose: OCRPurpose = "submissions",
    reporter=None,
    content_type: str | None = None,
) -> str:
    """Convert a supported upload into text.

    Ordinary text/native PDF stays on the cheap deterministic path. Images,
    scanned PDFs, and visibly fragmented math-layout PDFs use the provided OCR
    ingest skill when one is available.
    """
    safe_name = filename or "upload"
    inspection = inspect_upload_content(file_bytes, safe_name, content_type)
    if inspection.mismatch:
        raise HTTPException(
            status_code=415,
            detail={"code": "submission_source_content_type_mismatch"},
        )
    media_type = inspection.content_type
    if reporter:
        await reporter._emit_message(f"Reading {safe_name}...")

    if media_type.startswith("text/"):
        return await decode_text_bytes(file_bytes)

    if media_type == "application/pdf":
        if fitz is None:
            raise HTTPException(
                status_code=501,
                detail={"code": "pdf_processing_unavailable"},
            )
        text, page_count = await _extract_pdf_payload(file_bytes)

        scanned = _is_likely_scanned_pdf(text, page_count)
        fragmented_math = _is_likely_fragmented_math_pdf(text, purpose)
        if not scanned and not (fragmented_math and ocr_skill is not None):
            return text

        if reporter:
            reason = "scanned PDF" if scanned else "fragmented math/code layout"
            await reporter._emit_message(
                f"Detected {reason}: {safe_name}; rendering pages for vision OCR..."
            )
        images = _render_pdf_pages_for_ocr(file_bytes, safe_name)
        text = await _ocr_images(
            images,
            filename=safe_name,
            purpose=purpose,
            ocr_skill=ocr_skill,
            reporter=reporter,
        )
        return text

    if media_type in set(IMAGE_MEDIA_TYPES.values()):
        _check_image_size(file_bytes, safe_name)
        image = OCRImage(data=file_bytes, media_type=media_type, label=safe_name)
        return await _ocr_images(
            [image],
            filename=safe_name,
            purpose=purpose,
            ocr_skill=ocr_skill,
            reporter=reporter,
        )

    raise HTTPException(
        status_code=415,
        detail={"code": "submission_source_unsupported"},
    )


def _is_valid_file(name: str) -> bool:
    """Filter out OS junk files."""
    return not (name.startswith("__MACOSX") or ".DS_Store" in name)


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _looks_like_cp437_mojibake(text: str) -> bool:
    return any(0x2500 <= ord(ch) <= 0x259F for ch in text)


def _is_plausible_utf8_member_name(text: str) -> bool:
    """Accept a strict UTF-8 recovery only when it is printable and non-ASCII."""
    return bool(text) and any(ord(ch) > 127 for ch in text) and text.isprintable()


def _repair_zip_member_name(info: zipfile.ZipInfo) -> str:
    """Repair a ZIP member whose producer omitted its filename encoding flag.

    ZIP's legacy fallback is CP437, so ``zipfile`` has already decoded an
    unflagged byte name by the time it reaches us. Re-encoding that string is
    lossless and lets us prefer strict UTF-8 (the common broken-producer case)
    before applying the narrower legacy-GBK heuristic.
    """
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw_name = name.encode("cp437")
    except UnicodeError:
        return name

    if raw_name.isascii():
        return name

    try:
        repaired_utf8 = raw_name.decode("utf-8")
    except UnicodeError:
        repaired_utf8 = ""
    if _is_plausible_utf8_member_name(repaired_utf8):
        return repaired_utf8

    if not _looks_like_cp437_mojibake(name):
        return name
    try:
        repaired_gbk = raw_name.decode("gbk")
    except UnicodeError:
        return name
    if _has_cjk(repaired_gbk) and repaired_gbk.isprintable():
        return repaired_gbk
    return name


def _safe_member_name(name: str) -> str:
    """Return a stable relative member path or reject traversal/absolute input."""
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError("Unsafe path in submission archive")
    normalized = posixpath.normpath(normalized)
    if normalized in {"", "."} or normalized.startswith("../"):
        raise ValueError("Unsafe path in submission archive")
    return _bounded_source_name(normalized)


def _validate_archive_members(sizes: List[int]) -> None:
    if len(sizes) > SUBMISSION_ARCHIVE_MAX_FILES:
        raise _ArchiveLimitExceeded("submission_archive_limit_exceeded")
    if any(size < 0 for size in sizes) or sum(sizes) > SUBMISSION_ARCHIVE_MAX_EXPANDED_BYTES:
        raise _ArchiveLimitExceeded("submission_archive_limit_exceeded")


def _validate_extracted_member(data: bytes) -> None:
    if len(data) > SUBMISSION_ARCHIVE_MAX_MEMBER_BYTES:
        raise ValueError("Submission archive contains an oversized file")


def _read_archive_member(stream) -> bytes:
    data = stream.read(SUBMISSION_ARCHIVE_MAX_MEMBER_BYTES + 1)
    _validate_extracted_member(data)
    return data


def extract_raw_files_from_archive(
    file_bytes: bytes,
    filename: str,
    *,
    content_type: str | None = None,
) -> list[RawUploadSource]:
    """Unpack original bytes without performing OCR or text parsing.

    Keeping this boundary separate lets callers persist and account for every
    source before one file's OCR/provider failure can abort the rest of a batch.
    """
    sources: list[RawUploadSource] = []
    file_in_memory = io.BytesIO(file_bytes)
    lower = filename.lower()
    if not file_bytes:
        if lower.endswith(ARCHIVE_EXTENSIONS):
            raise RuntimeError("submission_source_empty")
        return [RawUploadSource(
            filename=_bounded_source_name(filename),
            content=file_bytes,
            content_type="application/octet-stream",
            pre_error_code="submission_source_empty",
            failure_phase="source_read",
            retryable=False,
        )]
    container_inspection = inspect_upload_content(file_bytes, filename, content_type)
    expanded_bytes = 0

    def member_label(raw_name: str, index: int) -> str:
        leaf = PurePosixPath(raw_name.replace("\\", "/")).name
        return _bounded_source_name(leaf or f"archive-member-{index + 1}")

    def add(clean: str, data: bytes) -> None:
        nonlocal expanded_bytes
        _validate_extracted_member(data)
        expanded_bytes += len(data)
        if expanded_bytes > SUBMISSION_ARCHIVE_MAX_EXPANDED_BYTES:
            raise _ArchiveLimitExceeded("submission_archive_limit_exceeded")
        inspection = inspect_upload_content(data, clean)
        sources.append(RawUploadSource(
            filename=clean,
            content=data,
            content_type=inspection.content_type,
            pre_error_code=(
                "submission_source_content_type_mismatch"
                if inspection.mismatch else None
            ),
            failure_phase="source_read" if inspection.mismatch else None,
        ))

    def add_failure(raw_name: str, index: int, code: str) -> None:
        sources.append(RawUploadSource(
            filename=member_label(raw_name, index),
            content=None,
            content_type=container_inspection.content_type,
            pre_error_code=code,
            failure_phase="archive",
            retryable=False,
        ))

    archive_suffix = lower.endswith(ARCHIVE_EXTENSIONS)
    if archive_suffix and container_inspection.mismatch:
        return [RawUploadSource(
            filename=_bounded_source_name(filename),
            content=file_bytes,
            content_type=container_inspection.content_type,
            pre_error_code="submission_source_content_type_mismatch",
            failure_phase="source_read",
        )]

    if lower.endswith(".zip"):
        with zipfile.ZipFile(file_in_memory, "r") as archive:
            members = [
                item for item in archive.infolist()
                if not item.is_dir() and _is_valid_file(item.filename)
            ]
            _validate_archive_members([item.file_size for item in members])
            for index, item in enumerate(members):
                repaired_name = _repair_zip_member_name(item)
                try:
                    clean = _safe_member_name(repaired_name)
                except (TypeError, ValueError):
                    add_failure(repaired_name, index, "submission_archive_member_unsafe_path")
                    continue
                if item.file_size > SUBMISSION_ARCHIVE_MAX_MEMBER_BYTES:
                    add_failure(clean, index, "submission_archive_member_too_large")
                    continue
                try:
                    with archive.open(item, "r") as extracted:
                        add(clean, _read_archive_member(extracted))
                except _ArchiveLimitExceeded:
                    raise
                except Exception:
                    add_failure(clean, index, "submission_archive_member_unreadable")
        return sources

    if lower.endswith(".rar"):
        if rarfile is None:
            raise ValueError("Processing .rar files requires rarfile")
        try:
            with rarfile.RarFile(file_in_memory, "r") as archive:
                members = [
                    item for item in archive.infolist()
                    if not item.is_dir() and _is_valid_file(item.filename)
                ]
                _validate_archive_members([item.file_size for item in members])
                for index, item in enumerate(members):
                    try:
                        clean = _safe_member_name(item.filename)
                    except (TypeError, ValueError):
                        add_failure(item.filename, index, "submission_archive_member_unsafe_path")
                        continue
                    if item.file_size > SUBMISSION_ARCHIVE_MAX_MEMBER_BYTES:
                        add_failure(clean, index, "submission_archive_member_too_large")
                        continue
                    try:
                        with archive.open(item) as extracted:
                            add(clean, _read_archive_member(extracted))
                    except _ArchiveLimitExceeded:
                        raise
                    except Exception:
                        add_failure(clean, index, "submission_archive_member_unreadable")
        except rarfile.UNRARError as exc:
            raise RuntimeError("submission_archive_invalid") from exc
        return sources

    if lower.endswith(".7z"):
        if py7zr is None:
            raise ValueError("Processing .7z files requires py7zr")
        with py7zr.SevenZipFile(file_in_memory, "r") as archive:
            members = [
                item for item in archive.list()
                if item.is_file and not item.is_symlink and _is_valid_file(item.filename)
            ]
        _validate_archive_members([int(item.uncompressed) for item in members])
        with tempfile.TemporaryDirectory(prefix="smartai-submissions-") as temp_dir:
            root = Path(temp_dir).resolve()
            for index, item in enumerate(members):
                try:
                    clean = _safe_member_name(item.filename)
                except (TypeError, ValueError):
                    add_failure(item.filename, index, "submission_archive_member_unsafe_path")
                    continue
                if int(item.uncompressed) > SUBMISSION_ARCHIVE_MAX_MEMBER_BYTES:
                    add_failure(clean, index, "submission_archive_member_too_large")
                    continue
                try:
                    with py7zr.SevenZipFile(io.BytesIO(file_bytes), "r") as member_archive:
                        member_archive.extract(path=root, targets=[item.filename])
                    extracted = (root / clean).resolve()
                    extracted.relative_to(root)
                    if not extracted.is_file() or extracted.is_symlink():
                        raise ValueError("unsafe extracted member")
                    extracted.chmod(0o600)
                    with extracted.open("rb") as handle:
                        add(clean, _read_archive_member(handle))
                except _ArchiveLimitExceeded:
                    raise
                except Exception:
                    add_failure(clean, index, "submission_archive_member_unreadable")
        return sources

    if lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2")):
        with tarfile.open(fileobj=file_in_memory, mode="r:*") as archive:
            members = [
                item for item in archive.getmembers()
                if item.isfile() and _is_valid_file(item.name)
            ]
            _validate_archive_members([item.size for item in members])
            for index, item in enumerate(members):
                try:
                    clean = _safe_member_name(item.name)
                except (TypeError, ValueError):
                    add_failure(item.name, index, "submission_archive_member_unsafe_path")
                    continue
                if item.size > SUBMISSION_ARCHIVE_MAX_MEMBER_BYTES:
                    add_failure(clean, index, "submission_archive_member_too_large")
                    continue
                try:
                    extracted = archive.extractfile(item)
                    if extracted is None:
                        raise ValueError("archive member is unreadable")
                    with extracted:
                        add(clean, _read_archive_member(extracted))
                except _ArchiveLimitExceeded:
                    raise
                except Exception:
                    add_failure(clean, index, "submission_archive_member_unreadable")
        return sources

    return [RawUploadSource(
        filename=_bounded_source_name(filename),
        content=file_bytes,
        content_type=container_inspection.content_type,
        pre_error_code=(
            "submission_source_content_type_mismatch"
            if container_inspection.mismatch else None
        ),
        failure_phase="source_read" if container_inspection.mismatch else None,
    )]


async def extract_files_from_archive(
    file_bytes: bytes,
    filename: str,
    ocr_skill: OCRIngestSkill | None = None,
    purpose: OCRPurpose = "submissions",
    reporter=None,
) -> List[Dict[str, str]]:
    """
    Extract supported files from an archive (zip/rar/7z/tar.*) or wrap a single
    file into the same [{"filename": ..., "content": ...}] format.
    """
    files_data: List[Dict[str, str]] = []
    file_in_memory = io.BytesIO(file_bytes)
    lower = filename.lower()

    if lower.endswith(".zip"):
        with zipfile.ZipFile(file_in_memory, "r") as zf:
            valid = [i for i in zf.infolist() if not i.is_dir() and _is_valid_file(i.filename)]
            _validate_archive_members([i.file_size for i in valid])

            async def process(info):
                clean = _safe_member_name(_repair_zip_member_name(info))
                data = zf.read(info)
                _validate_extracted_member(data)
                content = await extract_text_from_upload(
                    data,
                    clean,
                    ocr_skill=ocr_skill,
                    purpose=purpose,
                    reporter=reporter,
                )
                return {"filename": clean, "content": content}

            files_data.extend(await _gather_limited([process(i) for i in valid]))

    elif lower.endswith(".rar"):
        if rarfile is None:
            raise ValueError("Processing .rar files requires 'rarfile'; pip install rarfile")
        try:
            with rarfile.RarFile(file_in_memory, "r") as rf:
                valid = [i for i in rf.infolist() if not i.is_dir() and _is_valid_file(i.filename)]
                _validate_archive_members([i.file_size for i in valid])

                async def process(info):
                    clean = _safe_member_name(info.filename)
                    data = rf.read(info.filename)
                    _validate_extracted_member(data)
                    content = await extract_text_from_upload(
                        data,
                        clean,
                        ocr_skill=ocr_skill,
                        purpose=purpose,
                        reporter=reporter,
                    )
                    return {"filename": clean, "content": content}

                files_data.extend(await _gather_limited([process(i) for i in valid]))
        except rarfile.UNRARError as e:
            raise RuntimeError(
                f"RAR extraction failed: {e}. Ensure 'unrar' CLI is installed on the server."
            )

    elif lower.endswith(".7z"):
        if py7zr is None:
            raise ValueError("Processing .7z files requires 'py7zr'; pip install py7zr")
        with py7zr.SevenZipFile(file_in_memory, "r") as szf:
            valid = [
                info
                for info in szf.list()
                if info.is_file and not info.is_symlink and _is_valid_file(info.filename)
            ]
            _validate_archive_members([int(info.uncompressed) for info in valid])
            safe_targets = [_safe_member_name(info.filename) for info in valid]
            with tempfile.TemporaryDirectory(prefix="smartai-submissions-") as temp_dir:
                root = Path(temp_dir).resolve()
                szf.extract(path=root, targets=safe_targets)

                async def process(item):
                    info, clean = item
                    extracted = (root / clean).resolve()
                    try:
                        extracted.relative_to(root)
                    except ValueError as exc:
                        raise ValueError("Unsafe path in submission archive") from exc
                    if not extracted.is_file() or extracted.is_symlink():
                        raise ValueError("Unsafe path in submission archive")
                    extracted.chmod(0o600)
                    data = extracted.read_bytes()
                    _validate_extracted_member(data)
                    content = await extract_text_from_upload(
                        data,
                        clean,
                        ocr_skill=ocr_skill,
                        purpose=purpose,
                        reporter=reporter,
                    )
                    return {"filename": clean, "content": content}

                files_data.extend(await _gather_limited([
                    process(item) for item in zip(valid, safe_targets)
                ]))

    elif lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2")):
        with tarfile.open(fileobj=file_in_memory, mode="r:*") as tf:
            valid = [m for m in tf.getmembers() if m.isfile() and _is_valid_file(m.name)]
            _validate_archive_members([m.size for m in valid])

            async def process(member):
                clean = _safe_member_name(member.name)
                obj = tf.extractfile(member)
                if obj is None:
                    return None
                data = obj.read()
                _validate_extracted_member(data)
                content = await extract_text_from_upload(
                    data,
                    clean,
                    ocr_skill=ocr_skill,
                    purpose=purpose,
                    reporter=reporter,
                )
                return {"filename": clean, "content": content}

            results = await _gather_limited([process(m) for m in valid])
            files_data.extend([r for r in results if r is not None])

    else:
        content = await extract_text_from_upload(
            file_bytes,
            filename,
            ocr_skill=ocr_skill,
            purpose=purpose,
            reporter=reporter,
        )
        files_data.append({"filename": filename, "content": content})

    return files_data


async def _gather_limited(coros):
    semaphore = asyncio.Semaphore(max(1, settings.ocr_concurrency))

    async def run(coro):
        async with semaphore:
            return await coro

    return await asyncio.gather(*[run(coro) for coro in coros])
