"""Preserve explicit teacher allocations in preparation and recovery.

Free-form matching still uses the existing score-policy model call. Concrete
numbered constraints are derived from the already-frozen policy, not a second
persistent scoring representation.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from backend.domain.errors import ValidationError
from backend.services.question_structure import (
    MajorQuestionStructureV1, MajorQuestionSubpartV1, summarize_rubric_points,
)

_NUMBER = r"(?:[0-9]+(?:\.[0-9]+)*|[IVXLCDM]+|[〇零一二三四五六七八九十百两]+)"
_RANGE_SEPARATOR = r"(?:[–—\-~～至到]|\bto\b|\bthrough\b)"
_NUMBERS = rf"{_NUMBER}(?:\s*{_RANGE_SEPARATOR}\s*(?:第\s*)?{_NUMBER})?"
_DECLARATION = re.compile(
    rf"(?:第\s*(?P<ordinal>{_NUMBERS})\s*(?:大\s*)?题|"
    rf"(?<![A-Za-z0-9_])(?:大\s*题|题目|questions?|problems?|q)\s*(?P<prefixed>{_NUMBERS})|"
    rf"(?:^|[;；,，\n])\s*(?P<bare>{_NUMBERS})\s*[:：])"
    r"\s*[:：,，(]?\s*(?:(?:共|总共|总分|满分|为|是|计|每题|各|均|worth|total|is|are|each)\s*[:：=]?\s*)*"
    r"(?P<points>[0-9]+(?:\.[0-9]{1,2})?)\s*(?:分|points?|pts?)?"
    r"(?:\s*each\b)?(?=$|[\s,，;；。()]|\.(?![0-9]))",
    re.IGNORECASE,
)
_SUBPART = re.compile(
    r"(?<![A-Za-z0-9_])(?P<label>\((?:[0-9]{1,2}|[A-Za-z]|[ivxIVX]{1,6})\))"
    r"\s*[:：]?\s*(?P<points>[0-9]+(?:\.[0-9]{1,2})?)"
    r"\s*(?:分|points?|pts?)?(?=$|[\s,，;；。.)])",
    re.IGNORECASE,
)


@dataclass
class ExplicitTeacherScore:
    maximum: Decimal | None
    subparts: dict[str, Decimal] = field(default_factory=dict)


def _mismatch() -> ValidationError:
    return ValidationError(
        "Question numbers or point allocations do not match the teacher's explicit instructions.",
        code="question_structure_score_mismatch",
    )


def normalize_question_number(value: object) -> str:
    number = unicodedata.normalize("NFKC", str(value or "")).strip()
    number = re.sub(r"^(?:第\s*|大\s*题\s*|题目\s*|question\s*|problem\s*|q\s*)", "", number, flags=re.I)
    number = re.sub(r"\s*(?:大\s*)?题$", "", number.strip(" .、:：")).strip()
    if re.fullmatch(r"[〇零一二三四五六七八九十百两]+", number):
        digits = dict(zip("〇零一二三四五六七八九两", [0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 2]))
        total = current = 0
        for character in number:
            if character in {"十", "百"}:
                total += (current or 1) * (10 if character == "十" else 100)
                current = 0
            else:
                current = digits[character]
        number = str(total + current)
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", number):
        number = ".".join(str(int(part)) for part in number.split("."))
    return number.casefold()


def _expand_numbers(value: str) -> list[str]:
    parts = re.split(_RANGE_SEPARATOR, value, flags=re.IGNORECASE)
    if len(parts) == 1:
        return [normalize_question_number(value)]
    first, last = [normalize_question_number(part).split(".") for part in parts]
    if first[:-1] != last[:-1] or not first[-1].isdigit() or not last[-1].isdigit():
        raise _mismatch()
    start, end = int(first[-1]), int(last[-1])
    if not 1 <= end - start + 1 <= 200:
        raise _mismatch()
    return [".".join([*first[:-1], str(number)]) for number in range(start, end + 1)]


def _label(value: str) -> str:
    return "(" + unicodedata.normalize("NFKC", value).strip().strip("()").casefold() + ")"


def explicit_teacher_scores(text: str) -> dict[str, ExplicitTeacherScore]:
    """Parse declarations independently of the extracted question set."""
    text = unicodedata.normalize("NFKC", text or "")
    declarations = list(_DECLARATION.finditer(text))
    result: dict[str, ExplicitTeacherScore] = {}
    for index, match in enumerate(declarations):
        numbers = _expand_numbers(match.group("ordinal") or match.group("prefixed") or match.group("bare"))
        # A range's aggregate total must never be copied to every question.
        per_question = len(numbers) == 1 or bool(re.search(r"每题|各|均|\beach\b", match.group(), re.I))
        maximum = Decimal(match.group("points")) if per_question else None
        end = declarations[index + 1].start() if index + 1 < len(declarations) else len(text)
        for number in numbers:
            entry = result.setdefault(number, ExplicitTeacherScore(maximum))
            if entry.maximum is not None and maximum is not None and entry.maximum != maximum:
                raise _mismatch()
            if entry.maximum is None:
                entry.maximum = maximum
            if maximum is None:
                continue
            for part in _SUBPART.finditer(text[match.end():end]):
                label, points = _label(part.group("label")), Decimal(part.group("points"))
                if label in entry.subparts and entry.subparts[label] != points:
                    raise _mismatch()
                entry.subparts[label] = points
    return result


def _require_coverage(problems: Mapping[str, Mapping[str, Any]], outline) -> None:
    counts = Counter(normalize_question_number(row.get("number")) for row in problems.values())
    # A teacher may specify only some questions. Those declarations must each
    # match exactly once; unrelated questions are not removed or forbidden.
    if any(counts[number] != 1 for number in outline):
        raise _mismatch()


def validate_teacher_question_coverage(problems: Mapping[str, Mapping[str, Any]], policy) -> None:
    if policy.mode == "per_question":
        _require_coverage(problems, explicit_teacher_scores(policy.per_question_text or ""))


def validate_teacher_subpart_rubric(
    criterion: str, problem: Mapping[str, Any], expected: Mapping[str, str],
) -> None:
    if not expected:
        return
    structure = MajorQuestionStructureV1.model_validate(problem.get("question_structure"))
    labels = list(dict.fromkeys([*(_label(part.label) for part in structure.subparts), *expected]))
    # Include all known labels so an unspecified (b) is not added to (a).
    structure = structure.model_copy(update={"subparts": [
        MajorQuestionSubpartV1(subpart_id=f"sp{i + 1}", label=label, order=i)
        for i, label in enumerate(labels)
    ]})
    summary = summarize_rubric_points(criterion, problem["max_score"], structure)
    actual = {_label(item.label): Decimal(item.points) for item in summary.items}
    if not summary.is_valid or any(actual.get(label) != Decimal(points) for label, points in expected.items()):
        raise _mismatch()


def teacher_score_requirements(
    problems: Mapping[str, Mapping[str, Any]], policy, *, check_rubrics: bool = False,
) -> dict[str, dict[str, str]]:
    """Derive generation-only constraints without mutating persisted rows."""
    if policy.mode != "per_question":
        return {}
    outline = explicit_teacher_scores(policy.per_question_text or "")
    _require_coverage(problems, outline)
    requirements: dict[str, dict[str, str]] = {}
    for q_id, problem in problems.items():
        entry = outline.get(normalize_question_number(problem.get("number")))
        if entry is None:
            continue
        if entry.maximum is not None and Decimal(str(problem.get("max_score"))) != entry.maximum:
            raise _mismatch()
        if entry.subparts:
            expected = {label: str(points) for label, points in entry.subparts.items()}
            requirements[q_id] = expected
            if check_rubrics:
                validate_teacher_subpart_rubric(str(problem.get("criterion") or ""), problem, expected)
    return requirements
