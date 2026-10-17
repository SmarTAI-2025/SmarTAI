from types import SimpleNamespace

import pytest

from backend.agents.grading_agent import _apply_low_confidence_policy
from backend.domain.confidence_policy import confidence_needs_attention, confidence_requires_review
from backend.models import Correction, TaskGradingSetup
from backend.services.question_sources import recognition_requires_confirmation
from backend.services.task_facade import _correction_review_blocks_finalization


@pytest.mark.parametrize("value,advice,required", [
    (0, True, True), (.2499, True, True), (.25, True, False),
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


@pytest.mark.parametrize("confidence,required", [(.2499, True), (.25, False), (.5999, False), (.60, False)])
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
    assert recognition_requires_confirmation({"confidence": .2499, "coverage": {}})


def test_legacy_slider_uses_current_fixed_policy():
    assert TaskGradingSetup(selected_provider_ids=["model"], primary_provider_id="model", low_confidence_threshold=.8).low_confidence_threshold == .6
