"""Recognize explicit scoring clauses, never a bare mention of working.

This is a bounded source-rule check, not a general natural-language parser.
Unresolved scoring language is distinct from an absent policy. Process marks,
result-component marks and whole-question binary scoring are separate decisions.
It does not decide whether a student's reasoning is correct.
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
    result_parts: bool = False
    unresolved: bool = False
    conditional_process: bool = False
    process_scoring: bool = False

    @property
    def interpretation(self) -> str:
        return "unresolved" if self.unresolved and self.mode == "unspecified" else self.mode

    @property
    def uses_rubric_score(self) -> bool:
        return self.mode == "allowed" or self.result_parts or (self.unresolved and self.mode != "forbidden")


_WORK = (r"(?:(?:正确|有效|完整|简要)(?:的)?)?"
         r"(?:(?:计算|解题|演算|推理|中间|推导)(?:的)?)?"
         r"(?:过程|步骤|推导|解释|方法|理由)(?:正确|有效)?")
_UNIT = r"(?:分(?!钟)|%|％|points?\b|marks?\b)"
_AMOUNT = rf"(?P<amount>\d+(?:\.\d+)?)\s*{_UNIT}"
_ALLOCATION = re.compile(
    rf"(?:(?:写出|给出|给|计)?{_WORK}|每(?:一)?步)\s*(?:分)?\s*(?:[（(]\s*)?"
    rf"(?:各|每步)?(?:给|得|计|占|可得|为|是)?\s*[:：]?\s*{_AMOUNT}\s*[）)]?"
    rf"|(?:working|process|reasoning|explanation|method|steps?|each step)"
    rf"\s*(?:is worth|worth|:)?\s*(?P<english_amount>\d+(?:\.\d+)?)\s*{_UNIT}",
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
_CORRECT_CONDITION = re.compile(r"(?:如果|若|仅当|只有)(?:最终)?(?:答案|结论|结果)正确")
_RESULT_PART = re.compile(
    rf"(?:每(?:个)?空|[一二三四五六七八九十两\d]+(?:个)?空各|第[一二三四五六七八九十\d]+空"
    rf"|each blank|blank \d+)\s*(?:答对|正确)?\s*(?:得|给|计|占|各)?\s*[:：]?\s*"
    rf"\d+(?:\.\d+)?\s*{_UNIT}", re.I,
)
_WORK_DEDUCTION = re.compile(
    rf"(?:{_WORK}(?:缺失|缺少|遗漏|错误|有误|不完整)?|(?:缺失|缺少|未写){_WORK})"
    rf"\s*(?:则|要|应)?扣\s*(?P<amount>\d+(?:\.\d+)?)\s*{_UNIT}"
    r"|(?:deduct|lose) (?P<english_amount>\d+(?:\.\d+)?) (?:points?|marks?) "
    r"for (?:missing|incorrect) (?:working|process|steps?|reasoning|explanation|method)", re.I,
)
_NO_DEDUCTION = re.compile(rf"{_WORK}(?:缺失|错误|有误)?不扣分")
_NON_PERMISSION = re.compile(r"(?:不禁止|并非不计)过程分")
_WORK_MENTION = re.compile(_WORK + r"|\b(?:working|process|steps?|reasoning|explanation|method)\b", re.I)
_SCORING_LANGUAGE = re.compile(
    rf"\d+(?:\.\d+)?\s*{_UNIT}|给分|计分|评分|扣分|过程分|\b(?:credit|points?|marks?|deduct)\b", re.I,
)


def _clauses(text: str):
    for sentence in re.split(r"[；;。\n]|(?<!\d)\.(?!\d)", text):
        condition = None
        for clause in re.split(r"[，,、]", sentence):
            clause = clause.strip().rstrip(".!！")
            clause = re.sub(r"^(?:\d+[.)]\s+|[（(]\d+[）)]\s*|[①-⑳]\s*|[-*•]\s+)", "", clause)
            clause = re.sub(r"^(?:评分标准|评分规则|评分细则|rubric|scoring rubric)\s*[:：]\s*", "", clause, flags=re.I)
            clause = re.sub(r"^本题\s*", "", clause)
            if _CONDITIONAL.search(clause):
                condition = clause
            # Conditions bind forward, never erase earlier independent rules.
            yield clause, condition


def process_credit_policy(criterion: str) -> ProcessCreditPolicy:
    text = (criterion or "").strip()
    allowed = forbidden = zero_allocation = all_or_nothing = False
    result_parts = unresolved = conditional_process = process_scoring = False
    for clause, condition in _clauses(text):
        if condition:
            # A stated prerequisite is not an unconditional grant. The existing
            # final-answer verdict can enforce an explicit correctness condition.
            if _CORRECT_CONDITION.fullmatch(condition) and _ALLOCATION.fullmatch(clause):
                conditional_process = True
            elif _WORK_MENTION.search(clause) and re.search(r"\d", clause):
                unresolved = True
            continue
        if _WRONG_ZERO.fullmatch(clause):
            all_or_nothing = forbidden = True
        elif _PROHIBITION.fullmatch(clause):
            forbidden = True
        elif _RESULT_PART.match(clause):
            result_parts = True
            # A trailing qualification needs rubric judgment; it must not turn
            # an explicit per-result allocation into whole-question 0/full.
        elif match := _ALLOCATION.fullmatch(clause):
            if float(match["amount"] or match["english_amount"]) > 0:
                allowed = process_scoring = True
            else:
                zero_allocation = True
        elif match := _WORK_DEDUCTION.fullmatch(clause):
            if float(match["amount"] or match["english_amount"]) > 0:
                allowed = process_scoring = True
        elif _PERMISSION.fullmatch(clause):
            allowed = process_scoring = True
        elif _NO_DEDUCTION.fullmatch(clause) or _NON_PERMISSION.fullmatch(clause):
            continue
        elif _WORK_MENTION.search(clause) and _SCORING_LANGUAGE.search(clause):
            # Not recognizing a scoring instruction is NOT evidence that the
            # teacher omitted it. Retain source-based grading, not a 0/full rewrite.
            unresolved = True
    return ProcessCreditPolicy(
        "forbidden" if forbidden else "allowed" if allowed else
        "forbidden" if zero_allocation else "unspecified",
        all_or_nothing=all_or_nothing and not result_parts,
        result_parts=result_parts, unresolved=unresolved,
        conditional_process=conditional_process and not forbidden, process_scoring=process_scoring,
    )


def conflicts_with_process_prohibition(text: str, *, points: float = 0, mode: str = "points") -> bool:
    """Check a proposed scoring effect, including a structured bare item label.

    Under a known source prohibition an unresolved working-related rule cannot be
    installed as an executable ledger either. The caller preserves the whole source.
    """
    policy = process_credit_policy(text)
    if policy.mode == "forbidden" and not policy.process_scoring and not policy.unresolved:
        return False  # A preserved prohibition is not a positive scoring effect.
    candidates = [text]
    if points > 0:
        candidates += ([f"{text}{points:g}分", f"{text} {points:g} points"] if mode == "points"
                       else [f"{text}扣{points:g}分", f"deduct {points:g} points for missing {text}"])
    return any((policy := process_credit_policy(candidate)).process_scoring
               or policy.conditional_process or policy.unresolved
               for candidate in candidates)
