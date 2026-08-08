from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.agents.grading_agent import _grade_single_answer
from backend.agents.multi_expert import AllExpertsFailed, run_multi_expert
from backend.models import ExpertResult, ProblemInfo, StepScore, StudentAnswerInfo
from backend.skills.base import InvalidScoreScale, normalize_score_scale
from backend.skills.concept import ConceptSkill
from backend.skills.proof import ProofSkill


def _problem(*, type_: str = "概念题", max_score: float = 5.0) -> ProblemInfo:
    return ProblemInfo(
        q_id="q1",
        number="1",
        type=type_,
        stem="Explain why the result follows.",
        criterion="Reasoning 60%; conclusion 40%.",
        max_score=max_score,
    )


def _answer(*, type_: str = "概念题") -> StudentAnswerInfo:
    return StudentAnswerInfo(
        q_id="q1", number="1", type=type_, content="A valid explanation."
    )


def _provider(provider_id: str = "mock:test"):
    provider = MagicMock()
    provider.provider_id = provider_id
    provider.provider_type = "mock"
    return provider


def test_normalize_score_scale_rescales_total_and_steps():
    score, steps = normalize_score_scale(
        score=8.0,
        reported_max_score=10.0,
        authoritative_max_score=5.0,
        steps=[StepScore(step_no=1, desc="reasoning", is_correct=True, score=6.0)],
    )
    assert score == 4.0
    assert steps[0].score == 3.0


def test_normalize_ten_point_result_to_twenty_point_question():
    score, steps = normalize_score_scale(
        score=10.0,
        reported_max_score=10.0,
        authoritative_max_score=20.0,
        steps=[StepScore(step_no=1, desc="complete", is_correct=True, score=10.0)],
    )
    assert score == 20.0
    assert steps[0].score == 20.0


def test_normalize_clamps_malicious_score_and_rejects_invalid_scale():
    score, _steps = normalize_score_scale(
        score=10_000.0,
        reported_max_score=10.0,
        authoritative_max_score=5.0,
    )
    assert score == 5.0
    with pytest.raises(InvalidScoreScale, match="reported_max_score_invalid"):
        normalize_score_scale(
            score=1.0,
            reported_max_score=0.0,
            authoritative_max_score=5.0,
        )


@pytest.mark.asyncio
async def test_concept_skill_uses_authoritative_question_max(monkeypatch):
    captured: dict[str, str] = {}

    async def retrieve(*_args, **_kwargs):
        return []

    async def structured_call(*_args, **kwargs):
        captured["system_prompt"] = kwargs["system_prompt"]
        return (
            SimpleNamespace(
                score=8.0,
                max_score=10.0,
                confidence=0.9,
                comment="ok",
                steps=[{
                    "step_no": 1,
                    "desc": "reasoning",
                    "is_correct": True,
                    "score": 6.0,
                }],
                hits=[],
            ),
            SimpleNamespace(content="{}", duration_ms=1.0),
        )

    monkeypatch.setattr("backend.skills.concept.kb_tool.retrieve", retrieve)
    monkeypatch.setattr("backend.skills.concept.structured_llm_call", structured_call)

    result = await ConceptSkill(_provider()).grade(_problem(), _answer(), student_id="s1")

    assert result.score == 4.0
    assert result.max_score == 5.0
    assert result.steps[0].score == 3.0
    assert "authoritative maximum score for this question is 5" in captured["system_prompt"]


@pytest.mark.asyncio
async def test_proof_skill_uses_authoritative_question_max(monkeypatch):
    captured: dict[str, str] = {}

    async def retrieve(*_args, **_kwargs):
        return []

    async def structured_call(*_args, **kwargs):
        captured["system_prompt"] = kwargs["system_prompt"]
        return (
            SimpleNamespace(
                score=16.0,
                max_score=20.0,
                confidence=0.9,
                comment="ok",
                steps=[{
                    "step_no": 1,
                    "desc": "proof",
                    "is_correct": True,
                    "score": 12.0,
                }],
            ),
            SimpleNamespace(content="{}", duration_ms=1.0),
        )

    monkeypatch.setattr("backend.skills.proof.kb_tool.retrieve", retrieve)
    monkeypatch.setattr("backend.skills.proof.structured_llm_call", structured_call)

    result = await ProofSkill(_provider()).grade(
        _problem(type_="证明题"), _answer(type_="证明题"), student_id="s1"
    )

    assert result.score == 4.0
    assert result.max_score == 5.0
    assert result.steps[0].score == 3.0
    assert "authoritative maximum score for this question is 5" in captured["system_prompt"]


@pytest.mark.asyncio
async def test_multi_expert_defensively_normalizes_custom_skill(monkeypatch):
    class Provider:
        provider_id = "mock:test"
        provider_type = "mock"

    class Registry:
        def list_available(self):
            return [Provider()]

    class Skill:
        name = "CustomSkill"

        def __init__(self, provider, **_kwargs):
            self.provider = provider

        async def grade(self, problem, answer, *, student_id=""):
            return ExpertResult(
                provider=self.provider.provider_id,
                score=8.0,
                max_score=10.0,
                confidence=0.9,
                comment="ok",
                steps=[StepScore(step_no=1, desc="step", is_correct=True, score=6.0)],
            )

    monkeypatch.setattr("backend.agents.multi_expert.get_skill_for_type", lambda _type: Skill)

    correction = await run_multi_expert(
        problem=_problem(),
        answer=_answer(),
        student_id="s1",
        registry=Registry(),
    )

    assert correction.score == 4.0
    assert correction.max_score == 5.0
    assert correction.steps[0].score == 3.0
    assert correction.expert_results[0].max_score == 5.0


@pytest.mark.asyncio
async def test_grading_failure_keeps_authoritative_max(monkeypatch):
    failure = ExpertResult(
        provider="mock:test",
        score=0.0,
        max_score=10.0,
        confidence=0.0,
        comment="failed",
        error_kind="general",
    )

    async def fail_all(**_kwargs):
        raise AllExpertsFailed([failure])

    monkeypatch.setattr("backend.agents.grading_agent.run_multi_expert", fail_all)

    correction = await _grade_single_answer(
        problem=_problem(),
        answer=_answer(),
        student_id="s1",
        registry=MagicMock(),
    )

    assert correction.score == 0.0
    assert correction.max_score == 5.0
    assert correction.confidence == 0.0
    assert correction.synthesis_method == "all_failed"
