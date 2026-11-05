"""Isolated PyMuPDF worker used by ``file_processing``.

The parent process owns timeout/concurrency policy and kills this process on a
deadline.  This module deliberately emits only a small UTF-8 JSON protocol and
never returns raw parser exceptions.
"""
from __future__ import annotations

import json
import sys

import pymupdf as fitz


def _write(payload: dict) -> None:
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _inspect_upload(content_type: str, max_pdf_pages: int) -> dict:
    """Identify and parse media in a bounded process, without OCR or rendering."""
    import image_evidence_worker

    # Parent deadlines/concurrency bound execution. Also cap decoder allocations
    # before opening hostile files; this limit applies only to this worker mode.
    try:
        import resource
        ceiling = 1024 * 1024 * 1024
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        limits = [ceiling] + [value for value in (soft, hard) if value >= 0]
        resource.setrlimit(resource.RLIMIT_AS, (min(limits), hard))
    except (ImportError, AttributeError):
        pass  # Non-POSIX platforms retain the parent's killable deadline.
    limit = 100 * 1024 * 1024 if content_type == "application/pdf" else image_evidence_worker.MAX_INPUT_BYTES
    body = sys.stdin.buffer.read(limit + 1)
    if len(body) > limit:
        return {"status": "error", "code": "pdf_input_too_large" if content_type == "application/pdf" else "image_input_too_large"}
    if content_type in image_evidence_worker.FORMATS:
        try:
            return {"status": "ok", **image_evidence_worker.inspect_image(body, content_type)}
        except image_evidence_worker.ImageFailure as exc:
            return {"status": "error", "code": exc.code}
        except (MemoryError, OverflowError, RecursionError):
            return {"status": "error", "code": "media_inspection_resource_limit"}
    if content_type != "application/pdf":
        return {"status": "error", "code": "media_inspection_failed"}
    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)
    try:
        with fitz.open(stream=body, filetype="pdf") as document:
            # PyMuPDF auto-detects PNG/JPEG even with filetype="pdf".
            if not document.is_pdf:
                return {"status": "error", "code": "pdf_invalid"}
            if document.needs_pass:
                return {"status": "error", "code": "pdf_encrypted"}
            if document.page_count < 1:
                return {"status": "error", "code": "pdf_invalid"}
            if max_pdf_pages and document.page_count > max_pdf_pages:
                return {"status": "error", "code": "pdf_page_limit_exceeded"}
            return {"status": "ok", "content_type": "application/pdf", "page_count": document.page_count}
    except (MemoryError, OverflowError):
        return {"status": "error", "code": "media_inspection_resource_limit"}
    except Exception:
        return {"status": "error", "code": "pdf_invalid"}


def main() -> int:
    try:
        if sys.argv[1] == "inspect-upload":
            _write(_inspect_upload(sys.argv[2], int(sys.argv[3])))
            return 0
        if sys.argv[1] == "evidence-v1":
            from pdf_evidence_worker import run_cli

            return run_cli(sys.argv[2] if len(sys.argv) > 2 else "")
        if sys.argv[1] == "inspect-pdf":
            max_pages = int(sys.argv[2])
            body = sys.stdin.buffer.read()
            doc = fitz.open(stream=body, filetype="pdf")
            try:
                _write({
                    "status": (
                        "page_limit" if doc.page_count > max_pages else "ok"
                    ),
                    "page_count": doc.page_count,
                })
                return 0
            finally:
                doc.close()
        if sys.argv[1] == "inspect-image":
            max_side = int(sys.argv[2])
            body = sys.stdin.buffer.read()
            pixmap = fitz.Pixmap(body)
            try:
                _write({
                    "status": (
                        "image_side_limit"
                        if max(pixmap.width, pixmap.height) > max_side
                        else "ok"
                    ),
                    "width": pixmap.width,
                    "height": pixmap.height,
                })
                return 0
            finally:
                pixmap = None
        max_pages = int(sys.argv[1])
        max_characters = int(sys.argv[2])
        body = sys.stdin.buffer.read()
        doc = fitz.open(stream=body, filetype="pdf")
        try:
            if doc.page_count > max_pages:
                _write({"status": "page_limit"})
                return 0
            parts: list[str] = []
            character_count = 0
            for page in doc:
                text = page.get_text()
                character_count += len(text)
                if character_count > max_characters:
                    _write({"status": "character_limit"})
                    return 0
                parts.append(text)
            _write({
                "status": "ok",
                "text": "".join(parts),
                "page_count": doc.page_count,
            })
            return 0
        finally:
            doc.close()
    except Exception:
        _write({"status": "invalid"})
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
