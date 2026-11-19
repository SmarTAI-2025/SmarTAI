import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents.ingest_agent import _extract_problems_call, generate_missing_question_materials
from backend.agents.question_preparation_agent import requested_major_question_materials
from backend.llm.providers import LLMResponse
from backend.models import ProblemInfo, StudentAnswerInfo, TestCase
from backend.services.programming_materials import resolve_generated_outputs
from backend.services.programming_rubric import frozen_programming_steps, grading_steps, settle_test_award, suite_award
from backend.services.rubric_drafts import RubricDefinition, generated_rubric
from backend.skills.programming import ProgrammingGradingOutput, ProgrammingSkill, _validate_awards
from backend.tools.code_interpreter import ExecutionReport, TestResult as Observation


def source(**changes):
    return dict(q_id="q1", number="1", type="编程题", stem="Implement def square(x): x is an integer. Return x*x.",
                criterion="", max_score=10, solution_code="def square(x): return x*x", **changes)


def inputs():
    return [TestCase(io_mode="function", function_args=[i], expected_return="999") for i in range(10)]


def definition():
    return {"part_rubric": {"id": "q1", "label": "1", "stem": source()["stem"], "total_pts": 10,
        "parts": [{"id": "p1", "total_pts": 10, "steps": [
            {"id": "tests", "criterion": "Functional test results", "total_pts": 6, "test_suite": True},
            {"id": "code", "criterion": "Correct algorithm in the submitted source", "total_pts": 4}],
            "alternative": {"deduct_per_error": 1}}]}}


@pytest.mark.asyncio
@pytest.mark.parametrize("ai", [False, True])
async def test_function_reference_supplies_outputs_without_stdin_wrapper(ai):
    row = source(ai_completion_provenance={"solution_code": {"source_kind": "ai_generated"}} if ai else {})
    cases = await resolve_generated_outputs(row, inputs())
    assert len(cases) == 10
    assert [case.expected_return for case in cases] == [str(i*i) for i in range(10)]
    assert all(case.source == "llm_generated" for case in cases)
    assert all(case.expected_return == "999" for case in inputs())


@pytest.mark.asyncio
async def test_teacher_reference_precedes_an_ai_solution():
    row = source(reference_answer="```python\ndef square(x): return x*x\n```",
                 ai_completion_provenance={"solution_code": {"source_kind": "ai_generated"}})
    row["solution_code"] = "def square(x): return 999"
    cases = await resolve_generated_outputs(row, inputs())
    assert cases[3].expected_return == "9"


@pytest.mark.asyncio
async def test_stdin_reference_preserves_inputs_and_captures_stdout():
    row = source()
    row.update(stem="Read an integer from stdin and print its square.", solution_code="n=int(input()); print(n*n)")
    cases = await resolve_generated_outputs(row, [TestCase(input=f"{i}\n", expected_output="wrong") for i in range(10)])
    assert [case.input for case in cases] == [f"{i}\n" for i in range(10)]
    assert [case.expected_output for case in cases] == [f"{i*i}\n" for i in range(10)]


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["c", "cpp"])
async def test_native_reference_dispatch_uses_the_declared_language(language):
    from backend.tools.code_interpreter import ExecutionReport, TestResult as Observation
    row = source()
    row.update(stem="Read an integer from stdin and print its square.",
               solution_code=f"```{language}\nint main(void) {{ return 0; }}\n```",
               ai_completion_provenance={"solution_code": {"source_kind": "ai_generated"}})
    async def execute(code, cases, **options):
        assert options["language"] == language and code.startswith("int main")
        return ExecutionReport(0, len(cases), 0, [Observation(
            test=c, passed=False, actual_output=str(i*i), stdout=str(i*i), error="", duration_ms=1,
            exit_reason="wrong_answer") for i, c in enumerate(cases)])
    cases = await resolve_generated_outputs(row, [TestCase(input=str(i)) for i in range(10)], execute=execute)
    assert cases[3].expected_output == "9"


@pytest.mark.asyncio
async def test_teacher_examples_are_never_rewritten_to_fit_generated_code():
    teacher = TestCase(function_name="square", function_args=[2], expected_return="5")
    row = source(test_cases=[teacher], ai_completion_provenance={"solution_code": {"source_kind": "ai_generated"}})
    with pytest.raises(ValueError, match="contradicts supplied teacher"):
        await resolve_generated_outputs(row, inputs())
    assert teacher.expected_return == "5"


@pytest.mark.asyncio
@pytest.mark.parametrize("cases", [inputs()[:9], inputs()[:9] + inputs()[:1]])
async def test_short_or_repeated_inputs_are_rejected_before_execution(cases):
    execute = AsyncMock()
    with pytest.raises(ValueError, match="distinct"):
        await resolve_generated_outputs(source(), cases, execute=execute)
    execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("criterion", ["10 points", "（满分10分）", "10 points for correct code"])
async def test_programming_total_in_question_source_is_not_a_grading_criterion(criterion):
    row = {**source(), "criterion": criterion}
    provider = SimpleNamespace(provider_id="synthetic", ainvoke=AsyncMock(return_value=LLMResponse(
        content=json.dumps({"problems": [row]}), provider="synthetic", model="synthetic", duration_ms=1)))
    extracted = await _extract_problems_call(f"1. {row['stem']}\n{criterion}", provider)
    extracted["q1"]["question_structure"] = None
    from backend.services.question_structure import annotate_major_question_structures
    targets = requested_major_question_materials(annotate_major_question_structures(extracted))
    assert extracted["q1"]["max_score"] == 10
    assert ("q1:criterion" in {target["target_id"] for target in targets}) == ("correct code" not in criterion)


@pytest.mark.asyncio
async def test_generation_freezes_executed_outputs_and_one_test_rubric_item():
    generated_definition = definition()
    generated_definition["part_rubric"]["parts"][0].pop("alternative")
    candidates = [dict(target_id="q1:criterion", rubric_definition=generated_definition),
                  dict(target_id="q1:test_cases", test_cases=[case.model_dump() for case in inputs()])]
    provider = SimpleNamespace(provider_id="synthetic", ainvoke=AsyncMock(return_value=LLMResponse(
        content=json.dumps({"candidates": candidates}), provider="synthetic", model="synthetic", duration_ms=1)))
    row = source()
    targets = [dict(q_id="q1", target_id=f"q1:{name}", target=name) for name in ["criterion", "test_cases"]]
    result = await generate_missing_question_materials({"q1": row}, targets, 6, provider)
    criterion, cases = result
    assert len(cases.test_cases) == 10 and cases.test_cases[3].expected_return == "9"
    steps = criterion.rubric_draft["plan"]["part_rubric"]["parts"][0]["steps"]
    assert len(steps) == 2 and sum(step.get("test_suite", False) for step in steps) == 1
    assert "通过数" in criterion.text_value
    provider.ainvoke.assert_awaited_once()


def test_test_award_is_computed_from_frozen_budget_and_execution_once():
    row = source()
    definition_with_four_test_points = definition()
    suite, code = definition_with_four_test_points["part_rubric"]["parts"][0]["steps"]
    suite["total_pts"], code["total_pts"] = 4, 6
    criterion, draft = generated_rubric(row, RubricDefinition.model_validate(definition_with_four_test_points))
    row.update(criterion=criterion, rubric_draft=draft.model_dump(mode="json"), ai_completion_provenance={"criterion": {"source_kind": "ai_generated"}})
    problem = ProblemInfo.model_validate(row)
    problem._math_rubric_source = {"presentation": row}
    rows = frozen_programming_steps(problem)
    misses = {2, 6, 7}
    run = SimpleNamespace(status="executed", report=ExecutionReport(7, 10, .7, [
        Observation(case, number not in misses, "", "", 1,
                    exit_reason="wrong_answer" if number in misses else "success")
        for number, case in enumerate(inputs(), 1)
    ]))
    assert grading_steps(rows, run)[0]["score"] == 2.8
    output = ProgrammingGradingOutput(steps=[
        dict(step_no=1, score=4, max_points=4, is_correct=True, desc="Model incorrectly says all pass"),
        dict(step_no=2, score=5, max_points=6, is_correct=False, desc="Independent code criterion")], max_score=10, confidence=.9)
    settle_test_award(output, rows, run, "zh")
    _validate_awards(output, 10)
    assert output.score == 7.8 and output.steps[0].score == 2.8
    assert output.steps[0].part_id == "p1" and output.steps[0].item_id == "tests"
    assert "7/10" in output.steps[0].desc and not output.steps[0].is_correct
    assert [case.points for case in output.steps[0].awards] == [.4, 0, .4, .4, .4, 0, 0, .4, .4, .4]
    assert all(f"样例 {number}（结果与预期不符）" in output.steps[0].desc for number in misses)
    assert output.steps[1].score == 5
    output.steps.append(output.steps[0].model_copy())
    with pytest.raises(ValueError, match="exactly once"):
        settle_test_award(output, rows, run, "zh")
    problem.criterion += " Teacher changed the allocation."
    with pytest.raises(ValueError, match="configuration_invalid"):
        frozen_programming_steps(problem)


@pytest.mark.parametrize("budget,passed,total,expected", [(3.35, 7, 10, 2.35), (6, 7, 12, 3.5), (.005, 10, 10, .005)])
def test_test_fraction_rounds_once_and_never_exceeds_its_budget(budget, passed, total, expected):
    assert suite_award(budget, SimpleNamespace(passed_count=passed, total_count=total)) == expected


def test_ten_cases_still_have_one_fractional_test_rubric_step():
    row, proposal = source(), definition()
    row["max_score"] = proposal["part_rubric"]["total_pts"] = 1
    part = proposal["part_rubric"]["parts"][0]
    part["total_pts"] = 1
    part["steps"][0]["total_pts"], part["steps"][1]["total_pts"] = .2, .8
    _, draft = generated_rubric(row, RubricDefinition.model_validate(proposal))
    steps = draft.plan.part_rubric.parts[0].steps
    assert len(steps) == 2 and steps[0].test_suite and steps[0].total_pts == .2


def test_aggregate_test_award_cannot_leak_into_nonprogramming_rubrics():
    row = source()
    row["type"] = "计算题"
    with pytest.raises(ValueError, match="Only programming rubrics"):
        generated_rubric(row, RubricDefinition.model_validate(definition()))


@pytest.mark.asyncio
async def test_function_without_stdin_wrapper_does_not_create_ten_empty_outputs():
    row = source()
    row["stem"] = "Read an integer from stdin and print its square."
    execute = AsyncMock()
    with pytest.raises(ValueError, match="only defines functions"):
        await resolve_generated_outputs(row, [TestCase(input=str(i)) for i in range(10)], execute=execute)
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_generated_reference_keeps_independent_material_for_bounded_retry():
    row = source()
    row["solution_code"] = ""
    candidates = [dict(target_id="q1:reference_answer", text_value="Return the square."),
                  dict(target_id="q1:solution_code", text_value="def square(x): raise ValueError('bad reference')"),
                  dict(target_id="q1:test_cases", test_cases=[case.model_dump() for case in inputs()])]
    provider = SimpleNamespace(provider_id="synthetic", ainvoke=AsyncMock(return_value=LLMResponse(
        content=json.dumps({"candidates": candidates}), provider="synthetic", model="synthetic", duration_ms=1)))
    targets = [dict(q_id="q1", target_id=f"q1:{name}", target=name) for name in ["reference_answer", "solution_code", "test_cases"]]
    feedback = {}
    result = await generate_missing_question_materials({"q1": row}, targets, 10, provider,
                                                      allow_partial=True, repair_feedback=feedback)
    assert [item.target for item in result] == ["reference_answer"]
    assert "q1:solution_code" in feedback and "q1:test_cases" in feedback
    provider.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_grading_preserves_code_awards_and_settles_test_fraction(monkeypatch):
    row = source()
    criterion, draft = generated_rubric(row, RubricDefinition.model_validate(definition()))
    row.update(criterion=criterion, test_cases=await resolve_generated_outputs(row, inputs()),
               rubric_draft=draft.model_dump(mode="json"), ai_completion_provenance={"criterion": {"source_kind": "ai_generated"}})
    problem = ProblemInfo.model_validate(row)
    problem._math_rubric_source = {"presentation": row}
    prediction = dict(steps=[
        dict(step_no=1, score=6, max_points=6, is_correct=True, desc="Predicted all passing"),
        dict(step_no=2, score=3, max_points=4, is_correct=False, desc="Independent source criterion")], max_score=10, confidence=.9)
    provider = SimpleNamespace(provider_id="synthetic", ainvoke=AsyncMock(return_value=LLMResponse(
        content=json.dumps(prediction), provider="synthetic", model="synthetic", duration_ms=1)))
    monkeypatch.setattr("backend.skills.programming.kb_tool.retrieve_for_grading", AsyncMock(return_value=[]))
    answer = StudentAnswerInfo(q_id="q1", number="1", type="编程题", content="def square(x): return x*x if x<7 else -1")
    result = await ProgrammingSkill(provider, language="zh").grade(problem, answer)
    assert result.error_kind is None, result.comment
    assert result.score == 7.2 and [step.score for step in result.steps] == [4.2, 3]
    assert result.programming_evidence.generated_tests.passed == 7
    assert "7/10" in result.comment


@pytest.mark.asyncio
async def test_new_test_rubric_cannot_fall_back_to_model_predicted_outputs(monkeypatch):
    row = source()
    criterion, draft = generated_rubric(row, RubricDefinition.model_validate(definition()))
    row.update(criterion=criterion, rubric_draft=draft.model_dump(mode="json"),
               ai_completion_provenance={"criterion": {"source_kind": "ai_generated"}})
    problem = ProblemInfo.model_validate(row)
    problem._math_rubric_source = {"presentation": row}
    generate = AsyncMock(side_effect=AssertionError("Do not invent new outputs during grading"))
    monkeypatch.setattr("backend.skills.programming._generate_test_cases", generate)
    result = await ProgrammingSkill(SimpleNamespace(provider_id="synthetic")).grade(problem,
        StudentAnswerInfo(q_id="q1", number="1", type="编程题", content="def square(x): return x*x"))
    assert result.error_kind == "programming_cases_unavailable"
    assert result.programming_evidence.score_status == "unavailable"
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_runner_preserves_independent_generated_material(monkeypatch):
    from backend.tools.code_interpreter import HostCodeExecutionDisabled
    candidates = [dict(target_id="q1:reference_answer", text_value="Return the square."),
                  dict(target_id="q1:test_cases", test_cases=[case.model_dump() for case in inputs()])]
    provider = SimpleNamespace(provider_id="synthetic", ainvoke=AsyncMock(return_value=LLMResponse(
        content=json.dumps({"candidates": candidates}), provider="synthetic", model="synthetic", duration_ms=1)))
    monkeypatch.setattr("backend.tools.code_interpreter.run_sandbox", AsyncMock(side_effect=HostCodeExecutionDisabled()))
    feedback = {}
    result = await generate_missing_question_materials({"q1": source()}, [
        dict(q_id="q1", target_id=f"q1:{name}", target=name) for name in ["reference_answer", "test_cases"]
    ], 10, provider, allow_partial=True, repair_feedback=feedback)
    assert [item.target for item in result] == ["reference_answer"]
    assert "unavailable" in feedback["q1:test_cases"]
