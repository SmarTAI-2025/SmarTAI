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
_TOKEN = re.compile(
    r"\\[A-Za-z]+|\\[^\s]|(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?|"
    r"[A-Za-z_\u0370-\u03ff][A-Za-z_0-9\u0370-\u03ff]*|[^\s]"
)
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


def _include_complete_math_delimiters(source: str, first: int, last: int) -> tuple[int, int]:
    # Preserve paired delimiters when all formula content is present, without
    # expanding a subexpression into additional mathematical content.
    for span in _MATH_SPAN.finditer(source):
        inner = list(_TOKEN.finditer(source, span.start(span.lastgroup), span.end(span.lastgroup)))
        if inner and first <= inner[0].start() and last >= inner[-1].end():
            first, last = min(first, span.start()), max(last, span.end())
    return first, last


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
        end = start + len(quote)
        # A literal substring is still not a valid quotation if it truncates
        # a number/name/TeX command: x=1 cannot be quoted from x=10.
        if any(token.start() < boundary < token.end()
               for token in _TOKEN.finditer(source) for boundary in (start, end)):
            return None
        first, last = _include_complete_math_delimiters(source, start, end)
        return SourceMatch(source[first:last], first, last,
                           "exact" if (first, last) == (start, end) else "format")
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
    # When the excerpt contains an entire formula, preserve its original
    # paired delimiters too. Otherwise "Hence $x=1$" could become "Hence $x=1".
    # Never expand a quoted subexpression into additional mathematical content.
    first, last = _include_complete_math_delimiters(source, first, last)
    return SourceMatch(source[first:last], first, last, "format")
