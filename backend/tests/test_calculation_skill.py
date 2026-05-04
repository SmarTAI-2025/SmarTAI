"""Unit tests for the rewritten CalculationSkill (sympy verification ladder).

Coverage:
  - Reference present + sympy matches      → score = max_score, ✓ footer
  - Reference present + sympy mismatches   → score capped, ✗ footer
  - No reference + LLM-generated sympy ok  → matched path
  - No reference + LLM sympy fails         → sympy_failed footer + LLM_ONLY
  - Unparseable student answer             → sympy_failed footer

Run with:
    /opt/anaconda3/envs/smartai/bin/python -m pytest backend/tests/test_calculation_skill.py -v
"""
from __future__ import annotations

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
)


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
) -> ProblemInfo:
    return ProblemInfo(
        q_id=q_id,
        number="1",
        type=type_,
        stem=stem,
        criterion=criterion,
        reference_answer=reference_answer,
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


# ─── End-to-end grade() — sympy says matched (teacher reference) ─────────────

@pytest.mark.asyncio
async def test_calc_with_reference_matched(monkeypatch):
    problem = _make_problem(reference_answer="42")
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
    assert result.score == 10.0  # forced to full marks
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

    # _generate_sympy_program → returns code; _run_sympy_in_sandbox → returns "42".
    async def fake_gen(provider, problem):
        return "print(42)"

    async def fake_run(code, *, timeout=10.0):
        return "42"

    monkeypatch.setattr("backend.skills.calculation._generate_sympy_program", fake_gen)
    monkeypatch.setattr("backend.skills.calculation._run_sympy_in_sandbox", fake_run)

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


# ─── End-to-end grade() — sympy code execution fails ────────────────────────

@pytest.mark.asyncio
async def test_calc_sympy_failed_fallback(monkeypatch):
    problem = _make_problem(reference_answer=None)
    answer = _make_answer("Some attempt: result = 99")

    async def fake_gen(provider, problem):
        return "raise RuntimeError"

    async def fake_run(code, *, timeout=10.0):
        return None  # simulating sandbox failure

    monkeypatch.setattr("backend.skills.calculation._generate_sympy_program", fake_gen)
    monkeypatch.setattr("backend.skills.calculation._run_sympy_in_sandbox", fake_run)

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

    # LLM_ONLY branch — score honors LLM output; metadata says "未启用"
    assert result.score == 4.0
    assert "未启用" in result.comment
    assert "AI 推理" in result.comment


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
