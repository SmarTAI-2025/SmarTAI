"""Recognize explicit scoring clauses, never a bare mention of working.

This is a bounded source-rule check, not a general natural-language parser.
Conditional/ambiguous wording stays unspecified and the full source is retained
by callers. It does not decide whether a student's reasoning is correct.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ProcessCreditPolicy:
    mode: Literal["allowed", "forbidden", "unspecified"] = "unspecified"
    # Distinct from banning working marks: multiple-choice subset marks are
    # still result-based unless the source explicitly requires all or nothing.
    all_or_nothing: bool = False


_WORK = (r"(?:(?:正确|有效|完整|简要)(?:的)?)?(?:解题)?"
         r"(?:过程|步骤|推导|解释|方法|理由)(?:正确|有效)?")
_AMOUNT = r"(?P<amount>\d+(?:\.\d+)?)\s*(?:分|points?|marks?)"
_ALLOCATION = re.compile(
    rf"(?:(?:写出|给出|给|计)?{_WORK}|每(?:一)?步)\s*(?:分)?\s*(?:[（(]\s*)?"
    rf"(?:各|每步)?(?:给|得|计|占|可得|为|是)?\s*[:：]?\s*{_AMOUNT}\s*[）)]?"
    rf"|(?:working|process|reasoning|explanation|method|steps?|each step)"
    rf"\s*(?:is worth|worth|:)?\s*(?P<english_amount>\d+(?:\.\d+)?)\s*(?:points?|marks?)",
    re.I,
)
_PERMISSION = re.compile(
    rf"(?:按{_WORK}(?:给分|计分|评分)|(?:允许|可以|可)(?:给|计)?{_WORK}分)"
    r"|(?:award|allow|give) (?:partial |process |working )?credit for "
    r"(?:working|reasoning|explanation|steps)", re.I,
)
_PROHIBITION = re.compile(
    rf"(?:不计|不算|不考虑|不给|不允许|禁止|严禁)(?:任何)?{_WORK}(?:分|得分)?"
    rf"|(?:不允许|禁止|严禁)按{_WORK}(?:给分|计分|评分)"
    rf"|{_WORK}(?:不计分|不给分|不得分|不评分)"
    r"|(?:只|仅)(?:看|按|计|依据)(?:最终)?(?:答案|结果)(?:给分|计分|评分)?"
    r"|(?:do not|never) (?:award|give) (?:process|working|reasoning) (?:credit|marks|points)"
    r"|no (?:process|working|reasoning) (?:credit|marks|points)"
    r"|(?:only (?:grade|score|consider) (?:the )?(?:final )?answer|final[- ]answer only)", re.I,
)
_WRONG_ZERO = re.compile(
    r"(?:最终)?答案(?:错误|不正确|错)(?:就|就是|即|则)?(?:得|计|给)?\s*0\s*分"
    r"|(?:答错|答错误)(?:就|即|则)?(?:得|计|给)?\s*0\s*分"
    r"|(?:incorrect|wrong) (?:final )?answers? (?:receive|get|score) 0 (?:points?|marks?)", re.I,
)
_CONDITIONAL = re.compile(r"如果|若|除非|否则|仅当|只有|\bif\b|\bunless\b|only when\b", re.I)


def _unconditional_clauses(text: str):
    for sentence in re.split(r"[；;。\n]|(?<!\d)\.(?!\d)", text):
        # Never detach an if-prefix from its consequent. A separate condition
        # must not erase an earlier complete, unconditional source rule either.
        if not _CONDITIONAL.search(sentence):
            yield from re.split(r"[，,、]", sentence)


def process_credit_policy(criterion: str) -> ProcessCreditPolicy:
    text = (criterion or "").strip()
    allowed = forbidden = zero_allocation = all_or_nothing = False
    for clause in _unconditional_clauses(text):
        clause = clause.strip().rstrip(".!！")
        # A list prefix is presentation, not part of its scoring rule. Require
        # whitespace after a decimal-style prefix so 2.5 is never stripped.
        clause = re.sub(r"^(?:\d+[.)]\s+|[（(]\d+[）)]\s*|[①-⑳]\s*|[-*•]\s+)", "", clause)
        clause = re.sub(r"^本题\s*", "", clause)
        if _WRONG_ZERO.fullmatch(clause):
            all_or_nothing = forbidden = True
        elif _PROHIBITION.fullmatch(clause):
            forbidden = True
        elif match := _ALLOCATION.fullmatch(clause):
            if float(match["amount"] or match["english_amount"]) > 0:
                allowed = True
            else:
                zero_allocation = True
        elif _PERMISSION.fullmatch(clause):
            allowed = True
    return ProcessCreditPolicy(
        "forbidden" if forbidden else "allowed" if allowed else
        "forbidden" if zero_allocation else "unspecified",
        all_or_nothing=all_or_nothing,
    )
