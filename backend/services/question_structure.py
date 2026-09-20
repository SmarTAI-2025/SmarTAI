"""Major-question structure and rubric invariants.

One :class:`AssignmentQuestionRecord` is one scored major question.  Markers
such as ``(a)`` and ``(2)`` are descriptive structure inside that record; they
never receive their own q_id, maximum score, generation job, or grading row.

The helpers in this module are deliberately deterministic.  Model output is
normalised before score policy is applied, and explicit point allocations in a
rubric are validated with :class:`~decimal.Decimal` before a write is allowed.
"""
from __future__ import annotations

import copy
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.tools.problem_dedup import normalize_stem


class MajorQuestionSubpartV1(BaseModel):
    """Non-scoring metadata for one labelled part inside a major question."""

    model_config = ConfigDict(extra="forbid")

    subpart_id: str = Field(pattern=r"^sp[1-9][0-9]*$")
    label: str = Field(min_length=1, max_length=32)
    order: int = Field(ge=0)
    stem: str = ""
    type_hint: str | None = Field(default=None, max_length=64)
    source_span_ids: list[str] = Field(default_factory=list, max_length=50)


class MajorQuestionStructureV1(BaseModel):
    """Versioned metadata attached to exactly one scored question record."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal[1] = 1
    scoring_unit: Literal["major_question"] = "major_question"
    major_number: str = Field(default="", max_length=64)
    major_order: int = Field(ge=0)
    shared_stem: str = ""
    subparts: list[MajorQuestionSubpartV1] = Field(default_factory=list, max_length=100)
    structure_source: Literal[
        "deterministic", "bounded_repair", "legacy_single_question"
    ] = "deterministic"
    review_status: Literal["confirmed", "needs_review"] = "confirmed"

    @model_validator(mode="after")
    def _unique_order_and_labels(self):
        orders = [part.order for part in self.subparts]
        labels = [_normalize_label(part.label) for part in self.subparts]
        if orders != list(range(len(self.subparts))):
            raise ValueError("subpart orders must be contiguous from zero")
        if len(labels) != len(set(labels)):
            raise ValueError("subpart labels must be unique")
        return self


class RubricPointItemV1(BaseModel):
    """One explicit subpart allocation parsed from teacher-visible rubric text."""

    model_config = ConfigDict(extra="forbid")

    subpart_id: str = Field(pattern=r"^sp[1-9][0-9]*$")
    label: str
    points: str = Field(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$")


class RubricPointSummaryV1(BaseModel):
    """Derived, non-authoritative summary used by API and frontend validation."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal[1] = 1
    has_explicit_subpart_points: bool = False
    items: list[RubricPointItemV1] = Field(default_factory=list)
    total_points: str | None = None
    major_max_score: str
    is_valid: bool = True
    issue_code: Literal[
        "rubric_subpart_points_incomplete",
        "rubric_subpart_points_duplicate",
        "rubric_subpart_points_mismatch",
    ] | None = None


class QuestionRubricValidationError(ValueError):
    """Stable validation failure for an explicit, inconsistent rubric."""

    def __init__(self, summary: RubricPointSummaryV1):
        self.summary = summary
        super().__init__(summary.issue_code or "rubric_subpart_points_invalid")


_PAREN_LABEL_RE = re.compile(
    r"(?P<label>[（(](?:[0-9]{1,2}|[A-Za-z]|[ivxIVX]{1,6})[)）])"
)
_PURE_LABEL_RE = re.compile(
    r"^\s*(?P<label>[（(](?:[0-9]{1,2}|[A-Za-z]|[ivxIVX]{1,6})[)）])"
    r"\s*[.、:：-]?\s*$"
)
_COMPOSITE_NUMBER_RE = re.compile(
    r"^\s*(?P<parent>.+?)\s*(?P<label>[（(](?:[0-9]{1,2}|[A-Za-z]|[ivxIVX]{1,6})[)）])"
    r"\s*[.、:：-]?\s*$"
)
_POINT_TOKEN = r"(?P<points>(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?)\s*(?:分|points?|pts?)"


def _normalize_label(label: str) -> str:
    value = unicodedata.normalize("NFKC", str(label or "")).strip().casefold()
    value = value.strip(".、:：- ")
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    return value


def _canonical_label(label: str) -> str:
    return f"({_normalize_label(label)})"


def _label_family_and_value(label: str) -> tuple[str, int] | None:
    token = _normalize_label(label)
    if token.isdigit():
        return ("number", int(token))
    roman_values = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5,
                    "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}
    if token in roman_values:
        return ("roman", roman_values[token])
    if len(token) == 1 and "a" <= token <= "z":
        return ("letter", ord(token) - ord("a") + 1)
    return None


def _select_subpart_matches(stem: str) -> list[re.Match[str]]:
    """Return a conservative sequential marker run.

    A lone parenthesised token is too easy to confuse with mathematics.  Two
    or more unique labels beginning at 1/a/i are required before metadata is
    emitted.  This never changes the authoritative stem.
    """

    matches = list(_PAREN_LABEL_RE.finditer(stem or ""))
    if len(matches) < 2:
        return []
    for start in range(len(matches)):
        first = _label_family_and_value(matches[start].group("label"))
        if first is None or first[1] != 1:
            continue
        family = first[0]
        selected = [matches[start]]
        expected = 2
        for match in matches[start + 1:]:
            parsed = _label_family_and_value(match.group("label"))
            if parsed == (family, expected):
                selected.append(match)
                expected += 1
            elif parsed and parsed[0] == family and parsed[1] == 1:
                break
        if len(selected) >= 2:
            return selected
    return []


def build_major_question_structure(
    problem: Mapping[str, Any],
    *,
    major_order: int,
    structure_source: Literal[
        "deterministic", "bounded_repair", "legacy_single_question"
    ] = "deterministic",
    review_status: Literal["confirmed", "needs_review"] = "confirmed",
) -> MajorQuestionStructureV1:
    stem = str(problem.get("stem") or "")
    matches = _select_subpart_matches(stem)
    shared_stem = stem[:matches[0].start()].strip() if matches else stem
    subparts: list[MajorQuestionSubpartV1] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(stem)
        subparts.append(MajorQuestionSubpartV1(
            subpart_id=f"sp{index + 1}",
            label=_canonical_label(match.group("label")),
            order=index,
            stem=stem[match.end():end].strip(),
        ))
    return MajorQuestionStructureV1(
        major_number=str(problem.get("number") or ""),
        major_order=major_order,
        shared_stem=shared_stem,
        subparts=subparts,
        structure_source=structure_source,
        review_status=review_status,
    )


def _split_display_number(number: Any) -> tuple[str | None, str | None]:
    text = unicodedata.normalize("NFKC", str(number or "")).strip()
    pure = _PURE_LABEL_RE.fullmatch(text)
    if pure:
        return (None, _canonical_label(pure.group("label")))
    composite = _COMPOSITE_NUMBER_RE.fullmatch(text)
    if composite:
        return (
            composite.group("parent").strip().rstrip(".、:：- "),
            _canonical_label(composite.group("label")),
        )
    return (text, None)


def _append_distinct(base: str, addition: str) -> str:
    if not addition.strip():
        return base
    if not base.strip():
        return addition
    base_norm = normalize_stem(base)
    addition_norm = normalize_stem(addition)
    if addition_norm and addition_norm in base_norm:
        return base
    return f"{base.rstrip()}\n\n{addition.lstrip()}"


def _merge_major_rows(target: dict[str, Any], row: Mapping[str, Any], label: str) -> None:
    labelled_stem = str(row.get("stem") or "")
    if label and normalize_stem(label) not in normalize_stem(labelled_stem[:16]):
        labelled_stem = f"{label} {labelled_stem}".strip()
    target["stem"] = _append_distinct(str(target.get("stem") or ""), labelled_stem)
    for field in ("criterion", "reference_answer"):
        value = str(row.get(field) or "").strip()
        if value:
            target[field] = _append_distinct(
                str(target.get(field) or ""), f"{label} {value}".strip()
            )
    target["preparation_issues"] = [
        *list(target.get("preparation_issues") or []),
        *list(row.get("preparation_issues") or []),
    ]
    if target.get("type") != row.get("type"):
        target["type"] = "其他"


def collapse_explicit_subpart_rows(
    problems: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Collapse model-emitted subpart rows into one major-question row.

    This is a defensive gate for outputs such as ``1(a)``, ``1(b)``, ``2``.
    It does not split already-correct major-question rows.  A leading run of
    pure ``(a)/(b)`` rows is only inferred as major question 1 when the next
    explicit top-level row is integer question 2; the result remains marked
    for teacher review.
    """

    rows = [copy.deepcopy(dict(row)) for row in problems.values()]

    # Bounded recovery for the reported failure shape: the model returned
    # leading ``(a)``, ``(b)`` (or ``(i)``, ``(ii)``) rows and then major
    # question 2.  Unlike parenthesised numbers, a sequential letter/roman run
    # immediately before explicit question 2 is strong enough to infer one
    # review-required major question 1.  No other unanchored run is guessed.
    leading: list[tuple[dict[str, Any], str, tuple[str, int]]] = []
    for row in rows:
        parent, label = _split_display_number(row.get("number"))
        parsed = _label_family_and_value(label or "") if parent is None and label else None
        if parsed is None:
            break
        leading.append((row, label or "", parsed))
    if (
        len(leading) >= 2
        and leading[0][2][0] in {"letter", "roman"}
        and [item[2] for item in leading]
        == [(leading[0][2][0], index) for index in range(1, len(leading) + 1)]
        and len(rows) > len(leading)
        and str(rows[len(leading)].get("number") or "").strip().rstrip(".、") == "2"
    ):
        recovered = leading[0][0]
        recovered["number"] = "1"
        recovered["stem"] = (
            f"{leading[0][1]} {str(recovered.get('stem') or '').lstrip()}".strip()
        )
        recovered["_structure_review_status"] = "needs_review"
        for row, label, _ in leading[1:]:
            _merge_major_rows(recovered, row, label)
        rows = [recovered, *rows[len(leading):]]

    collapsed: list[dict[str, Any]] = []
    # Pure ``(1)``/``(a)`` numbering is ambiguous without a preceding major
    # row.  Only a normal/composite major row may become an attachment anchor;
    # otherwise the parenthesised row remains an independent major question.
    active_parent: int | None = None
    active_family: str | None = None
    expected_subpart_value = 1
    for row in rows:
        parent, label = _split_display_number(row.get("number"))
        if label is None:
            collapsed.append(row)
            active_parent = len(collapsed) - 1
            active_family = None
            expected_subpart_value = 1
            continue
        if parent:
            if collapsed and str(collapsed[-1].get("number") or "") == parent:
                _merge_major_rows(collapsed[-1], row, label)
                active_parent = len(collapsed) - 1
            else:
                row["number"] = parent
                row["stem"] = f"{label} {str(row.get('stem') or '').lstrip()}".strip()
                row["_structure_review_status"] = "needs_review"
                collapsed.append(row)
                active_parent = len(collapsed) - 1
            active_family = None
            expected_subpart_value = 1
            continue
        parsed_label = _label_family_and_value(label)
        can_attach = (
            active_parent is not None
            and parsed_label is not None
            and parsed_label[1] == expected_subpart_value
            and (active_family is None or parsed_label[0] == active_family)
        )
        if can_attach:
            _merge_major_rows(collapsed[active_parent], row, label)
            collapsed[active_parent]["_structure_review_status"] = "needs_review"
            active_family = parsed_label[0]
            expected_subpart_value += 1
        else:
            # No parent evidence: preserving a possible top-level ``(1)`` is
            # safer than silently merging distinct scored questions.
            collapsed.append(row)
            active_parent = None
            active_family = None
            expected_subpart_value = 1

    result: Dict[str, Dict[str, Any]] = {}
    for index, row in enumerate(collapsed, start=1):
        q_id = f"q{index}"
        row["q_id"] = q_id
        for issue in row.get("preparation_issues") or []:
            if isinstance(issue, dict):
                issue["q_id"] = q_id
        result[q_id] = row
    return result


def annotate_major_question_structures(
    problems: Mapping[str, Mapping[str, Any]],
    *,
    structure_source: Literal["deterministic", "bounded_repair"] = "deterministic",
) -> Dict[str, Dict[str, Any]]:
    """Return one normalised record per major question with versioned metadata."""

    collapsed = collapse_explicit_subpart_rows(problems)
    result: Dict[str, Dict[str, Any]] = {}
    for order, (q_id, raw) in enumerate(collapsed.items()):
        row = dict(raw)
        review_status = row.pop("_structure_review_status", "confirmed")
        structure = build_major_question_structure(
            row,
            major_order=order,
            structure_source=structure_source,
            review_status=review_status,
        )
        row["question_structure"] = structure.model_dump()
        result[q_id] = row
    inspect_major_question_set(result)
    return result


def inspect_major_question_set(problems: Mapping[str, Mapping[str, Any]]) -> None:
    """Reject any candidate that still exposes a subpart as a scored row."""

    for expected_order, (q_id, problem) in enumerate(problems.items(), start=1):
        if q_id != f"q{expected_order}":
            raise ValueError("question_structure_non_contiguous_q_id")
        MajorQuestionStructureV1.model_validate(problem.get("question_structure"))


def _score_decimal(value: Any) -> Decimal:
    try:
        score = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("score_not_decimal") from exc
    if not score.is_finite() or score <= 0:
        raise ValueError("score_not_positive_finite")
    return score


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _allocation_marker_pattern(label: str) -> str:
    token = re.escape(_normalize_label(label))
    parenthesized = rf"[（(]\s*{token}\s*[)）]"
    return (
        rf"(?:第\s*{parenthesized}\s*问|"
        rf"(?:part|小问)\s*{parenthesized}|"
        rf"{parenthesized})"
    )


def _parse_allocations(
    text: str,
    subparts: list[MajorQuestionSubpartV1],
) -> dict[str, list[Decimal]]:
    """Parse one bounded text segment after each known subpart marker.

    Segmenting at the next known marker supports common forms such as
    ``(a) 方法正确，4分`` and prevents the score for ``(b)`` from being
    attributed to ``(a)``.  Spans are unique, so ``Part (a)`` cannot be
    double-counted by overlapping regular expressions.
    """

    occurrences: list[tuple[int, int, str]] = []
    for part in subparts:
        pattern = _allocation_marker_pattern(part.label)
        occurrences.extend(
            (match.start(), match.end(), part.subpart_id)
            for match in re.finditer(pattern, text or "", flags=re.IGNORECASE)
        )
    occurrences.sort(key=lambda item: (item[0], item[1]))
    values: dict[str, list[Decimal]] = {part.subpart_id: [] for part in subparts}
    seen_spans: set[tuple[int, int, str]] = set()
    for index, (start, end, subpart_id) in enumerate(occurrences):
        span_key = (start, end, subpart_id)
        if span_key in seen_spans:
            continue
        seen_spans.add(span_key)
        segment_end = (
            occurrences[index + 1][0]
            if index + 1 < len(occurrences)
            else min(len(text), end + 240)
        )
        segment = text[end:segment_end]
        point_matches = list(re.finditer(_POINT_TOKEN, segment, flags=re.IGNORECASE))
        component_scores: list[Decimal] = []
        for match in point_matches:
            prefix = segment[max(0, match.start() - 20):match.start()].casefold()
            if re.search(r"(?:总分|合计|total)\s*[:：=为-]?\s*$", prefix):
                continue
            component_scores.append(Decimal(match.group("points")))
        if component_scores:
            # Multiple point-bearing criteria inside one labelled item are
            # components of that subpart, not duplicate subpart declarations.
            values[subpart_id].append(sum(component_scores, Decimal("0")))
    return values


def summarize_rubric_points(
    criterion: str,
    max_score: Any,
    question_structure: Mapping[str, Any] | MajorQuestionStructureV1 | None,
) -> RubricPointSummaryV1:
    """Parse explicit subpart points and report exact-sum validity.

    Percentage rubrics, objective-question prose, and free text with no
    recognised subpart point allocations stay valid for backwards
    compatibility.  Once any known subpart has an absolute allocation, every
    known subpart must appear exactly once and the exact Decimal sum must equal
    the major-question maximum.
    """

    maximum = _score_decimal(max_score)
    structure = (
        question_structure
        if isinstance(question_structure, MajorQuestionStructureV1)
        else MajorQuestionStructureV1.model_validate(question_structure)
        if question_structure
        else MajorQuestionStructureV1(
            major_order=0,
            structure_source="legacy_single_question",
            review_status="needs_review",
        )
    )
    allocations = _parse_allocations(criterion or "", structure.subparts)
    parsed: list[tuple[MajorQuestionSubpartV1, list[Decimal]]] = [
        (part, allocations[part.subpart_id]) for part in structure.subparts
    ]
    if not any(values for _, values in parsed):
        return RubricPointSummaryV1(
            has_explicit_subpart_points=False,
            major_max_score=_decimal_text(maximum),
        )

    duplicate = any(len(values) > 1 for _, values in parsed)
    incomplete = any(len(values) == 0 for _, values in parsed)
    items = [
        RubricPointItemV1(
            subpart_id=part.subpart_id,
            label=part.label,
            points=_decimal_text(values[0]),
        )
        for part, values in parsed
        if values
    ]
    total = sum((Decimal(item.points) for item in items), Decimal("0"))
    issue_code = None
    if duplicate:
        issue_code = "rubric_subpart_points_duplicate"
    elif incomplete:
        issue_code = "rubric_subpart_points_incomplete"
    elif total != maximum:
        issue_code = "rubric_subpart_points_mismatch"
    return RubricPointSummaryV1(
        has_explicit_subpart_points=True,
        items=items,
        total_points=_decimal_text(total),
        major_max_score=_decimal_text(maximum),
        is_valid=issue_code is None,
        issue_code=issue_code,
    )


def validate_rubric_points(
    criterion: str,
    max_score: Any,
    question_structure: Mapping[str, Any] | MajorQuestionStructureV1 | None,
) -> RubricPointSummaryV1:
    summary = summarize_rubric_points(criterion, max_score, question_structure)
    if not summary.is_valid:
        raise QuestionRubricValidationError(summary)
    return summary


def presentation_question_structure(source: Mapping[str, Any] | None) -> dict | None:
    presentation = dict((source or {}).get("presentation") or {})
    value = presentation.get("question_structure")
    if not value:
        return None
    return MajorQuestionStructureV1.model_validate(value).model_dump()

