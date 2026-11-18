"""Settle an explicitly frozen aggregate test award without another model call."""
from decimal import Decimal, ROUND_HALF_UP

from backend.models import ScoreAward
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


def distinct_scoring_cases(problem, originals, cases):
    if not frozen_programming_steps(problem):
        return originals, cases
    from backend.services.programming_test_contract import ProgrammingCaseError, case_input_key
    from backend.services.programming_runs import _reference, _source
    from backend.services.python_source_encoding import python_execution_source
    reference = _reference(problem) or (_source(problem.solution_code) if problem.solution_code else None)
    if reference and any(case.io_mode == "function" for case in cases):
        reference = python_execution_source(reference)
    def expectation(case):
        return (case.expected_return if case.io_mode == "function" else case.expected_output,
                case.comparison, case.abs_tol, case.rel_tol)
    positions, kept_originals, kept = {}, [], []
    for original, case in zip(originals, cases, strict=True):
        key = case_input_key(problem.stem, case, reference=reference)
        if key in positions:
            index = positions[key]
            previous = kept[index]
            if expectation(case) != expectation(previous):
                raise ProgrammingCaseError("The same input has conflicting expected results.", issue="conflicting_expectations")
            if case.source == "teacher" and previous.source != "teacher":
                kept_originals[index], kept[index] = original, case
        else:
            positions[key] = len(kept)
            kept_originals.append(original)
            kept.append(case)
    return kept_originals, kept


def suite_award(maximum, report):
    return float(_suite_points(maximum, report.passed_count, report.total_count))


def _suite_points(maximum, passed, total):
    budget = Decimal(str(maximum))
    return min(budget, (budget * passed / total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _suite_case_awards(maximum, run, language):
    from backend.skills.programming_evidence import _failure_reason, _STUDENT_FAILURES

    report = run.report
    positions = (getattr(run, "domain_review", None) or {}).get("positions")
    subtotal, passed = Decimal(0), 0
    awards, failed = [], []
    for index, result in enumerate(report.results):
        number = positions[index] if positions else index + 1
        label = f"样例 {number}" if language == "zh" else f"Case {number}"
        passed += int(result.passed)
        points = _suite_points(maximum, passed, report.total_count)
        if not result.passed:
            reason = _STUDENT_FAILURES[_failure_reason(result)][0 if language == "zh" else 1]
            label = f"{label}（{reason}）" if language == "zh" else f"{label} ({reason})"
            failed.append(label)
        # Allocate display rounding cumulatively so the case awards match the suite subtotal.
        awards.append(ScoreAward(item_id=f"case-{number}", desc=label,
                                 points=float(points - subtotal), is_correct=result.passed))
        subtotal = points
    return awards, failed


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
        award.awards = award.deductions = None
        if step.test_suite:
            award.score = suite_award(step.total_pts, run.report)
            award.is_correct = award.score == step.total_pts
            award.deduction_basis = None
            award.desc = (f"测试通过 {run.report.passed_count}/{run.report.total_count}，本项得 {award.score:g}/{step.total_pts:g} 分。"
                          if language == "zh" else
                          f"Tests passed {run.report.passed_count}/{run.report.total_count}; awarded {award.score:g}/{step.total_pts:g} points.")
            award.awards, failed = _suite_case_awards(step.total_pts, run, language)
            if failed:
                award.desc += (" 失败样例：" + "、".join(failed) if language == "zh"
                               else " Failed cases: " + ", ".join(failed) + ".")
    output.steps = [awards[index] for index in sorted(awards)]
    output.score = float(sum((Decimal(str(step.score)) for step in output.steps), Decimal(0)))
