from types import SimpleNamespace

import pytest

from backend.agents.grading_agent import _apply_low_confidence_policy
from backend.domain.confidence_policy import confidence_needs_attention, confidence_requires_review
from backend.models import Correction, TaskGradingSetup
from backend.services.question_sources import recognition_requires_confirmation
from backend.services.task_facade import _correction_review_blocks_finalization


@pytest.mark.parametrize("value,advice,required", [
    (0, True, False), (.2499, True, False), (.25, True, False),
    (.5999, True, False), (.60, False, False), (1, False, False),
    (None, False, False), ("low", False, False), (True, False, False),
    (float("nan"), False, False), (-1, False, False), (2, False, False), (20, False, False), (60, False, False),
])
def test_shared_boundaries(value, advice, required):
    from backend.analytics.ask_workspace import confidence
    from backend.domain.confidence_policy import confidence_value
    assert confidence(value) == confidence_value(value)
    assert confidence_needs_attention(value) is advice
    assert confidence_requires_review(value) is required
    result = SimpleNamespace(result_status="graded", effective_score=8, ai_score=8,
                             ai_max_score=10, ai_confidence=value, teacher_review=None)
    assert _correction_review_blocks_finalization(result) is required
    result.teacher_review = {"confirmed": True, "new_score": 8}
    assert _correction_review_blocks_finalization(result) is False


@pytest.mark.parametrize("confidence,required", [(.2499, False), (.25, False), (.5999, False), (.60, False)])
def test_grading_policy_does_not_make_advice_mandatory(confidence, required):
    correction = Correction(q_id="q", type="proof", score=8, max_score=10, confidence=confidence,
                            comment="Evidence", steps=[], requires_human_review=True, review_reasons=["low_confidence"])
    actual = _apply_low_confidence_policy(correction, None)
    assert actual.requires_human_review is required
    assert ("low_confidence" in actual.review_reasons) is (confidence < .60)


def test_high_confidence_does_not_override_real_failures():
    for coverage in ({"failed_pages": [2]}, {"unprocessed_pages": [2]}, {"missing_targets": ["Q2"]}):
        assert recognition_requires_confirmation({"confidence": .99, "coverage": coverage})
    assert not recognition_requires_confirmation({"confidence": "low", "coverage": {}})
    assert not recognition_requires_confirmation({"confidence": .25, "coverage": {}})
    assert not recognition_requires_confirmation({"confidence": .2499, "coverage": {}})


def test_legacy_slider_uses_current_fixed_policy():
    assert TaskGradingSetup(selected_provider_ids=["model"], primary_provider_id="model", low_confidence_threshold=.8).low_confidence_threshold == .6


@pytest.mark.parametrize("score", [0, 8])
@pytest.mark.parametrize("low_confidence", [0, .2, .5999])
def test_any_valid_expert_confidence_is_advisory_and_confirmation_clears_it(score, low_confidence):
    from backend.domain.grading_review import review_state
    from backend.models import ExpertResult
    correction = Correction(q_id="q", type="proof", score=score, max_score=10, confidence=.95,
        comment="Saved", steps=[], expert_results=[
            ExpertResult(provider="strong", score=score, confidence=.95, comment="A"),
            ExpertResult(provider="uncertain", score=score, confidence=low_confidence, comment="B"),
            ExpertResult(provider="failed", score=0, confidence=0, error_kind="quota_exhausted", comment="C")])
    actual = _apply_low_confidence_policy(correction, None)
    assert "low_confidence" in actual.review_reasons
    assert review_state(actual.model_dump())[0]
    assert not review_state({**actual.model_dump(), "review_status": "confirmed", "teacher_score": score})[0]
    result = SimpleNamespace(result_status="needs_review", effective_score=score, ai_score=score,
                             ai_max_score=10, ai_confidence=low_confidence, teacher_review=None,
                             initial_review_reasons=["grading_paused", "partial_experts"])
    assert not _correction_review_blocks_finalization(result)


def test_failed_placeholder_confidence_never_creates_low_confidence_advice():
    from backend.models import ExpertResult
    correction = Correction(q_id="q", type="proof", score=8, max_score=10, confidence=.6,
        comment="Saved", steps=[], expert_results=[
            ExpertResult(provider="ok", score=8, confidence=.6, comment="A"),
            ExpertResult(provider="failed", score=0, confidence=0, error_kind="transient_llm", comment="B")])
    assert "low_confidence" not in _apply_low_confidence_policy(correction, None).review_reasons
