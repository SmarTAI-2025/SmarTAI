"""Unit tests for programming-skill function-only / LeetCode-mode behaviors:

- _detect_function_only correctly fires when student only submitted a def
  (no top-level input/print).
- run_sandbox in function-call mode wraps the student function and matches
  expected_return through ast.literal_eval normalization.
- Sandbox 0/N pass on llm_generated cases caps confidence ≤ 0.5 in the
  ExpertResult coming out of ProgrammingSkill.grade().
- _generate_test_cases returns ("llm_failed_transient", []) when the
  underlying provider raises, vs ("llm_returned_empty", []) when LLM's parsed
  list is empty.

Run:
    /opt/anaconda3/envs/smartai/bin/python -m pytest backend/tests/test_programming_function_only.py -v
"""
from __future__ import annotations

import os

os.environ["SMARTAI_HTTP_PROXY"] = ""
os.environ["SMARTAI_HTTPS_PROXY"] = ""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.models import ExpertResult, ProblemInfo, StudentAnswerInfo, TestCase
from backend.skills.programming import (
    ProgrammingSkill,
    _detect_function_only,
    _generate_test_cases,
)
from backend.tools.code_interpreter import (
    _values_match,
    run_sandbox,
)


# ─── _detect_function_only ───────────────────────────────────────────────────


def test_detect_function_only_yes():
    code = "def fib(n):\n    if n < 2: return n\n    return fib(n-1) + fib(n-2)"
    assert _detect_function_only(code) == "fib"


def test_detect_function_only_no_when_print_present():
    code = "def fib(n):\n    return n\nprint(fib(5))"
    assert _detect_function_only(code) is None


def test_detect_function_only_no_when_input_present():
    code = "n = int(input())\ndef fib(n): return n"
    assert _detect_function_only(code) is None


def test_detect_function_only_returns_first_def():
    code = "def helper(x): return x\n\ndef main(n): return n+1"
    assert _detect_function_only(code) == "helper"


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
    assert err == "llm_failed_transient"


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
    assert err == "llm_returned_empty"


# ─── ProgrammingSkill end-to-end: function-only path skips sandbox cleanly ──


@pytest.mark.asyncio
async def test_programming_skill_function_only_when_llm_fails():
    """Student submits bare `def`, LLM call to generate function-call cases
    fails transiently → cases_source 'llm_call_failed', no sandbox, comment
    has the network/quota footer."""
    provider = MagicMock()
    provider.provider_id = "mock:test"

    skill = ProgrammingSkill(provider)
    problem = ProblemInfo(
        q_id="q1", number="1", type="编程题",
        stem="实现 fib(n)", criterion="正确性",
    )
    answer = StudentAnswerInfo(
        q_id="q1", number="1", type="编程题",
        content="def fib(n):\n    return n if n < 2 else fib(n-1)+fib(n-2)\n",
    )

    # Make _generate_test_cases fail transiently AND make the final
    # grading LLM call return a successful structured grade.
    grading_result = MagicMock()
    grading_result.score = 7.0
    grading_result.max_score = 10.0
    grading_result.confidence = 0.7
    grading_result.comment = "Function looks correct."
    grading_result.steps = []
    grading_result.logs = ""
    fake_raw = MagicMock()
    fake_raw.content = "{}"
    fake_raw.duration_ms = 100.0

    call_count = {"n": 0}

    async def fake_structured(*_args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # _generate_test_cases call → simulate transient failure
            raise RuntimeError("Request timed out.")
        # subsequent call: the actual grading
        return grading_result, fake_raw

    with patch("backend.skills.programming.structured_llm_call", new=fake_structured):
        result = await skill.grade(problem, answer, student_id="S1")

    assert isinstance(result, ExpertResult)
    assert result.confidence == pytest.approx(0.7)
    assert "调用失败" in result.comment  # the llm_call_failed footer
    assert "fib" in result.comment.lower() or "Function looks correct" in result.comment


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
        TestCase(input="", expected_output="hello", description=f"case{i}", sandbox_feasible=True)
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
