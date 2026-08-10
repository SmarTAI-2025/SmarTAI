"""
Regression tests for XTS-DSY-W1-SYMPY-LOOP P0:
indefinite-integral +C false-mismatch when the reference is AI-computed.

Background (see help/context/w1-sympy-loop-report.md §3.1):
  When a calculation problem has no teacher reference answer, the skill asks
  the LLM to generate a SymPy program.  SymPy's ``integrate(2*x+3, x)`` returns
  ``x**2 + 3*x`` *without* the +C constant.  A student who correctly writes
  ``x**2 + 3*x + C`` would then be marked mismatched by strict symbolic
  comparison (``simplify((x²+3x+C)-(x²+3x)) = C ≠ 0``) and incorrectly lose
  marks.

Fix (option 4 — minimal scope):
  Only in the *no-reference* branch, when the LLM-generated program contains
  ``integrate(``, verify by comparing derivatives (``diff(student) -
  diff(ref) == 0``) so +C vanishes.  The teacher-reference branch is untouched
  so that a student who omits C against a teacher reference that includes C is
  still correctly marked wrong.

These tests mock the LLM and sandbox so no real provider / code execution is
needed.  Run with::

    SMARTAI_E2E_FAKE_PROVIDER=true \
    python -m pytest backend/tests/test_calculation_indefinite_integral_c.py -v
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock
from typing import Any

import pytest

from backend.models import ExpertResult, ProblemInfo, StudentAnswerInfo
from backend.skills.calculation import CalculationSkill
from backend.tools import numerical


# ─── Helpers (aligned with test_calculation_skill.py conventions) ────────────

def _fake_provider_returning(response_obj: Any = None, raw_content: str = "{}"):
    provider = MagicMock()
    provider.provider_id = "mock:test"
    response = MagicMock()
    response.content = raw_content
    response.duration_ms = 10.0
    provider.ainvoke = AsyncMock(return_value=response)
    return provider


def _make_problem(
    *,
    q_id: str = "q1",
    stem: str = "Compute 6 * 7.",
    criterion: str = "Final value must be 42.",
    reference_answer: str | None = None,
    max_score: float = 10.0,
) -> ProblemInfo:
    return ProblemInfo(
        q_id=q_id,
        number="1",
        type="计算题",
        stem=stem,
        criterion=criterion,
        reference_answer=reference_answer,
        max_score=max_score,
    )


def _make_answer(content: str) -> StudentAnswerInfo:
    return StudentAnswerInfo(q_id="q1", number="1", type="计算题", content=content)


def _llm_output(score=8.0, max_score=10.0, confidence=0.9, comment="OK.", steps=None):
    return MagicMock(
        score=score, max_score=max_score, confidence=confidence,
        comment=comment, steps=steps or [],
    ), MagicMock(content="{}", duration_ms=10.0)


def _patch_generate_and_run(monkeypatch, *, sympy_code, stdout):
    import backend.skills.calculation as calc

    async def fake_generate(provider, problem):
        return sympy_code

    async def fake_run(code, *, timeout=10.0):
        return stdout

    monkeypatch.setattr(calc, "_generate_sympy_program", fake_generate)
    monkeypatch.setattr(calc, "_run_sympy_in_sandbox", fake_run)


def _spy_verifiers(monkeypatch):
    """Wrap the numerical verifiers so tests can assert which one was used."""
    calls = {"derivative": 0, "equivalent": 0}
    orig_deriv = numerical.verify_derivative_equivalent
    orig_equiv = numerical.verify_equivalent

    async def spy_deriv(s, t):
        calls["derivative"] += 1
        return await orig_deriv(s, t)

    async def spy_equiv(s, t):
        calls["equivalent"] += 1
        return await orig_equiv(s, t)

    monkeypatch.setattr(numerical, "verify_derivative_equivalent", spy_deriv)
    # calculation.py does `from backend.tools import numerical` then calls
    # numerical.verify_equivalent, so patching the attribute on the module is
    # enough.
    monkeypatch.setattr(numerical, "verify_equivalent", spy_equiv)
    return calls


# ─── Unit-level: verify_derivative_equivalent ────────────────────────────────

def test_derivative_verify_makes_plus_c_equivalent():
    """Student's correct +C should match the AI ref (which lacks C)."""
    ok = asyncio.run(
        numerical.verify_derivative_equivalent("x**2 + 3*x + C", "x**2 + 3*x")
    )
    assert ok is True


def test_derivative_verify_catches_wrong_answer():
    """A genuinely wrong antiderivative must still mismatch."""
    ok = asyncio.run(
        numerical.verify_derivative_equivalent("x**2", "x**2 + 3*x")
    )
    assert ok is False


def test_strict_verify_still_flags_plus_c_difference():
    """verify_equivalent (strict) must still see +C as a difference — this is
    the behaviour relied upon by the teacher-reference branch (s5)."""
    ok = asyncio.run(
        numerical.verify_equivalent("x**2 + 3*x", "x**2 + 3*x + C")
    )
    assert ok is False


# ─── Integration-level: CalculationSkill.grade() flow ────────────────────────

@pytest.mark.asyncio
async def test_no_reference_indefinite_integral_student_with_c_matches(monkeypatch):
    """s3 scenario: no teacher ref, LLM integrates (no +C), student writes +C.

    Before the fix this was marked mismatched (false negative) and the student
    lost marks.  After the fix the skill detects ``integrate(`` and verifies
    via derivatives, yielding ``matched`` → full marks.
    """
    import backend.skills.calculation as calc

    _patch_generate_and_run(
        monkeypatch,
        sympy_code="import sympy\nx=sympy.Symbol('x')\nprint(sympy.integrate(2*x+3, x))",
        stdout="x**2 + 3*x",
    )
    calls = _spy_verifiers(monkeypatch)

    async def fake_call(*args, **kwargs):
        return _llm_output(score=7.0, comment="过程正确。")

    monkeypatch.setattr("backend.skills.calculation.structured_llm_call", fake_call)

    skill = CalculationSkill(provider=_fake_provider_returning())
    problem = _make_problem(
        stem="求不定积分 ∫ (2*x + 3) dx。",
        criterion="按步骤给分，最终答案正确得满分。",
        reference_answer=None,
    )
    answer = _make_answer("由幂函数积分公式逐项积分，2x 得 x²、3 得 3x，= x**2 + 3*x + C")

    result = await skill.grade(problem, answer, student_id="s1")

    assert calls["derivative"] == 1, "derivative verify should be used for integral"
    assert calls["equivalent"] == 0, "strict verify must not run for no-ref integral"
    # matched → score forced to max_score
    assert result.score == 10.0
    assert "✓" in result.comment


@pytest.mark.asyncio
async def test_no_reference_non_integral_uses_strict_verify(monkeypatch):
    """A non-integral (e.g. derivative) problem must still use strict verify."""
    _patch_generate_and_run(
        monkeypatch,
        sympy_code="import sympy\nx=sympy.Symbol('x')\nprint(sympy.diff(3*x**2-12*x+9, x))",
        stdout="6*x - 12",
    )
    calls = _spy_verifiers(monkeypatch)

    async def fake_call(*args, **kwargs):
        return _llm_output(score=8.0, comment="正确。")

    monkeypatch.setattr("backend.skills.calculation.structured_llm_call", fake_call)

    skill = CalculationSkill(provider=_fake_provider_returning())
    problem = _make_problem(
        stem="求 f(x)=3*x**2-12*x+9 的导数。",
        criterion="按步骤给分。",
        reference_answer=None,
    )
    answer = _make_answer("= 6*x - 12")

    result = await skill.grade(problem, answer, student_id="s1")

    assert calls["equivalent"] == 1, "strict verify should be used for non-integral"
    assert calls["derivative"] == 0
    assert result.score == 10.0
    assert "✓" in result.comment


@pytest.mark.asyncio
async def test_teacher_reference_integral_omitting_c_still_mismatched(monkeypatch):
    """s5 scenario: teacher ref includes +C, student omits C → must mismatch.

    This guards the teacher-reference branch against accidental extension of
    the derivative-verify fix.  A student who omits C against a teacher
    reference that includes C is genuinely wrong.
    """
    calls = _spy_verifiers(monkeypatch)

    async def fake_call(*args, **kwargs):
        return _llm_output(score=9.0, comment="基本正确。")

    monkeypatch.setattr("backend.skills.calculation.structured_llm_call", fake_call)

    skill = CalculationSkill(provider=_fake_provider_returning())
    problem = _make_problem(
        stem="求不定积分 ∫ (2*x + 3) dx。",
        criterion="按步骤给分。",
        reference_answer="x**2 + 3*x + C",
    )
    answer = _make_answer("由幂函数积分公式，2x 积分得 x²，3 积分得 3x，逐项相加得 = x**2 + 3*x")

    result = await skill.grade(problem, answer, student_id="s1")

    assert calls["equivalent"] == 1, "strict verify must be used with teacher ref"
    assert calls["derivative"] == 0, "derivative verify must NOT run with teacher ref"
    # mismatched → process-credit cap at 70%
    assert result.score <= 7.0
    assert "✗" in result.comment
