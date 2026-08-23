"""Unit tests for the rewritten CalculationSkill (sympy verification ladder).

Coverage:
  - Reference present + sympy matches      → score = max_score, ✓ footer
  - Reference present + sympy mismatches   → score capped, ✗ footer
  - No reference + LLM-generated sympy ok  → matched path
  - No reference + LLM sympy fails         → sympy_failed footer + LLM_ONLY
  - Unparseable student answer             → sympy_failed footer

Run with:
    python -m pytest backend/tests/test_calculation_skill.py -v
"""
from __future__ import annotations

import json
import os
# Tests must not pick up the developer's proxy
os.environ["SMARTAI_HTTP_PROXY"] = ""
os.environ["SMARTAI_HTTPS_PROXY"] = ""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.models import ExpertResult, ProblemInfo, StudentAnswerInfo
from backend.skills.calculation import (
    CalculationSkill,
    _extract_final_expression,
    _format_metadata_zh,
    _run_sympy_loop,
)
from backend.tools.grading_runner import RunnerResult


# ─── Helper: build a fake provider that returns a canned LLM response ────────

def _fake_provider_returning(response_obj: Any, raw_content: str = "{}"):
    """Return a MagicMock provider whose ainvoke returns response_obj.

    structured_llm_call wraps provider.ainvoke and parses the .content. We
    instead patch structured_llm_call directly in tests that need it — but
    keep this helper for tests that go through ainvoke.
    """
    provider = MagicMock()
    provider.provider_id = "mock:test"
    response = MagicMock()
    response.content = raw_content
    provider.ainvoke = AsyncMock(return_value=response)
    return provider


def _make_problem(
    *,
    q_id: str = "q1",
    type_: str = "计算题",
    stem: str = "Compute 6 * 7.",
    criterion: str = "Final value must be 42.",
    reference_answer: str | None = None,
    max_score: float = 10.0,
) -> ProblemInfo:
    return ProblemInfo(
        q_id=q_id,
        number="1",
        type=type_,
        stem=stem,
        criterion=criterion,
        reference_answer=reference_answer,
        max_score=max_score,
    )


def _make_answer(content: str) -> StudentAnswerInfo:
    return StudentAnswerInfo(q_id="q1", number="1", type="计算题", content=content)


# ─── _extract_final_expression heuristic ─────────────────────────────────────

def test_extract_final_expression_trailing_eq():
    text = "Setting up the formula: 6 * 7\nresult = 42"
    assert _extract_final_expression(text) == "42"


def test_extract_final_expression_chinese_prefix():
    text = "演算过程:\n6 乘以 7\n答案：42"
    assert _extract_final_expression(text) == "42"


def test_extract_final_expression_unparseable():
    # No digits, no operators — give up.
    assert _extract_final_expression("我不会做") is None


def test_extract_final_expression_empty():
    assert _extract_final_expression("") is None
    assert _extract_final_expression("   ") is None


# ─── _format_metadata_zh switch ──────────────────────────────────────────────

def test_metadata_matched_teacher():
    s = _format_metadata_zh("matched", has_reference=True, ref_origin="teacher")
    assert "✓" in s and "标答" in s


def test_metadata_matched_ai_computed():
    s = _format_metadata_zh("matched", has_reference=True, ref_origin="ai_computed")
    assert "✓" in s and "AI 计算结果" in s


def test_metadata_mismatched_uses_process_credit_phrasing():
    s = _format_metadata_zh("mismatched", has_reference=True, ref_origin="teacher")
    assert "✗" in s
    assert "过程分" in s


def test_metadata_sympy_failed_no_ref():
    s = _format_metadata_zh("sympy_failed", has_reference=False, ref_origin="n/a")
    assert "未启用" in s
    assert "AI 推理" in s


def test_metadata_timeout_requires_human_review():
    s = _format_metadata_zh(
        "sympy_failed",
        has_reference=False,
        ref_origin="n/a",
        loop_stop_reason="timeout",
    )
    assert "执行超时" in s
    assert "人工复核" in s


# ─── End-to-end grade() — sympy says matched (teacher reference) ─────────────

@pytest.mark.asyncio
async def test_calc_with_reference_matched(monkeypatch):
    problem = _make_problem(reference_answer="42", max_score=5.0)
    answer = _make_answer("Working: 6 * 7 = 42")

    # Mock structured_llm_call → return a 5/10 score with a comment.
    # Our skill MUST override score to max_score because sympy says matched.
    fake_output = MagicMock(
        score=5.0, max_score=10.0, confidence=0.9,
        comment="Looks right.", steps=[],
    )
    fake_raw = MagicMock(content="{}", duration_ms=100.0)

    async def fake_call(*args, **kwargs):
        return fake_output, fake_raw

    monkeypatch.setattr("backend.skills.calculation.structured_llm_call", fake_call)

    skill = CalculationSkill(provider=_fake_provider_returning(None))
    result = await skill.grade(problem, answer, student_id="s1")

    assert isinstance(result, ExpertResult)
    assert result.score == 5.0  # forced to the question's authoritative full marks
    assert result.max_score == 5.0
    assert "✓" in result.comment
    assert "标答" in result.comment


# ─── End-to-end grade() — sympy says mismatched ──────────────────────────────

@pytest.mark.asyncio
async def test_calc_with_reference_mismatched(monkeypatch):
    problem = _make_problem(reference_answer="42")
    answer = _make_answer("My calculation: 6 * 7 = 41")

    fake_output = MagicMock(
        score=9.0, max_score=10.0, confidence=0.9,
        comment="Mostly right.", steps=[],
    )
    fake_raw = MagicMock(content="{}", duration_ms=100.0)

    async def fake_call(*args, **kwargs):
        return fake_output, fake_raw

    monkeypatch.setattr("backend.skills.calculation.structured_llm_call", fake_call)

    skill = CalculationSkill(provider=_fake_provider_returning(None))
    result = await skill.grade(problem, answer, student_id="s1")

    # Process-credit cap: at most 70% of max_score.
    assert result.score <= 7.0
    assert "✗" in result.comment
    assert "过程分" in result.comment


# ─── End-to-end grade() — no reference, LLM generates sympy code ────────────

@pytest.mark.asyncio
async def test_calc_no_reference_generates_sympy(monkeypatch):
    problem = _make_problem(reference_answer=None, stem="What is 6 * 7?")
    # Use `=` so _extract_final_expression picks up "42" cleanly; otherwise the
    # heuristic returns the whole sentence and sympy can't parse it.
    answer = _make_answer("6 * 7 = 42")

    # Fake only the model generation; static checking and SymPy execution are real.
    async def fake_gen(provider, problem):
        return "from sympy import Integer\nprint(Integer(42))"

    monkeypatch.setattr("backend.skills.calculation._generate_sympy_program", fake_gen)

    fake_output = MagicMock(
        score=8.0, max_score=10.0, confidence=0.9,
        comment="Correct reasoning.", steps=[],
    )
    fake_raw = MagicMock(content="{}", duration_ms=100.0)

    async def fake_call(*args, **kwargs):
        return fake_output, fake_raw

    monkeypatch.setattr("backend.skills.calculation.structured_llm_call", fake_call)

    skill = CalculationSkill(provider=_fake_provider_returning(None))
    result = await skill.grade(problem, answer, student_id="s1")

    assert result.score == 10.0  # sympy matched → full marks
    assert "✓" in result.comment
    assert "AI 计算结果" in result.comment
    audit = json.loads(result.logs)
    assert audit["loop_status"] == "succeeded"
    assert audit["verification_status"] == "matched"
    assert len(audit["attempts"]) == 1


@pytest.mark.asyncio
async def test_calc_repairs_one_syntax_error_then_scores(monkeypatch):
    problem = _make_problem(reference_answer=None, stem="What is 6 * 7?")
    answer = _make_answer("6 * 7 = 42")
    repair_calls = []

    async def fake_gen(provider, problem):
        return "from sympy import Integer\nprint(Integer(42)"

    async def fake_repair(provider, problem, **kwargs):
        repair_calls.append(kwargs)
        return "from sympy import Integer\nprint(Integer(42))"

    monkeypatch.setattr("backend.skills.calculation._generate_sympy_program", fake_gen)
    monkeypatch.setattr("backend.skills.calculation._repair_sympy_program", fake_repair)

    fake_output = MagicMock(
        score=6.0, max_score=10.0, confidence=0.9,
        comment="Verified after tool repair.", steps=[],
    )
    fake_raw = MagicMock(content="{}", duration_ms=100.0)

    async def fake_call(*args, **kwargs):
        return fake_output, fake_raw

    monkeypatch.setattr("backend.skills.calculation.structured_llm_call", fake_call)

    result = await CalculationSkill(
        provider=_fake_provider_returning(None)
    ).grade(problem, answer, student_id="s1")

    assert result.score == 10.0
    assert len(repair_calls) == 1
    assert "SyntaxError" in repair_calls[0]["safe_error"]
    assert "自动修正 1 次" in result.comment
    audit = json.loads(result.logs)
    assert audit["loop_status"] == "succeeded"
    assert audit["repair_count"] == 1
    assert [item["exit_reason"] for item in audit["attempts"]] == [
        "syntax_error",
        "success",
    ]
    assert "Previous generated script" not in result.logs
    assert "6 * 7 = 42" not in result.logs


@pytest.mark.asyncio
async def test_calc_student_mismatch_never_repairs_tool_script(monkeypatch):
    problem = _make_problem(reference_answer=None, stem="What is 6 * 7?")
    answer = _make_answer("6 * 7 = 41")
    repair_calls = 0

    async def fake_gen(provider, problem):
        return "from sympy import Integer\nprint(Integer(42))"

    async def unexpected_repair(*args, **kwargs):
        nonlocal repair_calls
        repair_calls += 1
        return "print(41)"

    monkeypatch.setattr("backend.skills.calculation._generate_sympy_program", fake_gen)
    monkeypatch.setattr("backend.skills.calculation._repair_sympy_program", unexpected_repair)

    fake_output = MagicMock(
        score=9.0, max_score=10.0, confidence=0.9,
        comment="Process credit only.", steps=[],
    )
    fake_raw = MagicMock(content="{}", duration_ms=100.0)

    async def fake_call(*args, **kwargs):
        return fake_output, fake_raw

    monkeypatch.setattr("backend.skills.calculation.structured_llm_call", fake_call)

    result = await CalculationSkill(
        provider=_fake_provider_returning(None)
    ).grade(problem, answer, student_id="s1")

    assert repair_calls == 0
    assert result.score == 7.0
    assert "系统未修改学生答案" in result.comment
    audit = json.loads(result.logs)
    assert audit["repair_count"] == 0
    assert audit["verification_status"] == "mismatched"


@pytest.mark.asyncio
async def test_sympy_loop_timeout_stops_without_repair(monkeypatch):
    problem = _make_problem(reference_answer=None)
    repair_calls = 0

    async def fake_gen(provider, problem):
        return "while True:\n    pass\nprint(42)"

    async def unexpected_repair(*args, **kwargs):
        nonlocal repair_calls
        repair_calls += 1
        return "print(42)"

    monkeypatch.setattr("backend.skills.calculation._generate_sympy_program", fake_gen)
    monkeypatch.setattr("backend.skills.calculation._repair_sympy_program", unexpected_repair)

    loop = await _run_sympy_loop(
        _fake_provider_returning(None),
        problem,
        timeout=0.1,
        execution_id="timeout-loop",
    )

    assert loop.status == "timeout"
    assert loop.stop_reason == "timeout"
    assert loop.repair_count == 0
    assert repair_calls == 0
    assert len(loop.attempts) == 1
    assert loop.attempts[0].runner_result.exit_reason.value == "timeout"


@pytest.mark.asyncio
async def test_sympy_loop_never_exceeds_one_repair(monkeypatch):
    problem = _make_problem(reference_answer=None)
    repair_calls = 0

    async def fake_gen(provider, problem):
        return "from sympy import Integer\nprint(Integer(42)"

    async def still_broken(*args, **kwargs):
        nonlocal repair_calls
        repair_calls += 1
        return "from sympy import Integer\nprint(Integer(42)"

    monkeypatch.setattr("backend.skills.calculation._generate_sympy_program", fake_gen)
    monkeypatch.setattr("backend.skills.calculation._repair_sympy_program", still_broken)

    loop = await _run_sympy_loop(
        _fake_provider_returning(None),
        problem,
        max_repairs=99,
        execution_id="repair-budget",
    )

    assert loop.status == "repair_exhausted"
    assert loop.repair_count == 1
    assert repair_calls == 1
    assert len(loop.attempts) == 2


@pytest.mark.asyncio
async def test_sympy_loop_preserves_pretty_sanitizer_and_integral_metadata(monkeypatch):
    problem = _make_problem(reference_answer=None)
    executed_code = ""

    async def fake_gen(provider, problem):
        return (
            "import sympy as sp\n"
            "x = sp.symbols('x')\n"
            "result = sp.integrate(x**2, x)\n"
            "print(sp.pretty(result))\n"
        )

    async def fake_runner(request):
        nonlocal executed_code
        executed_code = request.code
        return RunnerResult(
            execution_id=request.execution_id,
            executed=True,
            exit_reason="success",
            stdout="x**3/3",
        )

    monkeypatch.setattr("backend.skills.calculation._generate_sympy_program", fake_gen)

    loop = await _run_sympy_loop(
        _fake_provider_returning(None),
        problem,
        execution_id="pretty-integral-loop",
        runner=fake_runner,
    )

    assert "pretty" not in executed_code
    assert "print(str(result))" in executed_code
    assert loop.status == "succeeded"
    assert loop.reference_value == "x**3/3"
    assert loop.is_indefinite_integral is True
    assert loop.integral_variable == "x"


# ─── End-to-end grade() — sympy code execution fails ────────────────────────

@pytest.mark.asyncio
async def test_calc_sympy_failed_fallback(monkeypatch):
    problem = _make_problem(reference_answer=None)
    answer = _make_answer("Some attempt: result = 99")

    async def fake_gen(provider, problem):
        return "from sympy import Integer\nraise RuntimeError('broken')\nprint(Integer(42))"

    async def fake_repair(*args, **kwargs):
        return None

    monkeypatch.setattr("backend.skills.calculation._generate_sympy_program", fake_gen)
    monkeypatch.setattr("backend.skills.calculation._repair_sympy_program", fake_repair)

    fake_output = MagicMock(
        score=4.0, max_score=10.0, confidence=0.5,
        comment="Couldn't fully verify.", steps=[],
    )
    fake_raw = MagicMock(content="{}", duration_ms=100.0)

    async def fake_call(*args, **kwargs):
        return fake_output, fake_raw

    monkeypatch.setattr("backend.skills.calculation.structured_llm_call", fake_call)

    skill = CalculationSkill(provider=_fake_provider_returning(None))
    result = await skill.grade(problem, answer, student_id="s1")

    # LLM_ONLY branch — score honors LLM output; metadata asks for review.
    assert result.score == 4.0
    assert "未完成" in result.comment
    assert "人工复核" in result.comment
    audit = json.loads(result.logs)
    assert audit["loop_status"] == "repair_generation_failed"
    assert audit["repair_count"] == 1


# ─── End-to-end grade() — student answer too vague to extract ────────────────

@pytest.mark.asyncio
async def test_calc_unparseable_student_answer(monkeypatch):
    problem = _make_problem(reference_answer="42")
    answer = _make_answer("我不会")

    fake_output = MagicMock(
        score=0.0, max_score=10.0, confidence=0.9,
        comment="No work shown.", steps=[],
    )
    fake_raw = MagicMock(content="{}", duration_ms=100.0)

    async def fake_call(*args, **kwargs):
        return fake_output, fake_raw

    monkeypatch.setattr("backend.skills.calculation.structured_llm_call", fake_call)

    skill = CalculationSkill(provider=_fake_provider_returning(None))
    result = await skill.grade(problem, answer, student_id="s1")

    # Reference present but student expression unparseable → sympy_failed
    assert "未启用" in result.comment
    assert result.score == 0.0
