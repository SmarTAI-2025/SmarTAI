"""
File processing tool: extract text files from archives (.zip, .rar, .7z, .tar.*)
or handle a single file.

Migrated from backend/utils.py with no behavior change — just relocated to
the tools/ namespace so it's discoverable as a "predefined tool" per docs §4.2.1.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import posixpath
import sys
import tarfile
import tempfile
import threading
import zipfile
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
SUBMISSION_ARCHIVE_MAX_FILES = 500
SUBMISSION_ARCHIVE_MAX_EXPANDED_BYTES = 100 * 1024 * 1024
SUBMISSION_ARCHIVE_MAX_MEMBER_BYTES = 5 * 1024 * 1024

TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".rst"}
IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
ARCHIVE_EXTENSIONS = (
    ".zip", ".rar", ".7z", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
)


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
                detail="Unable to decode file; please ensure UTF-8 or GBK encoding.",
            )


async def _extract_pdf_payload(
    pdf_bytes: bytes,
    *,
    max_pages: int = PDF_MAX_PAGES,
    max_characters: int = PDF_MAX_CHARACTERS,
    timeout_seconds: float = PDF_EXTRACTION_TIMEOUT_SECONDS,
) -> tuple[str, int]:
    """Extract PDF text in a killable subprocess with hard ceilings."""
    if fitz is None:
        raise HTTPException(
            status_code=501,
            detail="PDF processing requires 'PyMuPDF'; pip install PyMuPDF"
        )
    if not _PDF_EXTRACTION_SLOTS.acquire(blocking=False):
        raise HTTPException(status_code=429, detail={"code": "pdf_extraction_busy"})
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(_PDF_WORKER_PATH),
            str(max_pages),
            str(max_characters),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(pdf_bytes),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            logger.warning("PDF extraction timed out")
            raise HTTPException(
                status_code=408,
                detail={
                    "code": "pdf_extraction_timeout",
                    "timeout_seconds": timeout_seconds,
                },
            ) from exc
        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "pdf_extraction_failed"},
            ) from exc
        worker_status = payload.get("status")
        if worker_status == "ok":
            return str(payload.get("text", "")), int(payload.get("page_count", 0))
        if worker_status == "page_limit":
            raise HTTPException(
                status_code=413,
                detail={"code": "pdf_page_limit_exceeded", "max_pages": max_pages},
            )
        if worker_status == "character_limit":
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "pdf_character_limit_exceeded",
                    "max_characters": max_characters,
                },
            )
        raise HTTPException(status_code=400, detail={"code": "pdf_extraction_failed"})
    except HTTPException:
        raise
    except Exception as exc:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
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


def _require_ocr_skill(ocr_skill: OCRIngestSkill | None, filename: str) -> OCRIngestSkill:
    if ocr_skill is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"{filename} requires OCR, but no vision-capable provider is configured. "
                "Add a Gemini/OpenAI/Anthropic key that supports image input."
            ),
        )
    return ocr_skill


def _check_image_size(data: bytes, filename: str) -> None:
    if len(data) > settings.ocr_max_image_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"{filename} is too large for OCR "
                f"({len(data)} bytes > {settings.ocr_max_image_bytes} bytes)."
            ),
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
        raise HTTPException(status_code=422, detail=f"OCR returned empty text for {filename}.")
    return result.text


def _render_pdf_pages_for_ocr(pdf_bytes: bytes, filename: str) -> list[OCRImage]:
    if fitz is None:
        raise HTTPException(
            status_code=501,
            detail="PDF processing requires 'PyMuPDF'; pip install PyMuPDF"
        )
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            page_count = len(doc)
            if page_count > settings.ocr_max_pdf_pages:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"{filename} has {page_count} pages; OCR limit is "
                        f"{settings.ocr_max_pdf_pages} pages."
                    ),
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
) -> str:
    """Convert a supported upload into text.

    Text/native PDF stays on the cheap deterministic path. Images and scanned
    PDFs use the provided OCR ingest skill.
    """
    safe_name = filename or "upload"
    extension = _ext(safe_name)
    if reporter:
        await reporter._emit_message(f"Reading {safe_name}...")

    if extension in TEXT_EXTENSIONS:
        return await decode_text_bytes(file_bytes)

    if extension == ".pdf":
        if fitz is None:
            raise HTTPException(
                status_code=501,
                detail="PDF processing requires 'PyMuPDF'; pip install PyMuPDF"
            )
        text, page_count = await _extract_pdf_payload(file_bytes)

        if not _is_likely_scanned_pdf(text, page_count):
            return text

        if reporter:
            await reporter._emit_message(f"Detected scanned PDF: {safe_name}; rendering pages for OCR...")
        images = _render_pdf_pages_for_ocr(file_bytes, safe_name)
        text = await _ocr_images(
            images,
            filename=safe_name,
            purpose=purpose,
            ocr_skill=ocr_skill,
            reporter=reporter,
        )
        return text

    if extension in IMAGE_MEDIA_TYPES:
        _check_image_size(file_bytes, safe_name)
        image = OCRImage(data=file_bytes, media_type=IMAGE_MEDIA_TYPES[extension], label=safe_name)
        return await _ocr_images(
            [image],
            filename=safe_name,
            purpose=purpose,
            ocr_skill=ocr_skill,
            reporter=reporter,
        )

    raise HTTPException(
        status_code=415,
        detail=(
            f"Unsupported file type for {safe_name}. Supported: "
            ".txt, .md, .csv, .pdf, .jpg, .jpeg, .png, .webp, and archives."
        ),
    )


def _is_valid_file(name: str) -> bool:
    """Filter out OS junk files."""
    return not (name.startswith("__MACOSX") or ".DS_Store" in name)


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _looks_like_cp437_mojibake(text: str) -> bool:
    return any(0x2500 <= ord(ch) <= 0x259F for ch in text)


def _repair_zip_member_name(name: str) -> str:
    """Repair GBK zip member names that Python decoded as CP437 mojibake."""
    if _has_cjk(name) or not _looks_like_cp437_mojibake(name):
        return name
    try:
        repaired = name.encode("cp437").decode("gbk")
    except UnicodeError:
        return name
    return repaired if _has_cjk(repaired) else name


def _safe_member_name(name: str) -> str:
    """Return a stable relative member path or reject traversal/absolute input."""
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError("Unsafe path in submission archive")
    normalized = posixpath.normpath(normalized)
    if normalized in {"", "."} or normalized.startswith("../"):
        raise ValueError("Unsafe path in submission archive")
    return normalized


def _validate_archive_members(sizes: List[int]) -> None:
    if len(sizes) > SUBMISSION_ARCHIVE_MAX_FILES:
        raise ValueError("Submission archive contains too many files")
    if any(size < 0 or size > SUBMISSION_ARCHIVE_MAX_MEMBER_BYTES for size in sizes):
        raise ValueError("Submission archive contains an oversized file")
    if sum(sizes) > SUBMISSION_ARCHIVE_MAX_EXPANDED_BYTES:
        raise ValueError("Submission archive expands beyond the safe limit")


def _validate_extracted_member(data: bytes) -> None:
    if len(data) > SUBMISSION_ARCHIVE_MAX_MEMBER_BYTES:
        raise ValueError("Submission archive contains an oversized file")


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
                clean = _safe_member_name(_repair_zip_member_name(info.filename))
                data = zf.read(info.filename)
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
