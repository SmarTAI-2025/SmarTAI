"""Frozen question generation, Python function execution, and AI-only review."""
from __future__ import annotations

import os

os.environ["SMARTAI_HTTP_PROXY"] = ""
os.environ["SMARTAI_HTTPS_PROXY"] = ""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.models import ExpertResult, ProblemInfo, StudentAnswerInfo, TestCase
from backend.skills.programming import (
    ProgrammingSkill,
    _generate_test_cases,
)
from backend.tools.code_interpreter import (
    _values_match,
    run_sandbox,
)


# ─── _values_match (numeric tolerance + literal_eval) ─────────────────────────


def test_values_match_int_vs_str():
    assert _values_match("5", "5")


def test_values_match_list_normalization():
    # repr() of a list vs JSON-style; literal_eval makes them equal
    assert _values_match("[1, 2, 3]", "[1,2,3]")


def test_values_match_float_tolerance():
    assert _values_match("0.30000000000000004", "0.3")


def test_values_match_mismatch():
    assert not _values_match("5", "6")


# ─── Sandbox function-call mode (real subprocess) ────────────────────────────


@pytest.mark.asyncio
async def test_sandbox_function_call_mode():
    student = "def add(a, b):\n    return a + b\n"
    cases = [
        TestCase(
            description="2+3",
            source="llm_generated",
            function_name="add",
            function_args=[2, 3],
            expected_return="5",
        ),
        TestCase(
            description="negative",
            source="llm_generated",
            function_name="add",
            function_args=[-1, 1],
            expected_return="0",
        ),
        TestCase(
            description="should fail",
            source="llm_generated",
            function_name="add",
            function_args=[1, 1],
            expected_return="3",  # wrong on purpose
        ),
    ]
    report = await run_sandbox(student, cases, language="python", per_case_timeout=5.0)
    assert report.total_count == 3
    assert report.passed_count == 2


@pytest.mark.asyncio
async def test_sandbox_function_call_fibonacci():
    """The actual user-reported scenario: fib(n) via DP."""
    student = (
        "def fib(n):\n"
        "    if n < 2:\n"
        "        return n\n"
        "    a, b = 0, 1\n"
        "    for _ in range(n-1):\n"
        "        a, b = b, a + b\n"
        "    return b\n"
    )
    cases = [
        TestCase(function_name="fib", function_args=[0], expected_return="0", source="llm_generated"),
        TestCase(function_name="fib", function_args=[1], expected_return="1", source="llm_generated"),
        TestCase(function_name="fib", function_args=[10], expected_return="55", source="llm_generated"),
    ]
    report = await run_sandbox(student, cases, language="python", per_case_timeout=5.0)
    assert report.passed_count == 3, report.summary


# ─── _generate_test_cases error-kind split ───────────────────────────────────


@pytest.mark.asyncio
async def test_generate_test_cases_distinguishes_transient_failure():
    problem = ProblemInfo(q_id="q1", number="1", type="编程题", stem="x", criterion="y")
    provider = MagicMock()
    provider.provider_id = "mock:test"

    with patch("backend.skills.programming.structured_llm_call", new=AsyncMock(side_effect=RuntimeError("timeout"))):
        cases, err = await _generate_test_cases(provider, problem)
    assert cases == []
    assert err == "transient_llm"


@pytest.mark.asyncio
async def test_generate_test_cases_distinguishes_empty_response():
    problem = ProblemInfo(q_id="q1", number="1", type="编程题", stem="x", criterion="y")
    provider = MagicMock()
    provider.provider_id = "mock:test"

    fake_result = MagicMock()
    fake_result.cases = []
    with patch(
        "backend.skills.programming.structured_llm_call",
        new=AsyncMock(return_value=(fake_result, MagicMock())),
    ):
        cases, err = await _generate_test_cases(provider, problem)
    assert cases == []
    assert err == "programming_no_cases"


# ─── ProgrammingSkill end-to-end: function-only path skips sandbox cleanly ──


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "zh"])
async def test_generation_failure_uses_no_grading_vote_or_execution(language):
    provider = MagicMock(provider_id="mock:test")
    skill = ProgrammingSkill(provider, language=language)
    problem = ProblemInfo(q_id="q1", number="1", type="编程题", stem="def fib(n):", criterion="正确性")
    answer = StudentAnswerInfo(q_id="q1", number="1", type="编程题", content="def fib(n): return n")
    failed = AsyncMock(side_effect=TimeoutError())
    with patch("backend.skills.programming.structured_llm_call", new=failed), patch(
            "backend.skills.programming.run_sandbox", new_callable=AsyncMock) as execute:
        result = await skill.grade(problem, answer, student_id="S1")
    assert result.error_kind == "transient_llm"
    assert result.programming_evidence.score_status == "unavailable"
    assert result.programming_evidence.teacher_tests.total == result.programming_evidence.generated_tests.total == 0
    failed.assert_awaited_once()
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_programming_skill_zero_pass_rate_caps_confidence():
    """If sandbox runs ≥3 LLM-generated cases and 0 pass, confidence ≤ 0.5
    even when the LLM said it was confident."""
    provider = MagicMock()
    provider.provider_id = "mock:test"
    skill = ProgrammingSkill(provider)

    problem = ProblemInfo(
        q_id="q1", number="1", type="编程题",
        stem="A program that prints 'hello'.", criterion="Correctness.",
    )
    # Student code that just prints something else — guarantees 0% pass rate
    # against any reasonable LLM-generated stdin/stdout test
    answer = StudentAnswerInfo(
        q_id="q1", number="1", type="编程题",
        content="print('definitely not what you wanted')\n",
    )

    # Fake _generate_test_cases output: 3 stdin/stdout cases
    cases = MagicMock()
    cases.cases = [
        TestCase(input=str(i), expected_output="hello", description=f"case{i}", sandbox_feasible=True)
        for i in range(3)
    ]

    grade = MagicMock()
    grade.score = 8.0
    grade.max_score = 10.0
    grade.confidence = 0.95  # LLM was very confident
    grade.comment = "Looks great!"
    grade.steps = []
    grade.logs = ""
    raw = MagicMock(); raw.content = "{}"; raw.duration_ms = 100.0

    call_count = {"n": 0}

    async def fake_structured(*_args, **kwargs):
        call_count["n"] += 1
        return (cases, raw) if call_count["n"] == 1 else (grade, raw)

    with patch("backend.skills.programming.structured_llm_call", new=fake_structured):
        result = await skill.grade(problem, answer, student_id="S1")

    assert result.confidence <= 0.5, (
        f"Expected confidence cap to ≤ 0.5 on 0% pass-rate, got {result.confidence}"
    )

    assert result.programming_evidence.generated_tests.total == 3
    assert result.programming_evidence.requires_human_review
