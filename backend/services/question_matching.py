"""Conservative, assignment-wide question aliases; no model or position guesses."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter


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


def mark_question_match_conflicts(student, problems):
    """Check the actual retained fields, including after independent repairs."""
    for answer in student.get("stu_ans", []):
        numbered = question_number_candidates(answer.get("number"), problems)
        copied = copied_question_candidates(answer.get("content"), problems)
        if ((len(numbered) == 1 and answer.get("q_id") != numbered[0])
                or (copied and answer.get("q_id") not in copied)):
            answer["flag"] = list(dict.fromkeys([*answer.get("flag", [])[:99], "recognition_manual_pending"]))
