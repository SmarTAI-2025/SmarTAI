"""Conservative exercise labels derived from contiguous source text.

These are retrieval hints, never corrected OCR or replacement source content.
Cross-page inheritance requires both source and printed-page continuity, an
active exercises block, and increasing labels. Missing pages reset context.
"""
from __future__ import annotations

from collections import defaultdict
import re

_NUMBER = r"\d+(?:[ \t]*\.[ \t]*\d+)+"
_SECTION = re.compile(r"(?im)^[ \t]*(?:sec(?:tion)?\.?[ \t]*)(" + _NUMBER + r")(?![\w.])[^\n]*")
_HEADING = re.compile(r"(?m)^[ \t]*(" + _NUMBER + r")[ \t]+([A-Z][A-Z ,&-]{4,})[ \t]*$")
_EXERCISES = re.compile(r"(?im)^[ \t]*(?:e[ \t]*x[ \t]*e[ \t]*r[ \t]*c[ \t]*i[ \t]*s[ \t]*e[ \t]*s|习题|练习)[ \t]*$")
_LABEL = re.compile(r"(?m)^[ \t]*(\d{1,4})[ \t]*[.)、][ \t]+(?=\S)")
_PAGE = re.compile(r"(?m)^[ \t]*(\d{1,5})[ \t]*$")


def _number(raw):
    return re.sub(r"[ \t]", "", raw)


def _page_text(chunks):
    text = ""
    for chunk in sorted(chunks, key=lambda c: c.metadata.get("start", -1)):
        start, end = chunk.metadata.get("start"), chunk.metadata.get("end")
        if (type(start) is not int or type(end) is not int or start < 0 or end != start + len(chunk.content)
                or start > len(text) or end > 500000):
            return None
        overlap = min(len(text) - start, len(chunk.content))
        if text[start:start + overlap] != chunk.content[:overlap]:
            return None
        text += chunk.content[overlap:]
    return text


def exercise_hints(chunks):
    pages = defaultdict(list)
    for chunk in chunks:
        page = chunk.metadata.get("page_number")
        if type(page) is int and page > 0:
            pages[(chunk.document_id, chunk.content_version, page)].append(chunk)
    hints = defaultdict(set)
    continuations = defaultdict(set)
    previous = None
    for key, page_chunks in sorted(pages.items()):
        text = _page_text(page_chunks)
        if text is None:
            previous = None
            continue
        tail_start = max(0, len(text) - len("\n".join(text.rstrip().splitlines()[-5:])) - 1)
        printed = [m for m in _PAGE.finditer(text) if m.start() >= tail_start or m.start() > len(text) * .8]
        printed_page = int(printed[-1][1]) if printed else None
        continuous = bool(previous and key[:2] == previous[0][:2] and key[2] == previous[0][2] + 1
                          and printed_page is not None and previous[1] is not None and printed_page == previous[1] + 1)
        section = previous[2] if continuous else None
        active = previous[3] if continuous else False
        last = previous[4] if continuous else None
        anchors = list(_LABEL.finditer(text))
        if active and anchors and last is not None and int(anchors[0][1]) <= last:
            active = False
        if active and section and last is not None and anchors and anchors[0].start() > 0:
            head = text[:anchors[0].start()].strip()
            if head and not _SECTION.search(head) and not _HEADING.search(head) and not _EXERCISES.search(head):
                identifier = "id:" + section + "." + str(last)
                before = [c for c in pages[previous[0]] if identifier in hints[c.id]]
                after = [c for c in page_chunks if c.metadata["start"] < anchors[0].start()]
                for first in before:
                    for second in after:
                        hints[second.id].add(identifier)
                        continuations[first.id].add(second.id)
        sections = list(_SECTION.finditer(text))
        headings = list(_HEADING.finditer(text))
        footers = [m for m in sections if (m.start() >= tail_start or m.start() > len(text) * .8)
                   and (not anchors or m.start() > anchors[-1].start())]
        # A footer can label a single-section page, but cannot reach backwards
        # across an in-body section transition (e.g. exercises then a new section).
        if not headings and len({_number(m[1]) for m in sections}) == 1 and footers:
            footer_section = _number(footers[0][1])
            if section != footer_section:
                active, last = False, None
            section = footer_section
        events = [(m.start(), "section", _number(m[1])) for m in headings + sections if m not in footers]
        events += [(m.start(), "exercises", None) for m in _EXERCISES.finditer(text)]
        events += [(m.start(), "label", int(m[1])) for m in anchors]
        for start, kind, value in sorted(events):
            if kind == "section":
                section, active, last = value, False, None
            elif kind == "exercises":
                active, last = True, None
            elif active and section:
                if last is not None and value <= last:
                    active = False
                    continue
                identifier = "id:" + section + "." + str(value)
                for chunk in page_chunks:
                    if chunk.metadata["start"] <= start < chunk.metadata["end"]:
                        hints[chunk.id].add(identifier)
                last = value
        previous = (key, printed_page, section, active, last)
    return hints, continuations
