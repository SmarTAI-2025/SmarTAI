from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.agents.multi_expert import AllExpertsFailed, _synthesize, _weighted_average_fallback, run_multi_expert
from backend.models import ExpertResult, ProblemInfo, StudentAnswerInfo, TaskGradingSetup
from backend.services.grading_scores import round_ai_score


def expert(provider, score, confidence=0, error_kind=None):
    return ExpertResult(provider=provider, score=score, max_score=10, confidence=confidence,
                        error_kind=error_kind, comment=f"Explanation from {provider}")


def problem():
    return ProblemInfo(q_id="q1", number="1", type="proof", stem="Synthetic proof", criterion="Reasoning", max_score=10)


def setup(weights=None):
    return TaskGradingSetup(selected_provider_ids=["a", "b", "failed"], primary_provider_id="a",
                            aggregation_method="weighted_average", expert_weights=weights or {})


@pytest.mark.parametrize("weights,expected", [(None, 8.5), ({"a": 1, "b": 2, "failed": 100}, 9.0)])
def test_valid_votes_only_and_one_decimal(weights, expected):
    votes = [expert("a", 7, .95), expert("b", 10, .99), expert("failed", 0, .99, "parse_failed")]
    result = _weighted_average_fallback(problem(), votes, grading_setup=setup(weights))
    assert result.score == expected
    assert len(result.expert_results) == 3
    assert "Explanation from failed" not in result.comment


def test_no_confidence_uses_equal_weights_including_real_zero():
    result = _weighted_average_fallback(problem(), [expert("a", 0), expert("b", 9)])
    assert result.score == 4.5
    assert result.confidence == 0
    with pytest.raises(AllExpertsFailed):
        _weighted_average_fallback(problem(), [expert("failed", 0, .99, "general")])


@pytest.mark.parametrize("confidence", [None, "unavailable", float("nan"), float("inf"), -1, 2])
def test_invalid_confidence_does_not_erase_a_valid_score(confidence):
    valid = expert("a", 7, confidence)
    assert valid.confidence == 0
    assert _weighted_average_fallback(problem(), [valid, expert("b", 10)]).score == 8.5
    without_confidence = ExpertResult(provider="c", score=0, max_score=10, comment="Valid zero")
    assert without_confidence.confidence == 0


@pytest.mark.parametrize("weights", [{"a": 1}, {"a": 1, "b": 1, "other": 1}, {"a": 0, "b": 1, "failed": 1}, {"a": -1, "b": 1, "failed": 1}, {"a": float("nan"), "b": 1, "failed": 1}])
def test_custom_weights_require_complete_positive_finite_ratios(weights):
    with pytest.raises(ValidationError):
        setup(weights)


def test_weight_snapshot_roundtrip_and_round_half_up():
    configured = setup({"a": 1, "b": 2, "failed": 3})
    assert TaskGradingSetup.model_validate_json(configured.model_dump_json()).expert_weights == configured.expert_weights
    assert round_ai_score(8.55, 10) == 8.6
    assert round_ai_score(None, 10) is None


@pytest.mark.asyncio
async def test_judge_cannot_replace_configured_average(monkeypatch):
    async def judge(*args, **kwargs):
        return SimpleNamespace(score=1, max_score=10, confidence=.9, comment="Judge explanation", steps=[]), None
    monkeypatch.setattr("backend.agents.multi_expert.structured_llm_call", judge)
    result = await _synthesize(problem(), [expert("a", 7, .95), expert("b", 10, .99)], SimpleNamespace(), grading_setup=setup({"a": 1, "b": 2, "failed": 100}))
    assert result.score == 9
    assert result.comment == "Judge explanation"


@pytest.mark.asyncio
async def test_no_confidence_survives_fanout_and_persistence_adapter(monkeypatch):
    from backend.domain.education import QuestionDTO
    from backend.services.grading_adapter import correction_to_result
    class Skill:
        name = "Synthetic"
        def __init__(self, provider, **kwargs): self.provider = provider
        async def grade(self, *args, **kwargs): return expert(self.provider.provider_id, 0 if self.provider.provider_id == "a" else 9)
    monkeypatch.setattr("backend.agents.multi_expert.get_skill_for_type", lambda _: Skill)
    result = await run_multi_expert(problem=problem(), answer=StudentAnswerInfo(q_id="q1", content="Answer", number="1", type="proof"), student_id="s1",
        registry=SimpleNamespace(list_available=lambda: [SimpleNamespace(provider_id="a"), SimpleNamespace(provider_id="b")]), aggregation_method="weighted_average")
    assert result.score == 4.5
    question = QuestionDTO(id="q", assignment_id="task", q_id="q1", number="1", type="proof", stem="Synthetic", criterion="Rubric", max_score=10, order_index=0, created_at=0, updated_at=0)
    stored = correction_to_result(run_id="run", revision_id="rev", question=question, student_id="s1", correction=result)
    assert stored.ai_score == 4.5
    assert stored.result_status != "failed"


@pytest.mark.asyncio
async def test_custom_weights_use_saved_ids_even_for_identical_model_names(monkeypatch):
    providers = {"saved-a": SimpleNamespace(provider_id="openai:same-model", score=4),
                 "saved-b": SimpleNamespace(provider_id="openai:same-model", score=8)}
    class Skill:
        name = "Synthetic"
        def __init__(self, provider, **kwargs): self.provider = provider
        async def grade(self, *args, **kwargs): return expert(self.provider.provider_id, self.provider.score, .9)
    monkeypatch.setattr("backend.agents.multi_expert.get_skill_for_type", lambda _: Skill)
    configured = TaskGradingSetup(selected_provider_ids=list(providers), primary_provider_id="saved-a",
        aggregation_method="weighted_average", expert_weights={"saved-a": 1, "saved-b": 3})
    result = await run_multi_expert(problem=problem(), answer=StudentAnswerInfo(q_id="q1", content="Answer", number="1", type="proof"), student_id="s1",
        registry=SimpleNamespace(get=providers.get, list_available=lambda: list(reversed(providers.values()))),
        aggregation_method="weighted_average", grading_setup=configured)
    assert result.score == 7
    assert [item.provider_id for item in result.expert_results] == ["saved-a", "saved-b"]
