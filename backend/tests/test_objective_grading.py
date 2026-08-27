"""Unit tests for ObjectiveSkill (选择题 / 多选题 / 填空题).

Contract under test (题目识别与判卷问题记录 §1 "选择题、填空题不应有过程分"):

  * grading is result-based: correct final answer → full marks, else 0;
  * NO process (step) scores are ever emitted — ``steps`` is always [];
  * 多选题 partial credit only when the rubric states the rule AND the
    model explicitly confirmed it applies (fail-safe default: no credit);
  * a blank answer is certainly 0 and skips the LLM call entirely;
  * the three objective types route to ObjectiveSkill via the registry.

Run:
    python -m pytest backend/tests/test_objective_grading.py -v
"""
from __future__ import annotations

import json
import os
# Tests must not pick up the developer's proxy
os.environ["SMARTAI_HTTP_PROXY"] = ""
os.environ["SMARTAI_HTTPS_PROXY"] = ""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.models import ProblemInfo, StudentAnswerInfo
from backend.skills.base import get_skill_for_type
from backend.skills import objective
from backend.skills.objective import (
    ObjectiveGradingOutput,
    ObjectiveSkill,
    _is_result_based_criterion,
    _objective_score,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

CHOICE_STEM = (
    "1. 已知集合 $A=\\{x \\mid 0 < x < 5\\}$，$B=\\{1, 2, 3\\}$，"
    "则 $A \\cap B=$ 【　】\n"
    "A. $\\{1, 2\\}$  B. $\\{1, 3\\}$  C. $\\{2, 3\\}$  D. $\\{1, 2, 3\\}$"
)
MULTI_STEM = (
    "2. 设命题 $p$: 对任意 $x>0$，$x+\\frac{1}{x} \\ge 2$，则下列结论正确的是"
    "（有多项符合题目要求）。"
)
BLANK_STEM = "3. 若 $\\sin \\alpha = \\frac{\\sqrt{3}}{2}$ 且 $\\alpha$ 为锐角，则 $\\alpha =$________。"


def _problem(type_: str, *, criterion: str = "", max_score: float = 5.0) -> ProblemInfo:
    stems = {"选择题": CHOICE_STEM, "多选题": MULTI_STEM, "填空题": BLANK_STEM}
    return ProblemInfo(
        q_id="q1", number="1", type=type_,
        stem=stems[type_], criterion=criterion, max_score=max_score,
    )


def _answer(content: str) -> StudentAnswerInfo:
    return StudentAnswerInfo(q_id="q1", number="1", type="选择题", content=content)


def _fake_provider():
    provider = SimpleNamespace(provider_id="mock:model")
    provider.ainvoke = AsyncMock()
    return provider


def _patch_llm(monkeypatch, output: ObjectiveGradingOutput):
    """Patch structured_llm_call in the skill module to return `output`."""
    raw = SimpleNamespace(content=json.dumps(output.model_dump()), duration_ms=1.0)
    monkeypatch.setattr(
        objective, "structured_llm_call",
        AsyncMock(return_value=(output, raw)),
    )


@pytest.fixture(autouse=True)
def _no_kb(monkeypatch):
    monkeypatch.setattr(objective.kb_tool, "retrieve", AsyncMock(return_value=[]))


# ─── Registry routing ────────────────────────────────────────────────────────


def test_objective_types_route_to_objective_skill():
    for type_ in ("选择题", "多选题", "填空题"):
        assert get_skill_for_type(type_) is ObjectiveSkill


# ─── Deterministic score derivation ─────────────────────────────────────────


def test_binary_types_are_strictly_full_or_zero():
    assert _objective_score("选择题", correct=True, model_score=0.0,
                            partial_credit_applicable=False, max_score=5.0) == 5.0
    assert _objective_score("选择题", correct=False, model_score=4.0,
                            partial_credit_applicable=False, max_score=5.0) == 0.0
    # A model proposing process-style partial credit cannot leak through.
    assert _objective_score("填空题", correct=False, model_score=3.0,
                            partial_credit_applicable=True, max_score=5.0) == 0.0


def test_multi_choice_partial_credit_requires_explicit_flag():
    # Rubric states partial credit and the model confirms it applies.
    assert _objective_score("多选题", correct=False, model_score=2.0,
                            partial_credit_applicable=True, max_score=6.0) == 2.0
    # Flag missing / false → fail-safe: no partial credit.
    assert _objective_score("多选题", correct=False, model_score=2.0,
                            partial_credit_applicable=False, max_score=6.0) == 0.0
    # Model score is clamped to the question scale.
    assert _objective_score("多选题", correct=False, model_score=99.0,
                            partial_credit_applicable=True, max_score=6.0) == 6.0
    # Full correctness always wins, whatever the model proposed.
    assert _objective_score("多选题", correct=True, model_score=1.0,
                            partial_credit_applicable=False, max_score=6.0) == 6.0


def test_process_criterion_detection():
    # Default: no process points unless the rubric explicitly marks them.
    assert _is_result_based_criterion("")
    assert _is_result_based_criterion("答案唯一：答对满分，答错 0 分。")
    assert _is_result_based_criterion("全部选对的得6分，部分选对的得部分分，有选错的得0分。")
    assert not _is_result_based_criterion("过程2分，答案3分。")
    assert not _is_result_based_criterion("按步骤给分，每步2分。")


# ─── Skill behaviour ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_choice_correct_answer_gets_full_score_no_steps(monkeypatch):
    _patch_llm(monkeypatch, ObjectiveGradingOutput(
        student_answer="D", reference_answer="D", correct=True,
        score=5.0, confidence=0.95, comment="答案正确。",
    ))
    skill = ObjectiveSkill(_fake_provider(), language="zh")
    result = await skill.grade(_problem("选择题"), _answer("选D"), student_id="S1")

    assert result.score == 5.0
    assert result.max_score == 5.0
    assert result.steps == []  # 不输出过程分项
    assert result.confidence == 0.95


@pytest.mark.asyncio
async def test_choice_wrong_answer_gets_zero_no_steps(monkeypatch):
    # The model's own proposed partial score (2.5) must not leak through.
    _patch_llm(monkeypatch, ObjectiveGradingOutput(
        student_answer="A", reference_answer="D", correct=False,
        score=2.5, confidence=0.9, comment="答案错误，正确答案为 D。",
    ))
    skill = ObjectiveSkill(_fake_provider(), language="zh")
    result = await skill.grade(_problem("选择题"), _answer("A"), student_id="S1")

    assert result.score == 0.0
    assert result.steps == []


@pytest.mark.asyncio
async def test_blank_equivalent_answer_gets_full_score(monkeypatch):
    _patch_llm(monkeypatch, ObjectiveGradingOutput(
        student_answer=r"\frac{\pi}{3}", reference_answer="60°", correct=True,
        score=5.0, confidence=0.9, comment="等价，正确。",
    ))
    skill = ObjectiveSkill(_fake_provider(), language="zh")
    result = await skill.grade(_problem("填空题"), _answer(r"$\frac{\pi}{3}$"), student_id="S1")

    assert result.score == 5.0
    assert result.steps == []


@pytest.mark.asyncio
async def test_multi_choice_rubric_partial_credit_kept(monkeypatch):
    criterion = "全部选对的得6分，部分选对的得部分分，有选错的得0分。"
    _patch_llm(monkeypatch, ObjectiveGradingOutput(
        student_answer="AB", reference_answer="ABC", correct=False,
        partial_credit_applicable=True, score=4.0, confidence=0.85,
        comment="漏选 C，按部分给分。",
    ))
    skill = ObjectiveSkill(_fake_provider(), language="zh")
    result = await skill.grade(
        _problem("多选题", criterion=criterion, max_score=6.0),
        _answer("AB"), student_id="S1",
    )
    assert result.score == 4.0
    assert result.steps == []


@pytest.mark.asyncio
async def test_multi_choice_no_partial_credit_rule_is_binary(monkeypatch):
    # Rubric silent on partial credit: model must not invent any — and the
    # deterministic policy zeroes a proposal it did not confirm.
    _patch_llm(monkeypatch, ObjectiveGradingOutput(
        student_answer="AB", reference_answer="ABC", correct=False,
        partial_credit_applicable=False, score=3.0, confidence=0.8,
        comment="漏选 C，得 0 分。",
    ))
    skill = ObjectiveSkill(_fake_provider(), language="zh")
    result = await skill.grade(_problem("多选题"), _answer("AB"), student_id="S1")
    assert result.score == 0.0


@pytest.mark.asyncio
async def test_explicit_process_criterion_honors_rubric_score(monkeypatch):
    # Rubric explicitly allocates process points: the model's rubric-faithful
    # score (2 process points for a complete derivation, wrong final answer)
    # is honored as written — not flattened to the binary policy.
    criterion = "写出简要过程（2分），答案（3分）。"
    _patch_llm(monkeypatch, ObjectiveGradingOutput(
        student_answer="B", reference_answer="D", correct=False,
        score=2.0, confidence=0.9,
        comment="推导过程完整，得过程2分；最终答案错误，答案0分。",
    ))
    skill = ObjectiveSkill(_fake_provider(), language="zh")
    result = await skill.grade(
        _problem("选择题", criterion=criterion, max_score=5.0),
        _answer("B"), student_id="S1",
    )
    assert result.score == 2.0
    assert result.steps == []  # 仍不输出过程分项，只在总分里体现


@pytest.mark.asyncio
async def test_blank_answer_skips_llm_and_scores_zero(monkeypatch):
    llm = AsyncMock()
    monkeypatch.setattr(objective, "structured_llm_call", llm)
    skill = ObjectiveSkill(_fake_provider(), language="zh")
    result = await skill.grade(_problem("选择题"), _answer("   "), student_id="S1")

    llm.assert_not_awaited()
    assert result.score == 0.0
    assert result.confidence == 1.0
    assert result.steps == []


@pytest.mark.asyncio
async def test_llm_failure_yields_blank_result_with_error_kind(monkeypatch):
    monkeypatch.setattr(
        objective, "structured_llm_call",
        AsyncMock(side_effect=RuntimeError("provider_timeout")),
    )
    skill = ObjectiveSkill(_fake_provider(), language="zh")
    result = await skill.grade(_problem("选择题"), _answer("A"), student_id="S1")

    assert result.confidence == 0.0
    assert result.score == 0.0
    assert result.error_kind == "transient_llm"  # provider_timeout → 网络/超时


@pytest.mark.asyncio
async def test_unreachable_error_classified_transient():
    from backend.skills.base import classify_skill_error

    kind, _ = classify_skill_error(RuntimeError("provider_unreachable"))
    assert kind == "transient_llm"
