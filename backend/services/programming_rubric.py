"""Settle an explicitly frozen aggregate test award without another model call."""
from decimal import Decimal, ROUND_HALF_UP
import re
import unicodedata

from backend.services.part_rubric import TEST_SUITE_FORMULA
from backend.services.rubric_drafts import saved_draft
from backend.tools.structured_llm import StructuredOutputInvalidError


def domain_criterion(problem):
    draft = saved_draft(problem)
    if draft and has_test_award(draft.plan):
        # The server-rendered score formula is not an input constraint. Only
        # the original teacher rubric may constrain inputs beyond the stem.
        criterion = draft.plan.part_rubric.teacher_rubric or ""
        for formula in TEST_SUITE_FORMULA.values():
            criterion = criterion.replace(formula, "")
        return criterion
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


_TEST = r"(?:整组)?(?:(?:单元|功能)?测试(?:样例|用例|项|部分|得分|分|结果)?|样例|用例)|(?:all )?(?:test suite|test cases|test results|functional tests|functional test results|tests?)\b(?!-)"
_POINTS = r"\d+(?:\.\d+)?\s*(?:分(?!钟)|points?\b|marks?\b)"
_TEST_BUDGET_LABEL = rf"(?:{_TEST})\s*(?:占(?:总分)?|计|为|满分|[:：]|(?:are |is )?worth)?\s*"
_TEST_POINTS_PREFIX = re.compile(rf"{_TEST_BUDGET_LABEL}(?:{_POINTS}|\d+(?:\.\d+)?\s*%)", re.I)
_TEST_AWARD = re.compile(
    rf"(?:{_TEST_BUDGET_LABEL}{_POINTS}|"
    rf"(?:award\s+)?{_POINTS}\s+for\s+(?:the\s+)?(?:{_TEST}))", re.I,
)
_CN_PASS_RATIO = r"按(?:测试(?:用例|样例)?|用例|样例)?通过(?:的)?(?:比例|率)"
_RATIO_AWARD = re.compile(rf"{_CN_PASS_RATIO}\s*(?:[给得计](?:分(?:为)?)?|算作)\s*{_POINTS}")
_RATIO = re.compile(
    rf"{_CN_PASS_RATIO}(?:算|计算|计分|得分|给分|结算)?"
    r"|(?:scored\s+)?(?:in proportion to|proportional to|based on)\s+(?:the\s+)?"
    r"(?:fraction passed|pass rate|proportion of (?:tests|test cases) passed)", re.I,
)
_RENDERED_RATIO = tuple(
    tuple(clause.strip() for clause in unicodedata.normalize("NFKC", formula).split(";"))
    for formula in TEST_SUITE_FORMULA.values()
)
_CODE_ONLY = re.compile(
    rf"(?:代码正确(?:性)?(?:得|占|计)\s*{_POINTS}|"
    rf"award\s+(?:all\s+)?{_POINTS}\s+for\s+correct\s+(?:source\s+)?code)", re.I,
)
_NO_TEST_AWARD = {"不设测试分", "测试不计分", "no points for tests"}


def _test_award_clauses(clauses):
    """Identify explicit, supported suite rules in source order."""
    normalized = [unicodedata.normalize("NFKC", clause) for clause in clauses]
    result = {source: None for source, clause in zip(clauses, normalized, strict=True)
              if _RATIO_AWARD.fullmatch(clause)}
    for index, (clause, following) in enumerate(zip(normalized, normalized[1:])):
        if ((_TEST_AWARD.fullmatch(clause) and _RATIO.fullmatch(following))
                or any(clause.endswith(formula) and following == tail for formula, tail in _RENDERED_RATIO)):
            result[clauses[index]] = clauses[index + 1]
    return result


def is_test_scoring_clause(clause):
    return any(pattern.fullmatch(clause) for pattern in (_TEST_AWARD, _RATIO_AWARD, _RATIO)) or bool(re.fullmatch(_TEST, clause, re.I))


def _rendered_test_title(clause):
    clause = unicodedata.normalize("NFKC", clause)
    for formula, _ in _RENDERED_RATIO:
        if clause.endswith(formula):
            return re.sub(rf"^-\s*\[{_POINTS}\]\s*", "", clause[:-len(formula)].strip(), flags=re.I)
    return None


def _mentions_test_points(clause):
    normalized = unicodedata.normalize("NFKC", clause)
    return (_TEST_POINTS_PREFIX.match(normalized) is not None
            or _RATIO_AWARD.match(normalized) is not None
            or _TEST_AWARD.fullmatch(normalized) is not None)


def declares_test_award(criterion):
    from backend.services.math_rubric_plan import _source_clauses

    clauses = _source_clauses(criterion or "")
    # A stated test budget still needs review when its condition cannot be
    # represented. A new question must not silently fall back to prose.
    return any(_mentions_test_points(clause) for clause in clauses) or bool(_test_award_clauses(clauses))


def has_test_award(plan):
    return bool(plan and plan.part_rubric and any(
        step.test_suite for part in plan.part_rubric.parts for step in part.steps
    ))


def compile_test_award_edit(problem, *, previous=None):
    """Compile only explicit, complete point budgets; reject ambiguous edits atomically."""
    from backend.services.math_rubric_plan import _score_context_clause, _source_clauses, _source_requirements
    from backend.services.rubric_drafts import RubricDefinition, generated_rubric

    raw = {**problem._math_rubric_source.get("presentation", {}), **problem.model_dump()}
    criterion = problem.criterion or ""
    clauses = _source_clauses(criterion, problem.max_score)
    suite = _test_award_clauses(clauses)
    floors, awards = _source_requirements(raw)
    total = Decimal(str(problem.max_score))
    if (not suite and len(awards) == 1 and awards[0][1:] == ("points", total)
            and _CODE_ONLY.fullmatch(unicodedata.normalize("NFKC", awards[0][0]))
            and all(clause == awards[0][0] or clause.casefold() in _NO_TEST_AWARD for clause in clauses)):
        return None

    sources = [quote for quote, _, _ in awards]
    if (len(suite) != 1 or len(set(sources)) != len(sources) or len(sources) < 2
            or not suite.keys() <= set(sources) or floors
            or any(_mentions_test_points(quote) for quote in sources if quote not in suite)
            or any(mode != "points" or points <= 0 for _, mode, points in awards)
            or sum((points for _, _, points in awards), Decimal(0)) != total
            or any(clause not in sources and clause not in suite.values()
                   and not (re.match(r"(?:(?:本题)?(?:满分|总分)|maximum|total)", clause, re.I)
                            and _score_context_clause(clause, criterion, problem.max_score))
                   for clause in clauses)
            or (raw.get("question_structure") or {}).get("subparts")):
        raise ValueError("Give one proportional test award and explicit code points adding up to the question maximum.")

    title = _rendered_test_title(next(iter(suite)))
    if title is not None and not re.fullmatch(_TEST, title, re.I):
        prior_titles = {
            _rendered_test_title(step.source_quote) or unicodedata.normalize("NFKC", step.criterion)
            for part in previous.part_rubric.parts for step in part.steps if step.test_suite
        } if has_test_award(previous) else set()
        if title not in prior_titles:
            raise ValueError("Keep the standard proportional test item separate from additional scoring conditions.")

    steps = [dict(id=f"item-{index}", criterion=quote, source_quote=quote,
                  total_pts=float(points), test_suite=quote in suite)
             for index, (quote, _, points) in enumerate(awards, 1)]
    old_rules = previous.part_rubric if has_test_award(previous) else None
    # Keep the existing default without introducing a cap on code solutions.
    rate = old_rules.parts[0].alternative.deduct_per_error if old_rules else None
    definition = RubricDefinition.model_validate({"part_rubric": {
        "id": problem.q_id, "label": problem.number, "stem": problem.stem, "total_pts": problem.max_score,
        "rubric_version": old_rules.rubric_version + 1 if old_rules else 1,
        "parts": [{"id": "p1", "total_pts": problem.max_score, "steps": steps,
                   "alternative": {"deduct_per_error": min(rate or 1, problem.max_score)}}]}})
    _, draft = generated_rubric(raw, definition)
    return draft


def test_award_review_issue(question, plan, issues=()):
    if not has_test_award(plan):
        if not declares_test_award(question.criterion):
            return None
        field, reason = "rubric", "invalid_structured_rubric"
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
