"""Recognize explicit whole-question zero/full rules, never infer from a type.

This finite check only installs a constraint when the complete source has an
unqualified correct/full + otherwise/zero clause and no competing allocation.
The model still judges correctness; this code never compares answer strings.
"""
from __future__ import annotations

import re
from decimal import Decimal

_NUMBER = r"\d+(?:\.\d+)?"
_CORRECT = (r"(?:(?:答案|选项|结果)(?:完全)?正确|答对|选对|全(?:部)?选对"
            r"|选(?:择|项)?\s*[A-H]\s*(?:[（(][^）)\n]+[）)])?)")
_WRONG = r"(?:否则|其他(?:情况)?|答错|选错|(?:答案|选项|结果)(?:错误|不正确))"
_CN = re.compile(
    rf"(?:^|[。；;\n])\s*(?:[-*•]\s*)?(?:(?:评分标准|评分规则|答案唯一)[:：]\s*)?(?:本题)?"
    rf"(?P<rule>{_CORRECT}\s*(?:得|给|计|获)?\s*(?:满分|(?P<points>{_NUMBER})\s*分)"
    rf"\s*[，,；;。]\s*{_WRONG}\s*(?:(?:得|给|计)?\s*0\s*分|不得分|不给分))"
    rf"(?:[；;]\s*不要求过程)?(?:[:：]\s*(?P<rendered_points>{_NUMBER})\s*分)?"
    r"(?=$|[。；;\n])", re.I,
)
_EN = re.compile(
    rf"(?:^|[.;\n])\s*(?:[-*•]\s*)?(?:rubric:\s*)?"
    rf"(?P<rule>(?:correct(?: answer)?|(?:choose|select) [A-H])\s*"
    rf"(?:gets?|earns?|receives?|scores?|:)?\s*"
    rf"(?:full (?:marks?|credit)|(?P<points>{_NUMBER})\s*(?:points?|marks?))"
    rf"\s*[,;]\s*(?:otherwise|(?:incorrect|wrong)(?: answer)?)\s*"
    rf"(?:gets?|earns?|receives?|scores?|:)?\s*(?:0\s*(?:points?|marks?)|no credit))"
    rf"(?:[:：]\s*(?P<rendered_points>{_NUMBER})\s*(?:points?|marks?))?"
    r"(?=$|[.;\n])", re.I,
)
# Do not turn a subpart, an exception, or partial-credit language into a global
# rule. Unknown source remains under the existing full rubric interpretation.
_SCOPED = re.compile(
    r"每(?:个)?空|第.{0,8}(?:空|问|小题)|部分|漏选|酌情|除非|如果|仅当|只有|若"
    r"|不(?:采用|执行|适用)|并非|不是|示例|例如|假设|未作答|空白|空答"
    r"|\b(?:partial|subset|unless|except|if|each|blank|subpart|example|hypothetical)\b"
    r"|do not (?:use|apply)|not applicable", re.I,
)
_ALLOCATION = re.compile(
    rf"(?:得|给|计|扣|占|各)\s*(?:满分|{_NUMBER}\s*(?:分|%|％))"
    rf"|(?:award|deduct|worth|earns?|gets?|receives?)\s+{_NUMBER}\s*(?:points?|marks?)", re.I,
)


def binary_rule(criterion: str, maximum: float) -> str | None:
    """Return the exact unambiguous source clause, otherwise no override."""
    for pattern in (_CN, _EN):
        match = pattern.search(criterion or "")
        if not match:
            continue
        if any(match[name] and Decimal(match[name]) != Decimal(str(maximum))
               for name in ("points", "rendered_points")):
            continue
        rest = criterion[:match.start()] + criterion[match.end():]
        if _SCOPED.search(rest) or _ALLOCATION.search(rest):
            continue
        # The renderer's redundant amount is consumed above. ANY additional
        # numeric allocation, even another full-size item, needs rubric judgment.
        if re.search(rf"{_NUMBER}\s*(?:分(?!钟)|%|％|points?\b|marks?\b)", rest, re.I):
            continue
        return match["rule"]
    return None
