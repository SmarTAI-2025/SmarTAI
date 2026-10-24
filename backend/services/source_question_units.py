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

# The teacher's organized-question mode also permits ordinary exercise labels.
# Keep this opt-in: a numbered proof in a student's answer is not a question list.
NUMBERED_QUESTION_HEADING = re.compile(
    r"(?im)^[ \t]*(?:\*\*)?(?P<number>\d+(?:\.\d+)*)(?:[.、):：]|\*\*[.、):：])"
    r"(?:\*\*)?[ \t]+(?P<title>\S[^\n]*)$"
)


def question_headings(text: str, *, numbered=False):
    matches = list(QUESTION_HEADING.finditer(text))
    if numbered:
        starts = {match.start() for match in matches}
        matches.extend(match for match in NUMBERED_QUESTION_HEADING.finditer(text) if match.start() not in starts)
    return sorted(matches, key=lambda match: match.start())


@dataclass(frozen=True)
class SourceQuestionUnit:
    number: str
    text: str
    pages: tuple[int, ...]
    complete: bool
    closure: str


def source_question_units(pages: dict[int, str], *, source_complete=False,
                          known_numbers: set[str] | None = None,
                          allow_repeated_numbers=False, numbered=False) -> list[SourceQuestionUnit]:
    """Identify bounded source text, without inferring content or inventing IDs.

    Only a next explicit heading in contiguous saved pages closes a question.
    The final unit requires a confirmed complete source. Adjacent same-number
    headings across pages continue that unit; a number returning after another
    question, gaps and unfinished tails stay incomplete. Callers may persist fragments,
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
        matches, previous = [], None
        for match in question_headings(text, numbered=numbered):
            page = next(page for page, start, stop in offsets
                        if start <= match.start("number") < stop)
            # Repeating a running question label on its continuation page is
            # not a new question. Keep the heading and body in the same span.
            continuation = (previous is not None and previous[0] == match.group("number")
                            and previous[1] != page)
            if not continuation:
                matches.append(match)
            previous = (match.group("number"), page)
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
    # Test material may list independent case groups under the same question
    # number. Keep the normal gap/end-of-source checks for each group.
    if allow_repeated_numbers:
        return records
    counts = Counter(unit.number for unit in records)
    return [SourceQuestionUnit(unit.number, unit.text, unit.pages,
        unit.complete and counts[unit.number] == 1,
        unit.closure if counts[unit.number] == 1 else "repeated_heading") for unit in records]


@dataclass(frozen=True)
class SourcePageGroup:
    pages: tuple[int, ...]
    text: str
    complete: bool


def source_page_groups(pages: dict[int, str], *, source_complete=False, source_end_page=None,
                       numbered=True) -> list[SourcePageGroup]:
    """Batch the questions on a page; retain adjoining continuation pages.

    Heading candidates only determine request boundaries, never question IDs
    or a required question count. The semantic extractor may return no questions
    from exposition, section headings or numbered proof steps. One-page
    lookahead closes the trailing candidate without a separate model request.
    """
    numbers = sorted(pages)
    if not numbers:
        return []
    end_page = source_end_page if source_end_page is not None else numbers[-1] if source_complete else None
    spans = []
    for number in numbers:
        if not spans or spans[-1][-1] + 1 != number:
            spans.append([])
        spans[-1].append(number)
    units = [unit for span in spans for unit in source_question_units(
        {number: pages[number] for number in span}, source_complete=span[-1] == end_page,
        allow_repeated_numbers=True, numbered=numbered)]
    parent = {number: number for number in numbers}

    def root(number):
        while parent[number] != number:
            number = parent[number]
        return number

    for unit in units:
        for left, right in zip(unit.pages, unit.pages[1:]):
            parent[root(right)] = root(left)
    for left, right in zip(numbers, numbers[1:]):
        if right != left + 1:
            continue
        heading = next(iter(question_headings(pages[right], numbered=numbered)), None)
        if heading is None or pages[right][:heading.start()].strip():
            # Unnumbered rubrics, answers and test cases also cross pages.
            # No new question boundary means the following text may continue
            # the previous page, even when neither page has a heading.
            parent[root(right)] = root(left)
    groups = {}
    for number in numbers:
        groups.setdefault(root(number), []).append(number)
    result = []
    for group in groups.values():
        members = set(group)
        touching = [unit for unit in units if members.intersection(unit.pages)]
        # A gap or unfinished tail is retained for manual review, not permission
        # to generate an answer from a potentially truncated question.
        closed = all(unit.complete for unit in touching) if touching else (
            group[-1] + 1 in pages or group[-1] == end_page)
        first = group[0]
        heading = next(iter(question_headings(pages[first], numbered=numbered)), None)
        if (first > 1 and first - 1 not in pages
                and (heading is None or pages[first][:heading.start()].strip())):
            # Parallel OCR may finish the continuation before its opening page.
            # Do not publish a group whose left boundary can still move.
            closed = False
        result.append(SourcePageGroup(tuple(group), "\n\n".join(pages[n] for n in group), closed))
    return result
