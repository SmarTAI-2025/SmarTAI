"""Settle an explicitly frozen aggregate test award without another model call."""
from decimal import Decimal, ROUND_HALF_UP

from backend.services.rubric_drafts import saved_draft
from backend.tools.structured_llm import StructuredOutputInvalidError


def domain_criterion(problem):
    draft = saved_draft(problem)
    if draft and draft.plan.part_rubric and any(
        step.test_suite for part in draft.plan.part_rubric.parts for step in part.steps
    ):
        # The server-rendered score formula is not an input constraint. Only
        # the original teacher rubric may constrain inputs beyond the stem.
        return draft.plan.part_rubric.teacher_rubric or ""
    return problem.criterion


def frozen_programming_steps(problem):
    draft = saved_draft(problem)
    if not draft or not draft.plan.part_rubric:
        presentation = (getattr(problem, "_math_rubric_source", {}) or {}).get("presentation") or {}
        rules = ((presentation.get("rubric_draft") or {}).get("plan") or {}).get("part_rubric") or {}
        if any(step.get("test_suite") is True for part in rules.get("parts", []) for step in part.get("steps", [])):
            raise ValueError("programming_case_configuration_invalid")
        return []
    rows = [(part, step) for part in draft.plan.part_rubric.parts for step in part.steps]
    if not any(step.test_suite for _, step in rows):
        return []  # Existing teacher/legacy rules retain their original semantics.
    if (sum(step.test_suite for _, step in rows) != 1
            or any(part.deductions for part in draft.plan.part_rubric.parts)):
        raise ValueError("The programming test allocation is incomplete.")
    return rows


def grading_steps(rows, run):
    if rows and (run.status != "executed" or not run.report or not run.report.total_count):
        raise ValueError("programming_execution_unavailable")
    return [{"step_no": index, "part_id": part.id, "item_id": step.id,
             "criterion": step.criterion, "max_points": step.total_pts,
             **({"test_suite": True, "passed": run.report.passed_count,
                 "total": run.report.total_count, "score": suite_award(step.total_pts, run.report)} if step.test_suite else {})}
            for index, (part, step) in enumerate(rows, 1)]


def suite_award(maximum, report):
    budget = Decimal(str(maximum))
    return float(min(budget, (budget * report.passed_count / report.total_count).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)))


def settle_test_award(output, rows, run, language):
    if not rows:
        return
    awards = {step.step_no: step for step in output.steps}
    if len(output.steps) != len(rows) or set(awards) != set(range(1, len(rows) + 1)):
        raise StructuredOutputInvalidError("Return each frozen programming scoring item exactly once.")
    for index, (part, step) in enumerate(rows, 1):
        award = awards[index]
        if award.max_points != step.total_pts:
            raise StructuredOutputInvalidError("Preserve each frozen programming item budget.")
        award.part_id, award.item_id = part.id, step.id
        if step.test_suite:
            award.score = suite_award(step.total_pts, run.report)
            award.is_correct = run.report.passed_count == run.report.total_count
            award.deduction_basis = None
            award.desc = (f"测试通过 {run.report.passed_count}/{run.report.total_count}，本项得 {award.score:g}/{step.total_pts:g} 分。"
                          if language == "zh" else
                          f"Tests passed {run.report.passed_count}/{run.report.total_count}; awarded {award.score:g}/{step.total_pts:g} points.")
    output.steps = [awards[index] for index in sorted(awards)]
    output.score = float(sum((Decimal(str(step.score)) for step in output.steps), Decimal(0)))
