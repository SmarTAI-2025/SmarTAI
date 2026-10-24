"""Conservative, source-scoped zero/full policies for the shared grader.

A binary clause is not a whole-question policy. Only a complete supported
policy document can establish that scope. Unparsed text (including exceptions,
other allocations and conditions) leaves settlement to the existing rubric.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from backend.services.math_rubric_plan import _NUMBER, _number_value

_CORRECT = (r"(?:(?:答案|选项|结果)(?:完全)?正确|答对|选对|全(?:部)?选对"
            rf"|选(?:择|项)?\s*[A-H]\s*(?:[（(]\s*{_NUMBER}\s*[）)])?)")
_WRONG = r"(?:否则|其他(?:情况)?|答错|选错|(?:答案|选项|结果)(?:错误|不正确))"
_CN = (
    rf"(?P<rule>{_CORRECT}\s*(?:得|给|计|获)?\s*(?:满分|(?P<points>{_NUMBER})\s*分)"
    rf"\s*[，,；;。]\s*{_WRONG}\s*(?:(?:得|给|计)?\s*(?:0|零|〇)\s*分|不得分|不给分))"
    rf"(?:[；;]\s*不要求过程)?(?:[:：]\s*(?P<rendered_points>{_NUMBER})\s*分)?"
)
_EN = (
    rf"(?P<rule>(?:correct(?: answer)?|(?:choose|select) [A-H])\s*"
    rf"(?:gets?|earns?|receives?|scores?|:)?\s*"
    rf"(?:full (?:marks?|credit)|(?P<points>{_NUMBER})\s*(?:points?|marks?))"
    rf"\s*[,;]\s*(?:otherwise|(?:incorrect|wrong)(?: answer)?)\s*"
    rf"(?:gets?|earns?|receives?|scores?|:)?\s*(?:0\s*(?:points?|marks?)|no credit))"
    rf"(?:[:：]\s*(?P<rendered_points>{_NUMBER})\s*(?:points?|marks?))?"
)
# Whole-document grammar, not a search followed by a list of forbidden words.
# The optional renderer envelope must itself declare whole-question scope.
# Every other line must be one of these complete non-allocating statements;
# arbitrary route labels/notes are not trusted to be harmless metadata.
_HEADER = (r"(?:(?:正确选项及结果|正确选项|最终答案|答案|结果)\s*—\s*"
           r"(?:所有作答|全题|本题)\s*\n\s*)?")
_NOTES = (rf"(?:\s*\n\s*(?:只要求选项，无过程分|不要求过程|"
          rf"结果为\s*{_NUMBER})[。.]?)*")
_DOCUMENTS = (
    re.compile(r"\s*" + _HEADER + r"(?:[-*•]\s*)?"
        r"(?:(?:评分标准|评分规则|答案唯一)[:：]\s*)?(?:本题)?" + _CN
        + r"[。；;]?" + _NOTES + r"\s*", re.I),
    re.compile(r"\s*(?:[-*•]\s*)?(?:rubric:\s*)?" + _EN + r"[.;]?\s*", re.I),
)


@dataclass(frozen=True)
class BinaryScoringPolicy:
    scope: Literal["question"]
    source_quote: str
    correct_score: Decimal
    incorrect_score: Decimal = Decimal(0)


def whole_question_policy(criterion: str, maximum: float) -> BinaryScoringPolicy | None:
    """Prove complete source coverage before assigning question-wide scope.

    Composite/scoped/unrecognized policies remain in MathRubricPlan, with their
    original applicability and exceptions. No partial clause is promoted here.
    """
    for pattern in _DOCUMENTS:
        match = pattern.fullmatch(criterion or "")
        if not match:
            continue
        try:
            if any(match[name] and _number_value(match[name]) != Decimal(str(maximum))
                   for name in ("points", "rendered_points")):
                continue
        except ValueError:
            continue
        return BinaryScoringPolicy("question", match["rule"], Decimal(str(maximum)))
    return None


def binary_rule(criterion: str, maximum: float) -> str | None:
    policy = whole_question_policy(criterion, maximum)
    return policy.source_quote if policy else None
