"""Isolated PyMuPDF worker used by ``file_processing``.

The parent process owns timeout/concurrency policy and kills this process on a
deadline.  This module deliberately emits only a small JSON protocol and never
returns raw parser exceptions.
"""
from __future__ import annotations

import json
import sys

import pymupdf as fitz


def _write(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def main() -> int:
    # On Windows the subprocess's stdout pipe inherits the system ANSI/GBK
    # codepage.  We always emit UTF-8 JSON (ensure_ascii=False), so ask the
    # interpreter for UTF-8 stdio to prevent UnicodeEncodeError from silently
    # degrading into a "{status: invalid}" payload.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    try:
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
