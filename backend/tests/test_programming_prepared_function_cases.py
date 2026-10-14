"""Regression for the real Demo's prepared stdin/function interface mismatch."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.models import ProblemInfo, StudentAnswerInfo, TestCase
from backend.skills.programming import ProgrammingSkill
from backend.tools.code_interpreter import _values_match, run_sandbox


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_prepared_generated_cases_call_function_and_preserve_real_failure(monkeypatch, legacy):
    function_cases = [TestCase(source="llm_generated", function_name="stable_softmax",
                              function_args=[xs], expected_return=expected)
                      for xs, expected in [([], "[]"), ([0, 0], "[0.5, 0.5]"),
                                           ([1000, 1000], "[0.5, 0.5]")]]
    stored = ([TestCase(source="llm_generated", input="[0,0]", expected_output="[0.5,0.5]")]
              if legacy else function_cases)
    problem = ProblemInfo(q_id="q1", number="1", type="编程题", max_score=10,
        stem="Implement the required function:\ndef stable_softmax(xs):\n    ...", criterion="Numerical stability", test_cases=stored)
    answer = StudentAnswerInfo(q_id="q1", number="1", type="编程题", content=(
        "import math\ndef stable_softmax(xs):\n"
        "    if not xs: return []\n    exps = [math.exp(x) for x in xs]\n"
        "    return [v / sum(exps) for v in exps]\n"))
    generate = AsyncMock(return_value=(function_cases, None))
    monkeypatch.setattr("backend.skills.programming._generate_test_cases", generate)
    monkeypatch.setattr("backend.skills.programming.kb_tool.retrieve_for_grading", AsyncMock(return_value=[]))
    grade = AsyncMock(return_value=(MagicMock(score=5, max_score=10, confidence=.8,
        comment="Overflow defect remains.", steps=[], logs=""), MagicMock(content="{}", duration_ms=1)))
    monkeypatch.setattr("backend.skills.programming.structured_llm_call", grade)
    reports = []
    async def execute(*args, **kwargs):
        report = await run_sandbox(*args, **kwargs)
        reports.append(report)
        return report
    monkeypatch.setattr("backend.skills.programming.run_sandbox", execute)
    provider = MagicMock(provider_id="synthetic")
    result = await ProgrammingSkill(provider, language="en").grade(problem, answer, student_id="synthetic")
    # Real sandbox execution: empty/uniform inputs pass, overflow still fails.
    assert "passed 2/3" in result.comment
    assert "AI-generated" in result.comment
    assert "teacher-provided" not in result.comment
    prompt = grade.call_args.kwargs["user_prompt"]
    assert "SANDBOX_RAN_LLM_FUNCTION_CALL_CASES" in prompt
    assert "OverflowError" in reports[0].results[2].error
    assert generate.await_count == int(legacy)
    assert problem.test_cases == stored  # Do not rewrite saved teacher material.


@pytest.mark.parametrize("actual,expected,match", [
    ("[0.30000000000000004, 0.7]", "[0.3, 0.7]", True),
    ("{'x': [0.30000000000000004]}", "{'x': [0.3]}", True),
    ("[0.3, 0.7]", "[0.3, 0.6]", False),
    ("[0.3]", "[0.3, 0.7]", False),
    ("[1, 2]", "(1, 2)", False),
])
def test_function_return_float_tolerance_preserves_structure(actual, expected, match):
    assert _values_match(actual, expected) is match
