"""Lossless, offset-preserving chunks for the current knowledge ingestion pipeline."""
import re

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_CHARS_PER_CHUNK = 2000
DEFAULT_CHUNK_WORDS = 500
DEFAULT_OVERLAP_WORDS = 50


def chunk_spans(text: str, *, chunk_words=DEFAULT_CHUNK_WORDS, overlap_words=DEFAULT_OVERLAP_WORDS):
    """Lossless windows with exact character offsets into the original page.

    Prefer word boundaries, but split oversized Chinese/code/formula runs by
    characters. Whitespace and formatting are retained; overlapping windows can
    be reconstructed using offsets without counting their overlap twice.
    """
    if not text or not text.strip():
        return []
    chunk_words = chunk_words if chunk_words > 0 else DEFAULT_CHUNK_WORDS
    overlap_words = overlap_words if 0 <= overlap_words < chunk_words else chunk_words // 10
    result, start = [], 0
    while start < len(text):
        limit = min(len(text), start + MAX_CHARS_PER_CHUNK)
        words = list(re.finditer(r"\S+", text[start:limit]))
        end = start + words[chunk_words].start() if len(words) > chunk_words else limit
        result.append(dict(content=text[start:end], start=start, end=end))
        if end == len(text):
            break
        if len(words) > chunk_words:
            next_start = start + words[chunk_words - overlap_words].start()
        else:
            next_start = end - (min(200, (end - start) // 10) if overlap_words else 0)
        start = max(start + 1, next_start)
    return result
