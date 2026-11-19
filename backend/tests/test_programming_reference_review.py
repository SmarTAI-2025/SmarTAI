"""Preparation, persistence and scoring must agree on the same executed suite."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models import ProblemInfo, StudentAnswerInfo, TestCase
from backend.services.programming_materials import resolve_generated_outputs
from backend.services.rubric_drafts import RubricDefinition, generated_rubric
from backend.skills.programming import ProgrammingSkill
from backend.tests.test_programming_generated_materials import definition, inputs, source
from backend.tests.test_question_material_format_repair import Provider


def frozen_problem(row, cases):
    criterion, draft = generated_rubric(row, RubricDefinition.model_validate(definition()))
    row = {**row, "criterion": criterion, "test_cases": cases,
           "rubric_draft": draft.model_dump(mode="json"),
           "ai_completion_provenance": {**row.get("ai_completion_provenance", {}),
                                       "criterion": {"source_kind": "ai_generated"}}}
    problem = ProblemInfo.model_validate(row)
    problem._math_rubric_source = {"presentation": row}
    return problem


async def grade(monkeypatch, problem, code):
    prediction = {"steps": [dict(step_no=1, score=6, max_points=6, is_correct=True, desc="Tests"),
                             dict(step_no=2, score=4, max_points=4, is_correct=True, desc="Algorithm")],
                  "max_score": 10, "confidence": .9}
    provider = Provider([json.dumps(prediction)])
    monkeypatch.setattr("backend.skills.programming.kb_tool.retrieve_for_grading", AsyncMock(return_value=[]))
    return await ProgrammingSkill(provider, language="zh").grade(problem,
        StudentAnswerInfo(q_id="q1", number="1", type="编程题", content=code))


@pytest.mark.asyncio
async def test_chinese_range_survives_preparation_and_grading(monkeypatch):
    row = source()
    row["stem"] = "实现函数 square(x)。输入是 0 到 100 的整数。返回其平方。"
    cases = await resolve_generated_outputs(row, inputs())
    result = await grade(monkeypatch, frozen_problem(row, cases), row["solution_code"])
    assert result.error_kind is None, result.comment
    assert result.programming_evidence.generated_tests.total == 10
    assert result.score == 10


@pytest.mark.asyncio
async def test_unknown_domain_is_not_saved_as_a_usable_suite():
    row = source()
    row["stem"] = "Implement def square(x): x is a prime integer. Return x*x."
    execute = AsyncMock()
    with pytest.raises(ValueError, match="domain"):
        await resolve_generated_outputs(row, inputs(), execute=execute)
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_positional_keyword_aliases_cannot_inflate_generated_count():
    cases = inputs()[:9] + [TestCase(io_mode="function", function_args=[], function_kwargs={"x": 0})]
    with pytest.raises(ValueError, match="distinct"):
        await resolve_generated_outputs(source(), cases)


@pytest.mark.asyncio
async def test_reference_signature_also_deduplicates_an_unnamed_function():
    row = source()
    row["stem"] = "Implement a function. Input is an integer. Return its square."
    cases = inputs()[:9] + [TestCase(io_mode="function", function_args=[], function_kwargs={"x": 0})]
    with pytest.raises(ValueError, match="distinct"):
        await resolve_generated_outputs(row, cases)


@pytest.mark.parametrize("value,expected", [(0, "allowed"), (100, "allowed"), (-1, "outside"), (101, "outside"), (.5, "outside")])
@pytest.mark.parametrize("function", [True, False])
def test_chinese_range_keeps_both_bounds_and_integer_type(value, expected, function):
    from backend.services.programming_input_domain import input_domain
    row = source()
    row["stem"] = ("实现函数 square(x)。" if function else "从标准输入读取。") + "输入是 0 到 100 的整数。返回其平方。"
    if not function:
        row["stem"] = "输入是 0 到 100 的整数。输出其平方。"
    case = TestCase(io_mode="function", function_args=[value]) if function else TestCase(input=str(value))
    assert input_domain(ProblemInfo.model_validate(row), case) == expected


def test_effective_input_identity_handles_defaults_without_evaluating_code():
    from backend.services.programming_test_contract import case_input_key
    stem = "Implement def square(x=0): x is an integer. Return x*x."
    omitted = TestCase(io_mode="function", function_args=[])
    positional = TestCase(io_mode="function", function_args=[0])
    keyword = TestCase(io_mode="function", function_args=[], function_kwargs={"x": 0})
    assert case_input_key(stem, omitted) == case_input_key(stem, positional) == case_input_key(stem, keyword)
    assert case_input_key(stem, omitted) != case_input_key(stem, TestCase(io_mode="function", function_args=[None]))
    dynamic = "Implement def square(x=danger()): x is an integer. Return x*x."
    assert case_input_key(dynamic, positional)
    with pytest.raises(ValueError, match="explicit arguments"):
        case_input_key(dynamic, omitted)


@pytest.mark.parametrize("conflict", [None, "expected_return", "abs_tol"])
def test_deduplication_preserves_teacher_authority_and_rejects_conflicts(conflict):
    from backend.services.programming_rubric import distinct_scoring_cases
    generated = TestCase(io_mode="function", function_args=[0], expected_return="0", source="llm_generated")
    teacher = TestCase(io_mode="function", function_args=[], function_kwargs={"x": 0}, expected_return="0", source="teacher")
    if conflict:
        teacher = teacher.model_copy(update={conflict: "1" if conflict == "expected_return" else .1})
    cases = [generated, teacher]
    problem = frozen_problem(source(), cases)
    if conflict:
        with pytest.raises(ValueError, match="conflicting"):
            distinct_scoring_cases(problem, cases, cases)
    else:
        originals, kept = distinct_scoring_cases(problem, cases, cases)
        assert len(kept) == 1 and kept[0].source == "teacher" and originals == [teacher]
    assert len(cases) == 2 and generated.source == "llm_generated"


@pytest.mark.parametrize("declared", [False, True])
def test_fenced_signature_is_formatting_not_an_invented_input_domain(declared):
    from backend.services.programming_input_domain import input_domain
    row = source()
    row["stem"] = "Implement the function.\n```python\ndef square(x):\n    ...\n```"
    if declared:
        row["stem"] += "\nx is an integer."
    result = input_domain(ProblemInfo.model_validate(row), inputs()[0])
    assert result == ("allowed" if declared else "unknown")


def test_dictionary_value_order_is_not_erased_by_input_identity():
    from backend.services.programming_test_contract import case_input_key
    stem = "Implement def f(x):"
    a = TestCase(io_mode="function", function_args=[{"a": 1, "b": 2}])
    b = TestCase(io_mode="function", function_args=[{"b": 2, "a": 1}])
    assert case_input_key(stem, a) != case_input_key(stem, b)


def test_rounded_test_award_obeys_the_existing_full_credit_contract():
    from backend.services.programming_rubric import settle_test_award
    from backend.skills.programming import ProgrammingGradingOutput, _validate_awards
    from backend.tools.code_interpreter import ExecutionReport, TestResult as Observation
    rows = [(SimpleNamespace(id="p1"), SimpleNamespace(id="tests", test_suite=True, total_pts=.01)),
            (SimpleNamespace(id="p1"), SimpleNamespace(id="code", test_suite=False, total_pts=.99))]
    output = ProgrammingGradingOutput(steps=[
        dict(step_no=1, score=.01, max_points=.01, is_correct=True, desc="Tests"),
        dict(step_no=2, score=.99, max_points=.99, is_correct=True, desc="Code")], max_score=1, confidence=.9)
    report = ExecutionReport(9, 10, .9, [Observation(TestCase(), i < 9, "", "", 1,
        exit_reason="success" if i < 9 else "wrong_answer") for i in range(10)])
    settle_test_award(output, rows, SimpleNamespace(report=report), "zh")
    _validate_awards(output, 1)
    assert output.score == 1 and output.steps[0].is_correct
    assert "9/10" in output.steps[0].desc


@pytest.mark.asyncio
async def test_omitted_literal_default_is_admitted_using_its_actual_value():
    row = source()
    row.update(stem="Implement def square(x=0): x is an integer. Return x*x.",
               solution_code="def square(x=0): return x*x")
    cases = [TestCase(io_mode="function", function_args=[]), *inputs()[1:]]
    resolved = await resolve_generated_outputs(row, cases)
    assert len(resolved) == 10 and resolved[0].expected_return == "0"


@pytest.mark.asyncio
async def test_later_tests_keep_the_saved_rubric_domain_context():
    row = source()
    problem = frozen_problem(row, [])
    saved = problem._math_rubric_source["presentation"]
    cases = await resolve_generated_outputs(saved, inputs())
    assert len(cases) == 10


@pytest.mark.asyncio
async def test_saved_aliases_do_not_change_test_weight(monkeypatch):
    row = source()
    cases = [TestCase(io_mode="function", function_args=[i], expected_return=str(i*i), source="llm_generated") for i in range(9)]
    cases.append(TestCase(io_mode="function", function_args=[], function_kwargs={"x": 0},
                          expected_return="0", source="llm_generated"))
    result = await grade(monkeypatch, frozen_problem(row, cases), "def square(x): return -1 if x == 0 else x*x")
    assert result.error_kind is None, result.comment
    assert result.steps[0].score == 5.33
    assert result.programming_evidence.generated_tests.total == 9
    assert "8/9" in result.steps[0].desc


@pytest.mark.asyncio
@pytest.mark.parametrize("setup", ["import sys\nsys.setrecursionlimit(10000)\n", ""])
async def test_unused_stdin_entry_cannot_produce_empty_expectations(setup):
    row = source()
    row.update(stem="Read an integer from stdin and print its square.",
               solution_code=setup + "def solve():\n n=int(input())\n print(n*n)\n")
    with pytest.raises(ValueError):
        await resolve_generated_outputs(row, [TestCase(input=str(i)) for i in range(10)])


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["n=int(input())", "def solve(): print(int(input())**2)\nif False: solve()"])
async def test_runtime_output_check_catches_missing_print_and_dead_entry(code):
    row = source()
    row.update(stem="Read an integer from stdin and print its square.", solution_code=code)
    with pytest.raises(ValueError, match="no stdout"):
        await resolve_generated_outputs(row, [TestCase(input=str(i)) for i in range(10)])


@pytest.mark.asyncio
async def test_individual_empty_outputs_and_teacher_expectations_are_preserved():
    row = source()
    teacher = TestCase(input="0", expected_output="")
    row.update(stem="Read an integer from stdin. If n > 0 then print n*n.",
               solution_code="n=int(input())\nif n > 0: print(n*n)", test_cases=[teacher])
    cases = await resolve_generated_outputs(row, [TestCase(input=str(i)) for i in range(10)])
    assert cases[0].expected_output == teacher.expected_output == ""
    assert cases[1].expected_output == "1\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_round", [2, 3, None])
async def test_split_generation_keeps_reference_pending_until_tests_validate(monkeypatch, repair_round):
    from backend.agents import incremental_question_preparation as inc
    from backend.llm.providers import LLMResponse
    from backend.tests.test_incremental_question_preparation import setup, persisted
    from backend.domain.errors import ValidationError
    owner, _, operation, pipeline = await setup(monkeypatch)
    row = source(reference_answer="Return the square.")
    row["solution_code"] = ""
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([row])))
    calls = []
    async def invoke(messages):
        request = json.loads(messages[1].content.split("\n", 1)[1])
        targets = [t["target"] for t in request["requested_targets"]]
        calls.append(targets)
        if len(calls) == 1:
            candidates = [dict(target_id="q1:criterion", rubric_definition=definition()),
                          dict(target_id="q1:solution_code", text_value="def square(x): raise ValueError('bad')")]
        else:
            candidates = [dict(target_id="q1:test_cases", test_cases=[c.model_dump() for c in inputs()])]
            if "solution_code" in targets:
                code = ("def square(x): return x*x" if len(calls) == repair_round else
                        "def square(x): raise ValueError('bad')")
                candidates.append(dict(target_id="q1:solution_code", text_value=code))
        return LLMResponse(content=json.dumps({"candidates": candidates}), model="synthetic", provider="synthetic", duration_ms=1)
    pipeline.provider = SimpleNamespace(provider_id="synthetic", ainvoke=invoke)
    if repair_round is None:
        with pytest.raises(ValidationError):
            await pipeline.run()
    else:
        await pipeline.run()
    _, rows = persisted(owner, operation)
    assert rows["q1"]["criterion"]
    assert rows["q1"]["solution_code"] == ("def square(x): return x*x" if repair_round else "")
    assert len(rows["q1"].get("test_cases") or []) == (10 if repair_round else 0)
    assert len(calls) == (repair_round or 3)
    assert all(set(targets) == {"solution_code", "test_cases"} for targets in calls[1:])


@pytest.mark.asyncio
async def test_failed_teacher_code_is_never_reopened_as_ai_owned():
    from backend.agents.ingest_agent import generate_missing_question_materials
    row = source()
    row["solution_code"] = "def square(x): raise ValueError('teacher code')"
    provider = Provider([json.dumps({"candidates": [dict(target_id="q1:test_cases",
        test_cases=[case.model_dump() for case in inputs()])]})])
    feedback = {}
    result = await generate_missing_question_materials({"q1": row},
        [dict(q_id="q1", target_id="q1:test_cases", target="test_cases")], 10, provider,
        allow_partial=True, repair_feedback=feedback)
    assert result == [] and "q1:test_cases" in feedback and "q1:solution_code" not in feedback
    assert row["solution_code"] == "def square(x): raise ValueError('teacher code')"


@pytest.mark.asyncio
async def test_teacher_code_is_reused_without_requesting_an_ai_implementation():
    from backend.agents.ingest_agent import generate_missing_question_materials
    row = source(reference_answer="```python\ndef square(x): return x*x\n```")
    row["solution_code"] = ""
    provider = Provider([json.dumps({"candidates": [
        dict(target_id="q1:test_cases", test_cases=[case.model_dump() for case in inputs()])
    ]})])
    result = await generate_missing_question_materials({"q1": row}, [
        dict(q_id="q1", target_id=f"q1:{target}", target=target) for target in ["solution_code", "test_cases"]
    ], 10, provider)
    request = json.loads(provider.calls[0][1].content.split("\n", 1)[1])
    assert [target["target"] for target in request["requested_targets"]] == ["test_cases"]
    assert len(result) == 1 and result[0].test_cases[3].expected_return == "9"
    assert len(provider.calls) == 1 and row["solution_code"] == ""
    assert row["reference_answer"] == "```python\ndef square(x): return x*x\n```"


@pytest.mark.asyncio
async def test_redundant_code_only_request_uses_zero_model_calls():
    from backend.agents.ingest_agent import generate_missing_question_materials
    row = source(reference_answer="```python\ndef square(x): return x*x\n```")
    row["solution_code"] = ""
    provider = SimpleNamespace(ainvoke=AsyncMock(side_effect=AssertionError("No model call is needed")))
    assert await generate_missing_question_materials({"q1": row}, [
        dict(q_id="q1", target_id="q1:solution_code", target="solution_code")], 10, provider) == []
    provider.ainvoke.assert_not_awaited()


@pytest.mark.parametrize("teacher", [False, True])
def test_automatic_and_manual_target_selection_share_supplied_code_authority(teacher):
    from backend.agents.incremental_question_preparation import _rows
    from backend.agents.question_preparation_agent import requested_major_question_materials
    from backend.api.task_preparation import _missing_targets
    row = source(reference_answer="```python\ndef square(x): return x*x\n```")
    row["solution_code"] = ""
    if teacher:
        row["material_provenance"] = {"reference_answer": {"source_kind": "upload"}}
    else:
        row["ai_completion_provenance"] = {"reference_answer": {"source_kind": "ai_generated"}}
    data = _rows([row])
    for targets in [requested_major_question_materials(data), _missing_targets({"problem_data": data})]:
        names = {target["target"] for target in targets}
        assert ("solution_code" not in names) is teacher
        assert "reference_answer" not in names


@pytest.mark.asyncio
async def test_real_preparation_uses_teacher_code_without_a_duplicate_solution(monkeypatch):
    from backend.agents import incremental_question_preparation as inc
    from backend.tests.test_incremental_question_preparation import setup, persisted
    owner, _, operation, pipeline = await setup(monkeypatch)
    row = source(reference_answer="```python\ndef square(x): return x*x\n```")
    row["solution_code"] = ""
    monkeypatch.setattr(inc, "_extract_problems_call", AsyncMock(return_value=inc._rows([row])))
    provider = Provider([json.dumps({"candidates": [
        dict(target_id="q1:criterion", rubric_definition=definition()),
        dict(target_id="q1:test_cases", test_cases=[case.model_dump() for case in inputs()])
    ]})])
    pipeline.provider = provider
    await pipeline.run()
    _, rows = persisted(owner, operation)
    request = json.loads(provider.calls[0][1].content.split("\n", 1)[1])
    assert {target["target"] for target in request["requested_targets"]} == {"criterion", "test_cases"}
    assert len(provider.calls) == 1 and len(rows["q1"]["test_cases"]) == 10
    assert not rows["q1"].get("solution_code")
    assert "solution_code" not in rows["q1"].get("ai_completion_provenance", {})
