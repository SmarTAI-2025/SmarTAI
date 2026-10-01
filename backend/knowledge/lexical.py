"""Versioned search-only normalization; source text is never modified."""
from functools import lru_cache
import re
import threading
import unicodedata

import snowballstemmer

_LOCAL = threading.local()


@lru_cache(maxsize=32768)
def stem(word):
    if not hasattr(_LOCAL, "stemmer"):
        _LOCAL.stemmer = snowballstemmer.stemmer("english")
    return _LOCAL.stemmer.stemWord(word)


def stems(terms):
    return ["stem:" + stem(term) for term in terms if re.fullmatch(r"[a-z]{3,}", term)]


def windows(text):
    """Small overlapping passages prevent unrelated exercises diluting a hit."""
    normalized = unicodedata.normalize("NFKC", text).lower()
    positions = [m.start() for m in re.finditer(r"(?m)^\s*\d+[.)、]\s+|(?<=[.!?。])\s+", normalized)]
    positions = sorted({0, *positions, len(normalized)})
    for left, right in zip(positions, positions[1:]):
        part = normalized[left:right]
        for offset in range(0, len(part), 450):
            yield part[offset:offset + 600]
