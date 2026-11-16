"""Locate quoted evidence without mistaking presentation for mathematical meaning.

This is source grounding, not algebraic equivalence. Only paired math delimiters
and whitespace between lexical tokens may differ. No case folding, macro
expansion, Unicode substitution, equation simplification or fuzzy similarity.
Returned text always comes from the original source; ambiguous matches require
more context instead of guessing which occurrence the model meant.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
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


def source_lines(source: str):
    """Share Python's line boundaries while preserving original offsets."""
    offset = 0
    for number, line in enumerate(source.splitlines(keepends=True), 1):
        text = line.rstrip("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
        yield number, text, offset, offset + len(text)
        offset += len(line)


def source_identity(span: SourceMatch) -> tuple[int, int]:
    """Same original lexical span, independent of whitespace/math delimiters.

    Keep occurrence offsets: repeated equal text elsewhere is a distinct source,
    and a strict subexpression never becomes the identity of the whole line.
    """
    tokens = _tokens(span.text)
    return (span.start + tokens[0][1], span.start + tokens[-1][2]) if tokens else (span.start, span.end)


def source_catalog(answer: str, problem: str, criterion: str, *, parts=()) -> dict[str, tuple[str, int, int]]:
    """Address original lines without asking a model to re-encode their TeX.

    IDs are local to this immutable answer/question, never model-supplied text.
    The full inputs remain available when this bounded index is incomplete.
    """
    result, remaining = {}, 40000
    for prefix, source in (("student", answer), ("question", problem), ("rubric", criterion)):
        for number, text, start, end in source_lines(source):
            if text.strip() and len(text) <= remaining and len(result) < 240:
                result[f"{prefix}:{number}"] = (text, start, end)
                remaining -= len(text)
    ranges = _labelled_ranges(answer, parts) if len(parts) > 1 else {}
    for key, (_, start, end) in list(result.items()):
        if not key.startswith('student:'):
            continue
        for left, right in ranges.values():
            first, last = max(left, start), min(right, end)
            if start <= first < last <= end and (first, last) != (start, end):
                result[f'{key}:{first - start + 1}-{last - start}'] = (answer[first:last], first, last)
    return result


_PART_LABEL = r'(?:[（(](?:[0-9０-９]+|[A-Za-z]|[ivxIVX]{1,6})[)）]|[0-9０-９]+[)）])'


def _part_headings(text):
    """Prefer explicit headings over ambiguous coefficients and inline mentions.

    Inline headings still support same-line answers. When that label has a
    clear line heading, inline mentions cannot duplicate or displace it. Unmapped
    headings remain boundaries; uncertainty never extends the preceding part.
    """
    from backend.services.question_structure import _normalize_label
    candidates = []
    for _, line, start, _ in source_lines(text):
        for match in re.finditer(r'(?:^[ \t]*|(?<=[，,；;。])[ \t]*|(?<=[ \t]))(?P<label>' + _PART_LABEL + ')', line):
            prefix = line[:match.start()].rstrip()
            raw_tail = line[match.end():]
            tail = raw_tail.strip()
            anchored = not prefix
            separated = bool(prefix and prefix[-1] in '，,；;。')
            formula = bool(re.match(r'[A-Za-zα-ωΑ-Ω](?:[=+*/^−-]|$)', tail))
            if not anchored and not separated and (not tail or tail[0] in '=+*/^<>，,；;。' or formula):
                continue
            label = _normalize_label(match['label'].strip('（()）'))
            # A tight parenthesized coefficient at line start is ambiguous.
            # Keep it as a boundary unless a clearer heading for that label
            # exists. A sentence separator supports a same-line formula title.
            coefficient = (formula and raw_tail == raw_tail.lstrip()
                           and match['label'].startswith(('(', '（')))
            priority = (3 if anchored and not coefficient else 2 if separated else 1 if anchored else 0)
            begin = match.start() if anchored else match.start('label')
            candidates.append((start + begin, start + match.end(), label, anchored, priority))
    clear_labels = {label for _, _, label, _, priority in candidates if priority >= 2}
    candidates = [c for c in candidates if c[4] != 1 or c[2] not in clear_labels]
    anchored_labels = {label for _, _, label, anchored, _ in candidates if anchored}
    return [(start, end, label) for start, end, label, anchored, _ in candidates
            if anchored or label not in anchored_labels]


def _labelled_ranges(text, parts):
    from backend.services.question_structure import _normalize_label
    # Explicit headings may share a sentence or occupy their own line.
    # Trailing equation numbers do not override frozen answer-to-part links.
    headings = _part_headings(text)
    result = {}
    for part in parts:
        label = _normalize_label((part.label or '').strip('（()）'))
        matches = [(i, h) for i, h in enumerate(headings) if h[2] == label]
        if label and len(matches) == 1 and sum(_normalize_label((p.label or '').strip('（()）')) == label for p in parts) == 1:
            index, heading = matches[0]
            result[part.id] = (heading[0], headings[index + 1][0] if index + 1 < len(headings) else len(text))
    return result


def match_source_slice(source: str, start: int, end: int) -> SourceMatch | None:
    """Resolve an explicit address without truncating a name, number or command."""
    if not 0 <= start < end <= len(source) or not source[start:end].strip():
        return None
    if any(token.start() < boundary < token.end()
           for token in _TOKEN.finditer(source) for boundary in (start, end)):
        return None
    return SourceMatch(source[start:end], start, end, 'exact')


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
        # A literal substring is still not a valid quotation if it truncates
        # a number/name/TeX command: x=1 cannot be quoted from x=10.
        tokens = list(_TOKEN.finditer(source))
        starts = [token.start() for token in tokens]
        def boundary_ok(position):
            index = bisect_right(starts, position) - 1
            return index < 0 or not tokens[index].start() < position < tokens[index].end()
        located = None
        while start >= 0:
            end = start + len(quote)
            if boundary_ok(start) and boundary_ok(end):
                if located is not None:
                    return None
                located = start, end
            start = source.find(quote, start + 1)
        if located is not None:
            first, last = _include_complete_math_delimiters(source, *located)
            return SourceMatch(source[first:last], first, last,
                               "exact" if (first, last) == located else "format")
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
