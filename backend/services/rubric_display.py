"""Teacher-facing projections of frozen rules; never used as scoring input.

The model interprets response requirements during rubric generation. It does not
write the numbers in this projection. Source binding and ledger checks gate any
compression; an absent/unsupported interpretation retains the individual rules.
"""
import hashlib
import json
from fractions import Fraction
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from backend.services.math_source_evidence import match_source_quote


class PartDisplay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_id: str = Field(min_length=1, max_length=64)
    response: Literal["answer_only", "reasoning", "mixed", "unspecified"] = "unspecified"
    alternative_relevant: bool = True
    evidence: list[str] = Field(default_factory=list, max_length=8)


class DisplayProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parts: list[PartDisplay] = Field(max_length=100)


DISPLAY_INSTRUCTION = """
Include display_plan beside part_rubric, solely for teacher-facing presentation.
It does not change scoring. Return {"parts":[{"part_id":"the exact frozen part ID",
"response":"answer_only|reasoning|mixed|unspecified", "alternative_relevant":true,
"evidence":["exact short quote from the shared stem, this part or teacher rules"]}]}.
Read each part's requested response, not its overall question-type label. A detailed reference
answer does not require students to show work. A judgment with requested justification is not
answer_only. Independent answer-only items do not need alternative-method guidance. Mixed
questions can differ by part. Use unspecified and alternative_relevant=true when uncertain.
Do not infer all-or-nothing scoring, invent amounts or modify any scoring JSON to simplify its
display. Never hide explicit teacher conditions or method restrictions. This display
annotation must not prevent returning the complete requested reference answer and scoring rules.
"""


def _fingerprint(rules):
    # Question IDs/display numbers can be rebound during incremental assembly.
    body = rules.model_dump(mode="json", exclude={"id", "label", "rubric_version"})
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def alternative_cap_issue(q_id, rules):
    """Expose an unsupported AI cap for review without altering scoring policy."""
    from backend.services.part_rubric import _teacher_alternative_fields, _requires_method

    caps = []
    for part in rules.parts:
        policy = part.alternative
        maximum = float(policy.maximum.resolve(part.total_pts))
        minimum = float(policy.minimum.resolve(part.total_pts))
        if minimum <= maximum < part.total_pts and not policy.inferred_method_cap:
            # The existing required-method policy can give a fixed share of the
            # question floor. Its source requirement justifies that exact cap.
            method_floor = (rules.nonblank_floor.resolve(rules.total_pts)
                            * Fraction(str(part.total_pts)) / Fraction(str(rules.total_pts)))
            if minimum == maximum and policy.maximum.resolve(part.total_pts) == method_floor and _requires_method(policy.source_quote):
                continue
            # A teacher's deduction/floor quote does not justify an AI ceiling.
            # Reuse field-level binding so parsed caps and source-bound semantic
            # caps retain the same meaning as in the frozen scoring policy.
            try:
                fields = _teacher_alternative_fields(policy.source_quote, policy)
            except ValueError:
                fields = {}
            if "maximum" not in fields:
                caps.append({"label": part.label or "", "maximum": maximum, "total": part.total_pts})
    if not caps:
        return []
    return [{"issue_id": f"alternative-cap:{q_id}:{_fingerprint(rules)[:24]}", "q_id": q_id,
             "field": "rubric", "code": "math_rubric_advisory", "severity": "warning", "status": "open",
             "details": {"reason": "alternative_cap_review", "alternative_caps": caps}}]


def accept_display_plan(proposal, rules):
    """Invalid display metadata is discarded; a valid rubric still succeeds."""
    if proposal is None:
        return None
    try:
        parsed = DisplayProposal.model_validate(proposal)
        known = {part.id: part for part in rules.parts}
        if len(parsed.parts) != len(known) or {p.part_id for p in parsed.parts} != set(known):
            return None
        for view in parsed.parts:
            part = known[view.part_id]
            sources = (rules.stem or "", part.stem or "", rules.teacher_rubric or "")
            if (view.response != "unspecified" or not view.alternative_relevant) and (
                not view.evidence or any(len(quote) > 4000 or not quote.strip()
                    or not any(match_source_quote(source, quote) for source in sources) for quote in view.evidence)
            ):
                return None
        return {"fingerprint": _fingerprint(rules), **parsed.model_dump()}
    except (ValidationError, TypeError, ValueError):
        return None


def _view(rules, part, display_plan):
    if not isinstance(display_plan, dict) or display_plan.get("fingerprint") != _fingerprint(rules):
        return None
    try:
        return next((item for item in DisplayProposal.model_validate({"parts": display_plan.get("parts")}).parts
                     if item.part_id == part.id), None)
    except (ValidationError, TypeError, ValueError):
        return None


def answer_only_part(rules, part, display_plan=None, expected_truth=None):
    interpretation = _view(rules, part, display_plan)
    if interpretation:
        return interpretation.response == "answer_only"
    # Existing validated Boolean answer keys already establish this narrow
    # response form. They are not a keyword-based classifier for other tasks.
    return bool(expected_truth and set(expected_truth) == {p.id for p in rules.parts}
                and type(expected_truth.get(part.id)) is bool)


def show_alternative(rules, part, display_plan=None, expected_truth=None):
    policy = part.alternative
    # A display annotation cannot hide an explicit teacher policy or a special
    # numerical restriction, even if the model classified the response wrongly.
    special = (policy.source_quote or policy.field_sources or policy.inferred_method_cap
        or policy.maximum.resolve(part.total_pts) != part.total_pts
        or policy.minimum.resolve(part.total_pts) != 0
        or policy.max_deduction is not None)
    interpretation = _view(rules, part, display_plan)
    return bool(special) or (interpretation.alternative_relevant if interpretation
                            else not answer_only_part(rules, part, display_plan, expected_truth))


def simple_decisions(rules, stem=None, *, display_plan=None, expected_truth=None):
    # Equal budgets alone are insufficient: do not merge different requirements,
    # multi-output items, partial-credit conditions or unverified interpretations.
    return (bool(rules.parts) and not rules.teacher_rubric
        and rules.nonblank_floor.resolve(rules.total_pts) == 0
        and len({part.steps[0].criterion for part in rules.parts if part.steps}) == 1
        and all(answer_only_part(rules, part, display_plan, expected_truth) and len(part.steps) == 1 and not part.deductions
                and not part.steps[0].required_conditions and not show_alternative(rules, part, display_plan, expected_truth)
                and part.steps[0].total_pts == part.total_pts for part in rules.parts))


def rubric_display(rules, stem, language="zh", *, display_plan=None, expected_truth=None, semantic_policy_checks=()):
    zh = language == "zh"
    if simple_decisions(rules, stem, display_plan=display_plan, expected_truth=expected_truth):
        values = {part.total_pts for part in rules.parts}
        if len(values) == 1:
            points = next(iter(values))
            if len(rules.parts) == 1:
                return (f"回答正确得 {points:g} 分，错误或未答得 0 分。" if zh
                    else f"A correct answer earns {points:g} points; an incorrect or blank answer earns 0.")
            return (f"共 {len(rules.parts)} 小问，每问 {points:g} 分。回答正确得 {points:g} 分，错误或未答得 0 分。"
                if zh else f"{len(rules.parts)} parts, {points:g} points each. A correct answer earns {points:g} points; an incorrect or blank answer earns 0.")
        return "\n\n".join(f"- {part.label or index}：回答正确得 {part.total_pts:g} 分，错误或未答得 0 分。" if zh
            else f"- {part.label or index}: {part.total_pts:g} points for a correct answer; 0 for incorrect or blank."
            for index, part in enumerate(rules.parts, 1))
    teacher_targets = set()
    teacher_fallback = ""
    if rules.teacher_rubric:
        from backend.services.part_rubric import verify_teacher_numbers
        try:
            verify_teacher_numbers(rules.teacher_rubric, rules, rules.total_pts,
                verified_policy_ids=[check["policy_id"] for check in semantic_policy_checks if check.get("verdict") == "consistent"],
                verified_policy_checks=semantic_policy_checks, follow_through_targets=teacher_targets)
        except ValueError:
            # Older drafts may lack semantic binding evidence. Keep the teacher's
            # original instruction visible instead of silently omitting it.
            teacher_fallback = rules.teacher_rubric
    sections = []
    for part in rules.parts:
        unit = "分" if zh else "points"
        label = part.label or ""
        single = len(part.steps) == 1 and not part.deductions and part.steps[0].total_pts == part.total_pts
        lines = []
        if not single and (label or len(rules.parts) > 1):
            lines.append(f"**{label}（{part.total_pts:g} {unit}）**")
        for step in part.steps:
            text = step.criterion.rstrip("。.")
            conditions = ("；" if zh else "; ").join(step.required_conditions)
            if conditions:
                text += f"（{conditions}）" if zh else f" ({conditions})"
            if (part.id, step.id) in teacher_targets:
                amount = step.follow_through_award()
                text += (f"；若仅沿用前一步的错误结果，本步方法正确时得 {amount:g} 分" if zh
                    else f"; if only the earlier result is wrong and this step's method is correct, award {amount:g} points")
            prefix = f"{label} " if single and label else ""
            lines.append(f"- {prefix}{text}（{step.total_pts:g} {unit}）。" if zh
                else f"- {prefix}{text} ({step.total_pts:g} {unit}).")
        if part.deductions:
            if not label and len(rules.parts) == 1:
                lines.append(f"从满分 {part.total_pts:g} 分起，按以下规则扣分：" if zh
                    else f"Start from {part.total_pts:g} points and apply these deductions:")
            for rule in part.deductions:
                text = f"{rule.criterion.rstrip('。.') }：扣 {rule.deduct_pts:g} 分" if zh else f"{rule.criterion.rstrip('.')}: deduct {rule.deduct_pts:g} points"
                counting = {"independent_error": ("每个独立错误", "per independent error"),
                            "wrong_step": ("每个错误步骤", "per incorrect step"),
                            "target_item": ("每个未达成的作答目标", "per unmet answer target")}
                text += f"（{counting[rule.count_by][0]}）" if zh else f" ({counting[rule.count_by][1]})"
                if rule.limit is not None:
                    text += f"，最多扣 {rule.limit} 次" if zh else f", at most {rule.limit} times"
                if rule.max_deduction is not None:
                    text += f"，累计最多扣 {rule.max_deduction:g} 分" if zh else f", up to {rule.max_deduction:g} points total"
                if rule.exclusive_group:
                    text += "；同一目标只取一档" if zh else "; apply one tier per goal"
                lines.append("- " + text + ("。" if zh else "."))
        sections.append("\n\n".join(lines))
    floor = float(rules.nonblank_floor.resolve(rules.total_pts))
    if floor > 0:
        sections.append(f"整题有作答时，最低给 {floor:g} 分。" if zh else f"A nonblank response earns at least {floor:g} points.")
    if teacher_fallback:
        sections.append(("教师补充要求：\n\n" if zh else "Teacher requirements:\n\n") + teacher_fallback)
    return "\n\n".join(sections)


def alternative_display(part, language="zh"):
    zh = language == "zh"
    policy = part.alternative
    maximum, minimum = float(policy.maximum.resolve(part.total_pts)), float(policy.minimum.resolve(part.total_pts))
    if maximum == minimum:
        return f"其他解法按 {maximum:g} 分计。" if zh else f"Alternative methods receive {maximum:g} points."
    text = ((f"其他正确解法同样可得满分 {maximum:g} 分" if maximum == part.total_pts else f"其他解法最高得 {maximum:g} 分") if zh
        else (f"Other correct methods can earn all {maximum:g} points" if maximum == part.total_pts else f"Alternative methods earn up to {maximum:g} points"))
    if policy.deduct_per_error:
        text += f"，每个独立错误扣 {policy.deduct_per_error:g} 分" if zh else f"; deduct {policy.deduct_per_error:g} points per independent error"
    if minimum > 0:
        text += f"，最低保留 {minimum:g} 分" if zh else f"; retain at least {minimum:g} points"
    if policy.max_deduction is not None and policy.max_deduction < maximum - minimum:
        text += f"，累计最多扣 {policy.max_deduction:g} 分" if zh else f"; deduct at most {policy.max_deduction:g} points in total"
    if policy.needs_rate(part.total_pts):
        text += "；每个独立错误的扣分值待补齐" if zh else "; the per-error deduction is missing"
    return text + ("。" if zh else ".")
