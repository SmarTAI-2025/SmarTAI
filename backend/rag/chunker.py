"""
Text extraction + chunking for task-scoped RAG.

Reuses backend.tools.file_processing for PDF / text decoding so we share the
same charset detection (UTF-8 → GBK fallback) and PyMuPDF logic as the rest
of the ingest pipeline. Adds a simple sliding-window word chunker — no
tokenizer dependency, predictable RAM footprint on Render free tier.
"""
from __future__ import annotations

import logging
import os
from typing import List

from fastapi import HTTPException

from backend.tools.file_processing import decode_text_bytes, extract_text_from_pdf

logger = logging.getLogger(__name__)


# ─── Limits (per CLAUDE plan) ─────────────────────────────────────────────────
MAX_FILE_BYTES = 5 * 1024 * 1024   # 5 MB per upload
MAX_CHUNKS_PER_DOC = 500
MAX_CHARS_PER_CHUNK = 2000          # safety belt against runaway docs
DEFAULT_CHUNK_WORDS = 500
DEFAULT_OVERLAP_WORDS = 50


SUPPORTED_EXTS = (".pdf", ".md", ".markdown", ".txt", ".rst")


def _guess_kind(filename: str) -> str:
    name = (filename or "").lower()
    for ext in SUPPORTED_EXTS:
        if name.endswith(ext):
            return ext
    return ""


async def extract_text(filename: str, body: bytes) -> str:
    """Decode a KB upload to a single text string.

    Raises HTTPException(400) for unsupported types or decode failure, and
    HTTPException(413) if the file exceeds MAX_FILE_BYTES.
    """
    if len(body) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"KB file too large ({len(body)} bytes > {MAX_FILE_BYTES}); "
                   f"please split into smaller documents.",
        )

    kind = _guess_kind(filename)
    if not kind:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported KB file type: {os.path.basename(filename)}. "
                   f"Allowed: PDF, MD, TXT, RST.",
        )

    if kind == ".pdf":
        text = await extract_text_from_pdf(body)
    else:
        text = await decode_text_bytes(body)

    text = (text or "").strip()
    if not text:
        raise HTTPException(
            status_code=400,
            detail="KB file appears empty after extraction.",
        )
    return text


def chunk_text(
    text: str,
    *,
    chunk_words: int = DEFAULT_CHUNK_WORDS,
    overlap_words: int = DEFAULT_OVERLAP_WORDS,
) -> List[str]:
    """Split text into overlapping word-window chunks.

    Returns at most MAX_CHUNKS_PER_DOC chunks; later content is dropped with a
    warning. We split on whitespace (not real tokens) to keep this dependency
    free — embed providers re-tokenize anyway, and BM25 fallback wants words.
    """
    if not text or not text.strip():
        return []

    words = text.split()
    if not words:
        return []

    if chunk_words <= 0:
        chunk_words = DEFAULT_CHUNK_WORDS
    if overlap_words < 0 or overlap_words >= chunk_words:
        overlap_words = max(0, chunk_words // 10)

    step = chunk_words - overlap_words
    chunks: List[str] = []
    i = 0
    while i < len(words) and len(chunks) < MAX_CHUNKS_PER_DOC:
        window = words[i : i + chunk_words]
        chunk = " ".join(window).strip()
        if chunk:
            # Hard cap per-chunk char length so a doc of giant pseudo-words
            # can't blow up downstream embedding payloads.
            if len(chunk) > MAX_CHARS_PER_CHUNK:
                chunk = chunk[:MAX_CHARS_PER_CHUNK]
            chunks.append(chunk)
        if step <= 0:
            break
        i += step

    if len(chunks) >= MAX_CHUNKS_PER_DOC and i < len(words):
        logger.warning(
            f"chunk_text truncated at {MAX_CHUNKS_PER_DOC} chunks "
            f"({len(words) - i} words discarded)"
        )

    return chunks
