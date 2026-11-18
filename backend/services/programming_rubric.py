"""Settle an explicitly frozen aggregate test award without another model call."""
from decimal import Decimal, ROUND_HALF_UP

from backend.services.rubric_drafts import saved_draft
from backend.tools.structured_llm import StructuredOutputInvalidError


def domain_criterion(problem):
    draft = saved_draft(problem)
    if draft and draft.programming_scoring is not None:
        return draft.programming_scoring.input_rules
    if draft and verified_test_points(draft.plan) is not None:
        return draft.plan.part_rubric.teacher_rubric or ""
    return problem.criterion


def frozen_programming_steps(problem):
    draft = saved_draft(problem)
    if not draft or not draft.plan.part_rubric:
        presentation = (getattr(problem, "_math_rubric_source", {}) or {}).get("presentation") or {}
        if has_saved_scoring_contract(presentation):
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
            award.is_correct = award.score == step.total_pts
            award.deduction_basis = None
            award.desc = (f"测试通过 {run.report.passed_count}/{run.report.total_count}，本项得 {award.score:g}/{step.total_pts:g} 分。"
                          if language == "zh" else
                          f"Tests passed {run.report.passed_count}/{run.report.total_count}; awarded {award.score:g}/{step.total_pts:g} points.")
    output.steps = [awards[index] for index in sorted(awards)]
    output.score = float(sum((Decimal(str(step.score)) for step in output.steps), Decimal(0)))


def verified_test_points(plan):
    rules = plan.part_rubric if plan else None
    if not rules or any(part.deductions for part in rules.parts):
        return None
    steps = [step for part in rules.parts for step in part.steps]
    suites = [step.total_pts for step in steps if step.test_suite]
    return suites[0] if len(suites) == 1 and len(steps) > 1 else None


def compile_programming_scoring(problem, policy, *, previous=None):
    """Compile the selected suite budget and the code rubric atomically."""
    from backend.services.math_rubric_plan import _source_requirements
    from backend.services.rubric_drafts import RubricDefinition, generated_rubric

    raw = {**problem._math_rubric_source.get("presentation", {}), **problem.model_dump()}
    code_budget = Decimal(str(problem.max_score)) - Decimal(str(policy.test_points))
    if (code_budget <= 0 or not (problem.criterion or "").strip()
            or (raw.get("question_structure") or {}).get("subparts")):
        raise ValueError("A programming rubric requires a positive code allocation for the question.")

    # Point items in the text are code items. A prose-only code rubric uses
    # the entire remaining budget as one item. The suite comes from the field.
    floors, awards = _source_requirements(raw)
    if floors or any(mode != "points" or points <= 0 for _, mode, points in awards):
        raise ValueError("Use positive code point items.")
    if awards and sum((points for _, _, points in awards), Decimal(0)) != code_budget:
        raise ValueError("The code items must add up to the remaining code points.")
    steps = ([{"id": "tests", "criterion": "Test suite", "total_pts": policy.test_points, "test_suite": True}]
             if policy.test_points else [])
    steps.extend(
        {"id": f"code-{index}", "criterion": quote, "source_quote": quote, "total_pts": float(points)}
        for index, (quote, _, points) in enumerate(awards, 1)
    )
    if not awards:
        steps.append({"id": "code-1", "criterion": problem.criterion.strip(), "total_pts": float(code_budget)})

    old_rules = previous.part_rubric if previous else None
    rate = old_rules.parts[0].alternative.deduct_per_error if old_rules else None
    definition = RubricDefinition.model_validate({"part_rubric": {
        "id": problem.q_id, "label": problem.number, "stem": problem.stem, "total_pts": problem.max_score,
        "rubric_version": old_rules.rubric_version + 1 if old_rules else 1,
        "parts": [{"id": "p1", "total_pts": problem.max_score, "steps": steps,
                   "alternative": {"deduct_per_error": min(rate or 1, problem.max_score)}}]}})
    _, draft = generated_rubric(raw, definition)
    return type(draft).model_validate({**draft.model_dump(), "programming_scoring": policy})


def programming_scoring_for_review(question, draft):
    if draft and draft.programming_scoring is not None:
        return {**draft.programming_scoring.model_dump(), "code_criterion": question.criterion}
    points = verified_test_points(draft.plan if draft else None)
    if points is not None:
        rules = draft.plan.part_rubric
        # Old generated rubrics contain the suite in the editor text.
        # Prefill its verified amount and the remaining code items.
        code_criterion = "\n".join(
            step.source_quote or f"- [{step.total_pts:g} points] {step.criterion}"
            for part in rules.parts for step in part.steps if not step.test_suite
        )
        return {"test_points": points, "code_criterion": code_criterion, "input_rules": ""}
    # No trustworthy split yet. Show the old text in the code editor for
    # review, without automatically using it as the new input contract.
    return {"test_points": None, "code_criterion": question.criterion or "", "input_rules": ""}


def has_saved_scoring_contract(presentation):
    if presentation.get("programming_scoring_required"):
        return True
    draft = presentation.get("rubric_draft") or {}
    if not isinstance(draft, dict):
        return False
    if draft.get("programming_scoring") is not None:
        return True
    plan = draft.get("plan")
    rules = plan.get("part_rubric") if isinstance(plan, dict) else None
    parts = rules.get("parts") if isinstance(rules, dict) else None
    return isinstance(parts, list) and any(
        isinstance(step, dict) and step.get("test_suite") is True
        for part in parts if isinstance(part, dict) and isinstance(part.get("steps"), list)
        for step in part["steps"]
    )


def test_award_review_issue(question, draft, issues=()):
    plan = draft.plan if draft else None
    if verified_test_points(plan) is None:
        if draft and draft.programming_scoring is not None and draft.programming_scoring.test_points == 0:
            return None
        presentation = (question.source or {}).get("presentation") or {}
        if not has_saved_scoring_contract(presentation):
            return None
        field, reason = "rubric", "programming_test_award_unset"
    elif not question.test_cases:
        if any(issue.get("field") == "programming_tests" and issue.get("status") == "open"
               and issue.get("severity") == "blocking" for issue in issues):
            return None
        field, reason = "programming_tests", "required_programming_tests_missing"
    else:
        return None
    return {"issue_id": f"programming-{field}:{question.q_id}", "q_id": question.q_id,
            "field": field, "code": "generation_failed", "status": "open", "severity": "blocking",
            "source_tokens": [], "details": {"reason": reason}}
