"""Bind explicit teacher allocations to their scored conditions, never just to a sum."""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

from backend.services.math_source_evidence import match_source_quote
from backend.services.question_structure import _allocation_marker_pattern


def _words(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def _allocation_clauses(text):
    """Keep the source span for each assignment, including shared-unit lists."""
    from backend.services.math_rubric_plan import _AMOUNT, _NUMBER, _scoring_numeric_text, _source_clauses

    # An amount closes a postfix award ("setup 3 points ... result 7 points").
    # For prefix awards ("3 points for setup and 7 points for result") the
    # boundary instead precedes the next amount. Never split inside a number.
    join = r"(?:以及|并且|然后|而且|而|并|及|与|和|且|另|\band\b|\bwhile\b|\bthen\b)"
    for sentence in _source_clauses(text):
        normalized = unicodedata.normalize("NFKC", sentence)
        parallel = re.fullmatch(r"(.+?)分别(?:占|为|得|计|给)?\s*(.+)", normalized)
        if parallel:
            names = re.split(rf"\s*{join}\s*", parallel[1])
            values = re.split(rf"\s*{join}\s*", parallel[2])
            unit = re.search(r"(%|分|points?|marks?)$", values[-1], re.I)
            if len(names) > 1 and len(names) == len(values) and unit:
                resolved = [re.fullmatch(rf"({_NUMBER})\s*(%|分|points?|marks?)?", value, re.I)
                            for value in values]
                if all(resolved):
                    for name, value in zip(names, resolved):
                        yield sentence, name + " " + value[1] + (value[2] or unit[1])
                    continue
        amounts = list(_AMOUNT.finditer(_scoring_numeric_text(normalized)))
        if len(amounts) < 2:
            yield sentence, normalized
            continue
        start = 0
        for left, right in zip(amounts, amounts[1:]):
            bridge = normalized[left.end():right.start()]
            # An amount at the beginning or after "award" introduces the
            # condition that follows it (possibly followed by another award).
            if re.match(r"\s*(?:for\b|用于|给到)", bridge, re.I):
                boundary = re.search(rf"\s*{join}\s*(?:(?:award|give|deduct)\s*)?$", bridge, re.I)
                if boundary is None:
                    raise ValueError(f"Identify the separate funded conditions in «{sentence}».")
                stop = left.end() + boundary.start()
                following = left.end() + boundary.end()
            else:
                stop = left.end()
                connector = re.match(rf"\s*{join}\s*", bridge, re.I)
                following = stop + (connector.end() if connector else 0)
            yield normalized[start:stop], normalized[start:stop]
            start = following
        yield normalized[start:], normalized[start:]


def validate_teacher_point_items(rules, ordinary):
    from backend.services.math_rubric_plan import (
        _AMOUNT, _DEDUCTION, _amount_value, _score_context_clause, _scoring_numeric_text,
    )

    if not ordinary.strip():
        return {}
    parts = rules.parts
    scope = parts
    labels = [(p, re.compile(_allocation_marker_pattern(p.label), re.I)) for p in parts if p.label]
    anchors = {}
    for source_clause, clause in _allocation_clauses(ordinary):
        clause = re.sub(r"^(?:(?:参考解|标答解法|标答|主)?评分(?:标准|参考|说明)?|主标准|marking scheme|rubric)\s*:\s*",
                        "", clause, flags=re.I)
        # A part heading scopes its subsequent clauses. Internal mathematical
        # enumeration in a condition is not a new rubric heading.
        heading = next(((p, m) for p, pattern in labels if (m := pattern.match(clause))), None)
        if heading:
            part, marker = heading
            scope = (part,)
            clause = clause[marker.end():].strip(" ：:")
        if not clause:
            continue
        money = list(_AMOUNT.finditer(_scoring_numeric_text(clause)))
        if len(money) != 1:
            continue
        amount = money[0]
        # These amounts play other roles and have their own typed checks.
        if re.search(r"满分|总分|起[评分]|最多|至多|上限|最低|保底|至少|折算|沿用|后传|保留|"
                     r"粒度|(?:配分|评分|计分|给分|分值)单位|每(?:个)?小问|每(?:道)?题|[各每]步|各小问|每空|满打|空白|未作答|"
                     r"maximum|minimum|floor|at most|at least|follow.through|each part|per part|"
                     r"allocation unit|partial credit|otherwise|部分分", clause, re.I):
            continue
        if re.search(r"\d\s*/\s*$", clause[:amount.start()]) or clause[amount.end():].startswith(("之","钟")):
            continue
        if _score_context_clause(clause, ordinary, scope[0].total_pts if len(scope) == 1 else rules.total_pts):
            continue
        deduction = bool(_DEDUCTION.search(clause))
        prefix = clause[:amount.start()].rstrip(" (（:：=，,")
        suffix = clause[amount.end():].strip(" ()（）[]【】:：=.")
        amount_first = not prefix or re.fullmatch(r"(?:award|give|earn|deduct|扣除?|给|得)", prefix, re.I)
        condition = re.sub(r"^for\s+", "", suffix, flags=re.I) if amount_first else prefix
        condition = re.sub(r"(?:给分为|(?:可|应)?(?:给|得|计|占|扣除?)|\b(?:earns?|worth|receives?))\s*$",
                           "", condition, flags=re.I).strip()
        key = _words(condition)
        if not amount_first and suffix:
            # Keep qualifiers in the frozen obligation. Their presence after the
            # amount does not release the preceding named step from its budget.
            condition = f"{condition}（{suffix}）"
        if not key or not re.search(r"[^\d\W_]", key):
            continue
        if re.fullmatch(r"(?:给分|计分|分步|按步骤|各|共|按|得分|分值|每(?:一|个)?(?:错误|错|题|项)|正确结果|正确|"
                        r"答对|答错|错误|否则|其余|其他|full credit|total)", condition, re.I):
            continue
        if amount[1] in {"0", "零", "zero"}:
            continue  # A zero-credit condition is not a separately funded item.
        items = [(p, it) for p in scope for it in (p.deductions if deduction else p.steps)]
        named = [(p,it) for p,it in items if key in _words(it.criterion)]
        quoted = []
        for p, item in items:
            quote = item.source_quote
            if quote and (match_source_quote(quote, source_clause) or
                          (match_source_quote(source_clause, quote) and match_source_quote(quote, condition))):
                quoted.append((p, item))
        expected = _amount_value(*amount.groups(), scope[0].total_pts if len(scope) == 1 else rules.total_pts)
        group = []
        if not deduction and len(scope) > 1 and amount[2] in {"%", "％"}:
            # Unscoped percentages fund the named group across the question.
            group = named or quoted
            if not group and expected == Decimal(str(rules.total_pts)) and all(p.steps for p in scope):
                group = items
            if not group:
                raise ValueError(f"Identify the steps funded by «{clause}».")
        elif (not deduction and len(quoted) > 1 and len({p.id for p, _ in quoted}) == 1
              and not (named and "分别" in source_clause)):
            # A teacher award can fund several substeps. Each must cite this
            # specific allocation, not an entire sentence containing other awards.
            if all(match_source_quote(source_clause, it.source_quote) for _, it in quoted):
                group = quoted
        if group:
            actual = sum((Decimal(str(it.total_pts)) for _, it in group), Decimal(0))
            if actual != expected:
                raise ValueError(f"Steps funded by «{clause}» total {actual}; the teacher allocated {expected}.")
            for part, item in group:
                identity = (part.id, item.id)
                if identity in anchors:
                    raise ValueError(f"Duplicate teacher allocation for {part.id}/{item.id}: «{clause}».")
                description = item.criterion if key in _words(item.criterion) else f"{condition} · {item.criterion}"
                anchors[identity] = (description, source_clause.strip())
            continue
        matches = named or quoted or (items if len(items) == 1 else [])
        if len(matches) > 1 and quoted:
            matches = [(p,it) for p,it in matches if (p,it) in quoted]
        if len(matches) != 1:
            raise ValueError(f"Teacher allocation «{clause}» must identify exactly one {'deduction' if deduction else 'step'} in "
                             f"{[p.label or p.id for p in scope]}. Quote that condition on its scoring item.")
        p, item = matches[0]
        actual = Decimal(str(item.deduct_pts if deduction else item.total_pts))
        if actual != expected:
            raise ValueError(f"{p.id}/{item.id}: teacher allocation «{clause}» requires {expected} points, received {actual}.")
        if quoted and (p,item) not in quoted:
            raise ValueError(f"{p.id}/{item.id}: the source quote belongs to a different teacher condition: «{clause}».")
        identity = (p.id, item.id)
        if identity in anchors and _words(anchors[identity][0]) != key:
            raise ValueError(f"{p.id}/{item.id}: distinct teacher allocations require distinct scoring items: «{clause}».")
        anchors[identity] = (condition, source_clause.strip())
    return anchors


def teacher_awards_match(rules):
    from backend.services.part_rubric import ordinary_criterion

    if not rules or not rules.teacher_rubric:
        return True
    try:
        validate_teacher_point_items(rules, ordinary_criterion(rules.teacher_rubric))
    except ValueError:
        return False
    return True
