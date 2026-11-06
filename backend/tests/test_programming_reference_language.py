"""Reference language must follow the same provenance selection as its source."""
from unittest.mock import AsyncMock

import pytest

from backend.models import ProblemInfo, TestCase as Case
from backend.services import programming_runs as runs
from backend.tools.code_interpreter import ExecutionReport, TestResult as ObservedCase


POLYGLOT_REFERENCE = '#if 0\nprint(2)\n"""\n#endif\n#include <stdio.h>\nint main(void){puts("3");return 0;}\n#if 0\n"""\n#endif\n'


@pytest.mark.asyncio
@pytest.mark.parametrize("language,source", [
    ("c", '#include <stdio.h>\nint main(void) { puts("2"); return 0; }'),
    ("cpp", '#include <iostream>\nint main() { std::cout << 2; return 0; }'),
    ("python", 'print(2)'),
])
@pytest.mark.parametrize("generated_solution", [True, False])
async def test_reference_preparation_uses_selected_material_language(language, source, generated_solution):
    other_language, other_source = ("python", "print(2)") if language != "python" else (
        "c", '#include <stdio.h>\nint main(void) { puts("2"); return 0; }')
    problem = ProblemInfo(
        q_id="q1", number="1", type="编程题", stem="Read an integer from stdin and print it.",
        criterion="Correct output.", max_score=10,
        solution_code=f"```{other_language}\n{other_source}\n```",
        reference_answer=f"```{language}\n{source}\n```",
        test_cases=[Case(input="2", expected_output="2")],
    )
    if generated_solution:
        problem._math_rubric_source = {"presentation": {"ai_completion_provenance": {"solution_code": {}}}}
    selected_language, selected_source = (language, source) if generated_solution else (other_language, other_source)

    async def execute(code, cases, **options):
        assert code.strip() == selected_source
        assert options["language"] == selected_language
        rows = [ObservedCase(test=case, passed=True, actual_output="2", error="", duration_ms=0) for case in cases]
        return ExecutionReport(len(rows), len(rows), 1.0, rows, "fixture execution")

    runner = AsyncMock(side_effect=execute)
    _, cases, reason = await runs._prepare(runs.ProgrammingRun(), problem, None, None, runner, None)
    assert not reason and len(cases) == 1
    assert runner.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("generated_solution", [False, True])
async def test_old_reference_validation_is_reused_only_for_unchanged_selection(monkeypatch, generated_solution):
    problem = ProblemInfo(q_id="q1", number="1", type="编程题", stem="Read stdin.", criterion="Correct output.",
        solution_code="```python\nprint(2)\n```", reference_answer=f"```c\n{POLYGLOT_REFERENCE}\n```",
        test_cases=[Case(input="2", expected_output="2")])
    if generated_solution:
        problem._math_rubric_source = {"presentation": {"ai_completion_provenance": {"solution_code": {}}}}
    state = runs.ProgrammingRun()
    identity = runs._reference_execution_identity
    with monkeypatch.context() as old:
        old.setattr(runs, "_reference_execution_identity", lambda p: {
            k: v for k, v in identity(p).items() if k != "reference_language_selection_version"})
        old_key = runs._question_key(state, problem)
    data = [case.model_dump(mode="json") for case in problem.test_cases]
    state.store(runs.PREPARED, old_key, {"q_id": "q1", "cases": data,
        "suite_hash": runs.digest(data), "reference_checked": True})

    async def execute(code, cases, **options):
        assert options["language"] == "c"
        rows = [ObservedCase(test=case, passed=False, actual_output="3", error="", duration_ms=0) for case in cases]
        return ExecutionReport(0, len(rows), 0.0, rows, "fixture execution")

    runner = AsyncMock(side_effect=execute)
    key, cases, reason = await runs._prepare(state, problem, None, None, runner, None)
    if generated_solution:
        assert key != old_key and not cases and reason == "programming_reference_conflict"
        runner.assert_awaited_once()
    else:
        assert key == old_key and cases and not reason
        runner.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_retry", [False, True], ids=["recover-interrupted", "explicit-retry"])
async def test_language_fix_preserves_interrupted_generation_boundary(monkeypatch, explicit_retry):
    problem = ProblemInfo(q_id="q1", number="1", type="编程题", stem="Read stdin.", criterion="Correct output.",
        solution_code="```python\nprint(2)\n```", reference_answer="```c\nint main(void) { return 0; }\n```")
    problem._math_rubric_source = {"presentation": {"ai_completion_provenance": {"solution_code": {}}}}
    state = runs.ProgrammingRun()
    identity = runs._reference_execution_identity
    with monkeypatch.context() as old:
        old.setattr(runs, "_reference_execution_identity", lambda p: {
            k: v for k, v in identity(p).items() if k != "reference_language_selection_version"})
        old_key = runs._question_key(state, problem)
    # Only recovery of the same run restores STARTED; explicit retries do not inherit it.
    if not explicit_retry:
        state.store(runs.STARTED, old_key, {"q_id": "q1"})
    generate = AsyncMock(return_value=([], "programming_no_cases"))
    runner = AsyncMock()
    _, cases, reason = await runs._prepare(state, problem, None, generate, runner, None)
    assert not cases
    assert reason == ("programming_no_cases" if explicit_retry else "programming_preparation_interrupted")
    assert generate.await_count == int(explicit_retry)
    runner.assert_not_awaited()


@pytest.mark.parametrize("solution,generated_fields,affected", [
    ("print(2)", [], False),
    (None, [], False),
    ("print(2)", ["solution_code", "reference_answer"], False),
    ("", ["solution_code"], False),
    ("   ", ["solution_code"], True),
])
def test_language_cache_invalidation_is_scoped_to_excluded_nonempty_solution(solution, generated_fields, affected):
    problem = ProblemInfo(q_id="q1", number="1", type="编程题", stem="Read stdin.", criterion="Correct output.",
        solution_code=solution, reference_answer="```c\nint main(void) { return 0; }\n```")
    problem._math_rubric_source = {"presentation": {"ai_completion_provenance": dict.fromkeys(generated_fields, {})}}
    assert ("reference_language_selection_version" in runs._reference_execution_identity(problem)) == affected
