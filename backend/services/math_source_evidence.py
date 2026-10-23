"""Locate quoted evidence without mistaking presentation for mathematical meaning.

This is source grounding, not algebraic equivalence. Only paired math delimiters
and whitespace between lexical tokens may differ. No case folding, macro
expansion, Unicode substitution, equation simplification or fuzzy similarity.
Returned text always comes from the original source; ambiguous matches require
more context instead of guessing which occurrence the model meant.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
import re
from typing import Literal


_MATH_SPAN = re.compile(
    r"(?<!\\)(?:\$\$(?P<display>[\s\S]+?)\$\$|"
    r"\$(?!\$)(?P<inline>(?:\\.|[^$])+?)\$|"
    r"\\\((?P<paren>[\s\S]+?)\\\)|\\\[(?P<bracket>[\s\S]+?)\\\])"
)
_TOKEN = re.compile(r"\\[A-Za-z]+|\\[^\s]|\w+|[^\s]", re.UNICODE)
_LIMIT = 500_000


@dataclass(frozen=True)
class SourceMatch:
    text: str
    start: int
    end: int
    method: Literal["exact", "format"]


def _tokens(text: str) -> list[tuple[str, int, int]]:
    # Replace only the delimiters of complete, paired spans. Their contents
    # and all source offsets remain intact, including escaped dollar signs.
    parts, previous = [], 0
    for match in _MATH_SPAN.finditer(text):
        group = match.lastgroup
        parts.append((previous, match.start()))
        parts.append((match.start(group), match.end(group)))
        previous = match.end()
    parts.append((previous, len(text)))
    return [(match.group(), match.start(), match.end())
            for start, end in parts for match in _TOKEN.finditer(text, start, end)]


def match_source_quote(source: str, quote: str) -> SourceMatch | None:
    """Return one unambiguous original span; None means unresolved, not wrong.

    Whitespace is ignored *between tokens*, never inside identifiers/numbers.
    Thus ``x = 1`` matches ``x=1``, while ``x y`` cannot match ``xy`` and
    ``1 2`` cannot match ``12``. The caller still decides whether the statement
    follows from the problem and whether it earns or loses points.
    """
    if (not isinstance(source, str) or not isinstance(quote, str)
            or not quote.strip() or len(source) > _LIMIT or len(quote) > _LIMIT
            or "\0" in source or "\0" in quote):
        return None
    start = source.find(quote)
    if start >= 0:
        if source.find(quote, start + 1) >= 0:
            return None
        return SourceMatch(source[start:start + len(quote)], start, start + len(quote), "exact")
    source_tokens, quote_tokens = _tokens(source), _tokens(quote)
    if not quote_tokens or not source_tokens:
        return None
    # Token separators retain word and numeric boundaries. Searching the
    # encoded sequence avoids quadratic list comparisons on long submissions.
    keys, offsets, cursor = [], [], 0
    for value, _, _ in source_tokens:
        offsets.append(cursor)
        keys.append(value)
        cursor += len(value) + 1
    encoded = "\0" + "\0".join(keys) + "\0"
    sought = "\0" + "\0".join(value for value, _, _ in quote_tokens) + "\0"
    index = encoded.find(sought)
    if index < 0 or encoded.find(sought, index + 1) >= 0:
        return None
    token_index = bisect_left(offsets, index)
    first = source_tokens[token_index][1]
    last = source_tokens[token_index + len(quote_tokens) - 1][2]
    return SourceMatch(source[first:last], first, last, "format")
