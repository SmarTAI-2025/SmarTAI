"""Bind explicit teacher allocations to their scored conditions, never just to a sum."""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

from backend.services.math_source_evidence import match_source_quote
from backend.services.question_structure import _allocation_marker_pattern


def _words(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def _expand_subtotals(text, labels):
    """Check an explicit '其中' subtotal, then bind its children as ordinary leaves."""
    from backend.services.math_rubric_plan import _AMOUNT, _amount_value, _source_clauses

    if "其中" not in text:
        return text

    def expand(match):
        subtotal = _AMOUNT.fullmatch(match["total"])
        children = _source_clauses(match["children"])
        amounts = [list(_AMOUNT.finditer(child)) for child in children]
        if (not children or any(len(values) != 1 for values in amounts)
                or any(values[0][2] != subtotal[2] for values in amounts)):
            raise ValueError(f"Identify the substep allocations in «{match[0]}».")
        if sum(_amount_value(*values[0].groups(), 100) for values in amounts) != _amount_value(*subtotal.groups(), 100):
            raise ValueError(f"The substeps in «{match[0]}» must add up to their stated subtotal.")
        name = match["parent"].strip()
        heading = next((m[0] for _, marker in labels if (m := marker.match(name))), "")
        return heading + match["children"]

    return re.sub(r"(?P<parent>[^()（）,，、；;。\n]{1,160}?)(?P<total>" + _AMOUNT.pattern +
                  r")\s*[(（]\s*其中\s*[:：]?\s*(?P<children>[^()（）]+)[)）]", expand, text)


def _allocation_clauses(text, labels):
    """Keep the source span for each assignment, including shared-unit lists."""
    from backend.services.math_rubric_plan import _AMOUNT, _NUMBER, _scoring_numeric_text, _source_clauses

    # An amount closes a postfix award ("setup 3 points ... result 7 points").
    # For prefix awards ("3 points for setup and 7 points for result") the
    # boundary instead precedes the next amount. Never split inside a number.
    join = r"(?:以及|并且|然后|而且|而|并|及|与|和|且|另|\band\b|\bwhile\b|\bthen\b)"
    for sentence in _source_clauses(_expand_subtotals(text, labels)):
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
    for source_clause, clause in _allocation_clauses(ordinary, labels):
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
        # Remove the allocation operator only when it follows a named subject.
        # Keep the original clause for evidence, monetary roles and scope.
        prefix = clause[:amount.start()]
        operator = re.search(r"(?:占(?:本[问题]|整题)?(?:总分|满分)|最多|至多|最高|至少)(?:可?[给得]|为)?\s*$", prefix)
        subject = prefix[:operator.start()].strip() if operator else ""
        named_bound = bool(subject and not re.search(r"满分|总分|保底|累计|扣|后续|每错|非空|作答|本题|整题", subject))
        conditional_minimum = named_bound and "至少" in operator[0]
        budget_context = subject + clause[amount.start():] if named_bound else clause
        # These amounts play other roles and have their own typed checks.
        if re.search(r"满分|总分|起[评分]|最多|至多|上限|最低|保底|至少|折算|沿用|后传|保留|"
                     r"粒度|(?:配分|评分|计分|给分|分值)单位|每(?:个)?小问|每(?:道)?题|[各每]步|各小问|每空|满打|空白|未作答|"
                     r"maximum|minimum|floor|at most|at least|follow.through|each part|per part|"
                     r"allocation unit|partial credit|otherwise|部分分", budget_context, re.I):
            continue
        if re.search(r"\d\s*/\s*$", clause[:amount.start()]) or clause[amount.end():].startswith(("之","钟")):
            continue
        if _score_context_clause(clause, ordinary, scope[0].total_pts if len(scope) == 1 else rules.total_pts):
            continue
        deduction = bool(_DEDUCTION.search(clause))
        prefix = (subject if named_bound else prefix).rstrip(" (（:：=，,")
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
        budget = scope[0].total_pts if len(scope) == 1 else rules.total_pts
        if named_bound and re.search(r"整题|总分", operator[0]) and "本问" not in operator[0]:
            budget = rules.total_pts
        expected = _amount_value(*amount.groups(), budget)
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
        if actual < expected or (not conditional_minimum and actual != expected):
            raise ValueError(f"{p.id}/{item.id}: teacher allocation «{clause}» requires {expected} points, received {actual}.")
        if quoted and (p,item) not in quoted:
            raise ValueError(f"{p.id}/{item.id}: the source quote belongs to a different teacher condition: «{clause}».")
        identity = (p.id, item.id)
        if identity in anchors and _words(anchors[identity][0]) != key:
            raise ValueError(f"{p.id}/{item.id}: distinct teacher allocations require distinct scoring items: «{clause}».")
        anchors[identity] = (condition, source_clause.strip())
    return anchors


def bind_step_conditions(parts, ordinary, anchors):
    """Resolve explicit no-credit clauses to funded steps, retaining ambiguous sources for review."""
    from backend.services.math_rubric_plan import _source_clauses

    requirements = {}
    unresolved = []
    labels = [(p, re.compile(_allocation_marker_pattern(p.label), re.I)) for p in parts if p.label]
    scope = parts
    previous = ""
    for clause in _source_clauses(ordinary):
        preceding, previous = previous, clause
        heading = next(((p, m) for p, pattern in labels if (m := pattern.match(clause))), None)
        if heading:
            scope = (heading[0],)
            preceding = ""
            clause = clause[heading[1].end():].strip(" :：")
        if not re.search(r"不得分|不给分|不计分|no credit|zero points", clause, re.I):
            continue
        condition_word = r"未|缺|漏|没有|若|如|否则|without|unless|if\b"
        if not re.search(condition_word, clause, re.I):
            if not re.search(condition_word, preceding, re.I):
                continue
            joined = re.search(re.escape(preceding) + r"\s*[，,]\s*" + re.escape(clause), ordinary)
            if joined is None:
                continue
            clause = joined[0]
        items = [(p, s) for p in scope for s in p.steps]
        if not items:
            unresolved.append(clause)
            continue
        # Match the named award, not the condition whose absence triggers the loss.
        target = re.split(r"时|则|[，,]", clause)[-1]
        named = []
        for part, step in items:
            name = anchors.get((part.id, step.id), (step.criterion, ""))[0]
            if re.search(re.escape(_words(name)) + r"(?:项)?(?:不得分|不给分|不计分)", _words(target)):
                named.append((part, step))
        if not named:
            named = [(part, step) for part, step in items if clause in step.required_conditions]
        if len(named) == 1:
            part, step = named[0]
            requirements.setdefault((part.id, step.id), []).append(clause)
        else:
            unresolved.append(clause)
    # Explicit source-bound conditions expressed in other words can also be
    # judged separately. Never accept an invented condition as a scoring gate.
    for part in parts:
        for step in part.steps:
            key = (part.id, step.id)
            for condition in step.required_conditions:
                if not (match_source_quote(ordinary, condition) and re.search(
                    r"必须|须|要求|只有|仅当|未|缺|漏|不得分|should|must|required|only if|without|unless",
                    condition, re.I
                )):
                    raise ValueError(f"{part.id}/{step.id}: quote the teacher's required condition.")
                if condition in unresolved:
                    continue
                if any(condition in values for identity, values in requirements.items() if identity != key):
                    raise ValueError(f"{part.id}/{step.id}: condition belongs to a different scoring step.")
                values = requirements.setdefault(key, [])
                if condition not in values:
                    values.append(condition)
    gaps = [f"请核对该条件适用的评分项 / Check which scoring item this condition applies to: {clause}"
            for clause in unresolved]
    return {key: tuple(values) for key, values in requirements.items()}, gaps


def teacher_awards_match(rules):
    from backend.services.part_rubric import (
        _NOT_FULL, _REQUIRED, _requires_method, _teacher_alternative_fields, ordinary_criterion, verify_teacher_numbers,
    )

    if not rules:
        return True
    for part in rules.parts:
        policy = part.alternative
        if policy.max_deduction is not None and policy.source_quote:
            try:
                fields = _teacher_alternative_fields(policy.source_quote, policy)
            except ValueError:
                fields = {}
            if "deduct_per_error" in fields and "max_deduction" not in fields:
                return False
        if _NOT_FULL.search(policy.source_quote) and policy.maximum.resolve(part.total_pts) == part.total_pts:
            return False
        if (policy.source_quote in {rules.stem, part.stem} and _REQUIRED.search(policy.source_quote)
                and not _requires_method(policy.source_quote)):
            return False
    try:
        ordinary = ordinary_criterion(rules.teacher_rubric or "")
        anchors = validate_teacher_point_items(rules, ordinary)
        verify_teacher_numbers(rules.teacher_rubric or "", rules, rules.total_pts)
        conditions, gaps = bind_step_conditions(rules.parts, ordinary, anchors)
        if any(step.required_conditions != conditions.get((part.id, step.id), ())
               for part in rules.parts for step in part.steps) or set(gaps) - set(rules.teacher_gaps):
            return False
    except ValueError:
        return False
    return True
