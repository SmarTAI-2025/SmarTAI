"""Review reason projection must not alter grading or create model calls."""
import json
from unittest.mock import AsyncMock

import pytest

from backend.agents.multi_expert import _apply_math_review_advisory, _weighted_average_fallback
from backend.domain.grading_review import review_state
from backend.skills import math_grading as grading
from backend.tests.test_math_grading import answer, assessment, expert_for, plan_for, problem


def expert(*, needs_review=False, **trace_flags):
    value = expert_for(assessment(needs_review=needs_review), plan_for(), answer().content)
    trace = grading.read_math_trace(value)
    trace.update(trace_flags)
    return value.model_copy(update={"raw_output": json.dumps(trace), "confidence": .95})


@pytest.mark.parametrize("flags,reason", [
    ({"needs_review": True}, "math_model_review"),
    ({"review_advisory": True, "review_pending": True}, "math_verification_conflict"),
    ({"review_advisory": True, "proof_audit": {"review_concerns": [{"reason": "Missing premise"}]}}, "math_proof_concern"),
    ({"review_advisory": True}, "math_scoring_uncertainty"),
])
def test_classified_advisory_preserves_score_confidence_and_confirmation(monkeypatch, flags, reason):
    call = AsyncMock()
    monkeypatch.setattr(grading, "structured_llm_call", call)
    value = _weighted_average_fallback(problem(), [expert(**flags)])
    before = value.model_dump(exclude={"requires_human_review", "review_reasons"})
    _apply_math_review_advisory(value)
    assert value.review_reasons == [reason]
    assert value.score == 8 and value.confidence == .95
    assert value.model_dump(exclude={"requires_human_review", "review_reasons"}) == before
    assert review_state(value.model_dump()) == (True, False)
    assert review_state({**value.model_dump(), "teacher_score": 8, "review_status": "confirmed"}) == (False, True)
    call.assert_not_called()


def test_all_specific_reasons_survive_and_model_request_is_only_fallback():
    first = expert(needs_review=True, review_advisory=True, review_pending=True,
                   proof_audit={"review_concerns": [{"reason": "Missing premise"}]})
    second = expert_for(assessment(6, 2), plan_for(), answer().content)
    correction = _weighted_average_fallback(problem(), [first, first.model_copy(deep=True), second])
    _apply_math_review_advisory(correction)
    assert correction.review_reasons == ["math_expert_disagreement", "math_verification_conflict", "math_proof_concern"]
    assert correction.score == 8


def test_trace_details_without_active_advisory_do_not_create_new_trigger():
    value = _weighted_average_fallback(problem(), [expert(review_pending=True,
        proof_audit={"review_concerns": [{"reason": "Missing premise"}]})])
    assert value.review_reasons == []
    assert review_state(value.model_dump()) == (False, False)


@pytest.mark.parametrize("advisory", [False, True])
@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("proof", [False, True])
@pytest.mark.parametrize("requested", [False, True])
@pytest.mark.parametrize("disagreement", [False, True])
def test_reason_classification_preserves_original_trigger(advisory, pending, proof, requested, disagreement):
    from backend.models import Correction

    first = expert(needs_review=requested, review_advisory=advisory, review_pending=pending,
                   proof_audit={"review_concerns": [{"reason": "Missing premise"}] if proof else []})
    experts = [first]
    if disagreement:
        experts.append(expert_for(assessment(6, 2), plan_for(), answer().content))
    value = Correction(q_id="q1", type="calculation", score=8, max_score=10,
        confidence=.95, steps=first.steps, comment="Original", expert_results=experts)
    before = value.model_dump(exclude={"requires_human_review", "review_reasons"})
    old_trigger = disagreement or advisory or requested
    _apply_math_review_advisory(value)
    assert value.requires_human_review is old_trigger
    assert bool(value.review_reasons) is old_trigger
    assert value.model_dump(exclude={"requires_human_review", "review_reasons"}) == before
