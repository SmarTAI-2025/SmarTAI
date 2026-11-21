"""Conservative, assignment-wide question aliases; no model or position guesses."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Literal

from backend.recognition.quality import has_unreadable_marker
from backend.services.source_question_units import question_headings


QUESTION_IDENTIFIER_CONFLICT_FLAGS = frozenset({
    "question_identifier_conflict", "question_identifier_mismatch", "identifier_mismatch",
})


def _label(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def _parts(value):
    label = _label(value).casefold()
    label = re.sub(r"\b(?:section|sec|chapter|chap|ch|question|problem|number|no|q)\.?\s*(?=\d)", "", label)
    label = re.sub(r"[第章节题目]", " ", label)
    numbers = re.findall(r"\d+(?:\.\d+)*", label)
    remainder = re.sub(r"\d+(?:\.\d+)*", "", label)
    if not numbers or re.search(r"[^\s/.:：、()]", remainder):
        return ()
    return tuple(part for number in numbers for part in number.split("."))


def question_number_candidates(number, problems):
    """Keep all collisions, including local aliases outside a retry subset."""
    label, parts = _label(number).casefold(), _parts(number)
    if not label:
        return []
    if len(parts) == 1:
        # A bare '1' may mean any chapter's first exercise, even if one
        # assignment row also happens to have exactly that displayed label.
        return [q_id for q_id, problem in problems.items()
                if (known := _parts(problem.get("number"))) and known[-1:] == parts]
    exact = [q_id for q_id, problem in problems.items() if _label(problem.get("number")).casefold() == label]
    if exact and (not parts or re.search(r"\b(?:section|sec|chapter|chap|ch)\.?\s*\d|[章节]", label)):
        return exact
    return [q_id for q_id, problem in problems.items() if parts and _parts(problem.get("number")) == parts]


def blank_questions_needing_source_review(text, problems, blank_ids, *, low_confidence_ocr):
    """Locate conflicting or unreadable source evidence without assigning student work."""
    expected = {q_id: _parts(problem.get("number")) for q_id, problem in problems.items()}
    known = set(expected.values())
    unexpected = set()
    headings = question_headings(text, qualified=True)
    doubtful = set()
    has_unclear = low_confidence_ocr and has_unreadable_marker(text)
    if has_unclear and has_unreadable_marker(text[:headings[0].start() if headings else len(text)]):
        doubtful.update(blank_ids)
    for i, heading in enumerate(headings):
        parts = _parts(heading.group("number"))
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        body = heading.group("title") + " " + text[heading.end():end]
        if has_unclear and has_unreadable_marker(body):
            matches = question_number_candidates(heading.group("number"), problems)
            doubtful.update(blank_ids.intersection(matches) if len(matches) == 1 else blank_ids)
        if len(parts) < 3 or parts in known:
            continue
        if re.search(r"\w", re.sub(r"^\s*(?:proof|answer|solution|证明|解答)\s*[:：.]*", "", body, flags=re.I)):
            unexpected.add(parts)
    return doubtful | {q_id for q_id in blank_ids if (parts := expected.get(q_id)) and any(
        parts[:-1] == seen[:-1] and len(parts[-1]) == len(seen[-1])
        and sum(a != b for a, b in zip(parts[-1], seen[-1])) == 1 for seen in unexpected
    )}


def question_number_aliases(problems):
    # Parse once, rather than rescanning the assignment for every alias.
    entries = {q_id: (_label(problem.get("number")), _parts(problem.get("number")))
               for q_id, problem in problems.items()}
    exact = Counter(label.casefold() for label, _ in entries.values())
    full = Counter(parts for _, parts in entries.values() if parts)
    local = Counter(parts[-1] for _, parts in entries.values() if parts)
    aliases = {}
    for q_id, (label, parts) in entries.items():
        if len(parts) == 1:
            unique = local[parts[-1]] == 1
        elif not parts or re.search(r"(?i)\b(?:section|sec|chapter|chap|ch)\.?\s*\d|[章节]", label):
            unique = exact[label.casefold()] == 1
        else:
            unique = full[parts] == 1
        candidates = [label] if label and unique else []
        if parts and full[parts] == 1 and (len(parts) > 1 or local[parts[-1]] == 1):
            candidates.append(".".join(parts))
        if parts and local[parts[-1]] == 1:
            candidates.append(parts[-1])
        aliases[q_id] = list(dict.fromkeys(candidates))
    return aliases


def copied_question_candidates(text, problems):
    """Only complete copied stems qualify; similar formulas are not equal."""
    def stem_text(value):
        # Unlike identifier aliases, mathematical text must retain compatibility
        # distinctions such as superscripts/subscripts (x², x₂ and x2).
        return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(value or ""))).strip()

    content = stem_text(text)
    return [q_id for q_id, problem in problems.items()
            if len(stem := stem_text(problem.get("stem"))) >= 24 and stem in content]


def resolve_question_match(number, content, problems):
    """Resolve literal evidence or return None for semantic/manual matching."""
    numbered = question_number_candidates(number, problems)
    copied = copied_question_candidates(content, problems)
    if len(numbered) == 1:
        return numbered[0] if not copied or numbered[0] in copied else None
    if len(copied) == 1 and (not number or copied[0] in numbered):
        return copied[0]
    return None


def transcribed_answer_match(q_id, number, content, problems, flags) -> Literal["verified", "conflict", "unverified"]:
    """Use a leading exercise heading as evidence without changing assignment."""
    if QUESTION_IDENTIFIER_CONFLICT_FLAGS.intersection(flags):
        return "conflict"
    if not content or q_id not in problems:
        return "unverified"
    numbered = question_number_candidates(number, problems)
    if numbered and q_id not in numbered:
        return "conflict"
    copied = copied_question_candidates(content, problems)
    if any(candidate != q_id for candidate in copied):
        return "conflict"
    first_line = content.lstrip().partition("\n")[0]
    if len(first_line) > 2048:
        return "unverified"
    headings = question_headings(first_line, qualified=True)
    if headings:
        observed = headings[0].group("number")
    else:
        # OCR also emits qualified labels with a closing parenthesis or
        # in bold. These are evidence here, never page-splitting boundaries.
        label = re.match(r"^(?:(\d+(?:\.\d+){2,})[)）]|\*\*(\d+(?:\.\d+){2,})[.、:：)）]?\*\*(?=\s|$))", first_line)
        if not label:
            return "unverified"
        observed = label[1] or label[2]
    candidates = question_number_candidates(observed, problems)
    if candidates == [q_id]:
        return "verified"
    return "conflict" if q_id not in candidates else "unverified"


def mark_question_match_conflicts(student, problems):
    """Check the actual retained fields, including after independent repairs."""
    for answer in student.get("stu_ans", []):
        numbered = question_number_candidates(answer.get("number"), problems)
        copied = copied_question_candidates(answer.get("content"), problems)
        if ((len(numbered) == 1 and answer.get("q_id") != numbered[0])
                or (copied and answer.get("q_id") not in copied)):
            answer["flag"] = list(dict.fromkeys([*answer.get("flag", [])[:99], "recognition_manual_pending"]))
