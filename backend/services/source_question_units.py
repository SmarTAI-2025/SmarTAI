"""Conservative textual question boundaries shared by source consumers."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass


# Bare numbered lines are often solution steps. These are deliberately not
# structural boundaries. Semantic extraction remains the consumer's job.
QUESTION_HEADING = re.compile(
    r"(?im)^\s*(?:(?:#{1,6}\s+)(?:(?:question|problem|q|题目?|第)\s*)?"
    r"|(?:(?:question|problem|q)\s*|第\s*))"
    r"(?P<number>\d+(?:\.\d+)*)\s*(?:题)?(?:\s*[.、):：-]\s*|\s+|$)"
    r"(?P<title>[^\n]*)$"
)


@dataclass(frozen=True)
class SourceQuestionUnit:
    number: str
    text: str
    pages: tuple[int, ...]
    complete: bool
    closure: str


def source_question_units(pages: dict[int, str], *, source_complete=False,
                          known_numbers: set[str] | None = None) -> list[SourceQuestionUnit]:
    """Identify bounded source text, without inferring content or inventing IDs.

    Only a next explicit heading in contiguous saved pages closes a question.
    The final unit requires a confirmed complete source. Repeated headings,
    gaps and unfinished tails stay incomplete. Callers may persist fragments,
    but must not generate/grade from an incomplete unit.
    """
    groups: list[list[int]] = []
    for page in sorted(pages):
        if not groups or groups[-1][-1] + 1 != page:
            groups.append([])
        groups[-1].append(page)
    records = []
    for group in groups:
        parts, offsets, offset = [], [], 0
        for page in group:
            text = pages[page]
            parts.append(text)
            offsets.append((page, offset, offset + len(text)))
            offset += len(text) + 2
        text = "\n\n".join(parts)
        matches = list(QUESTION_HEADING.finditer(text))
        for index, match in enumerate(matches):
            number = match.group("number")
            if known_numbers is not None and number not in known_numbers:
                continue
            following = matches[index + 1] if index + 1 < len(matches) else None
            end = following.start() if following else len(text)
            # An unknown numbered heading is still a boundary, but cannot
            # assert completeness against a known question set.
            next_known = following is not None and (known_numbers is None
                or following.group("number") in known_numbers)
            final = following is None and source_complete and len(groups) == 1
            body = text[match.start():end].strip()
            records.append(SourceQuestionUnit(number, body,
                tuple(page for page, start, stop in offsets if stop > match.start() and start < end),
                bool(next_known or final), "next_heading" if next_known else "source_end" if final else "open"))
    counts = Counter(unit.number for unit in records)
    return [SourceQuestionUnit(unit.number, unit.text, unit.pages,
        unit.complete and counts[unit.number] == 1,
        unit.closure if counts[unit.number] == 1 else "repeated_heading") for unit in records]
