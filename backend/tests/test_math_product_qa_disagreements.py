"""Residual scoring disputes retain evidence without blocking valid grades."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents import multi_expert as multi
from backend.domain.grading_review import review_state
from backend.services.math_grading_context import math_plans_context
from backend.skills import math_grading as grading
from backend.tests.test_math_grading import answer, expert_for, plan_for, problem


@pytest.mark.asyncio
@pytest.mark.parametrize("review_fails", [False, True])
async def test_unresolved_holistic_scores_remain_advisory(monkeypatch, review_fails):
    p, a, plan = problem(), answer(), plan_for(mode="holistic")
    experts = [expert_for(grading.MathAssessment(
        route_id="main", route_reason="Overall teacher criterion applies.", score=score,
        claims=[grading.Claim(id="c", quote=a.content, verdict="refuted", reason="Final product is wrong.")],
        rationale="The method is valid but the final product is wrong.",
        confidence=.95, comment="Final arithmetic error."), plan, a.content)
        for score in (8, 9)]
    call = AsyncMock(side_effect=RuntimeError("optional review unavailable")) if review_fails else AsyncMock(
        return_value=(grading.ExpertReview(revisions=[]), SimpleNamespace(duration_ms=1)))
    monkeypatch.setattr(grading, "structured_llm_call", call)
    with math_plans_context({"q1": plan}), grading.math_review_scope():
        reviewed = await grading.review_math_experts(p, a, experts, SimpleNamespace(provider_id="mock"))
    correction = multi._weighted_average_fallback(p, reviewed)
    assert call.await_count == 1
    assert correction.score == sum(step.score for step in correction.steps) == 8.5
    assert [expert.score for expert in correction.expert_results] == [8, 9]
    assert correction.review_reasons == ["math_scoring_uncertainty"]
    # This disagreement is below the old variance threshold, yet it remains
    # visible as advice and keeps a real score usable by the existing result UI.
    assert multi._compute_review_signal(reviewed)[1] is False
    assert review_state(correction.model_dump()) == (True, False)


def _consequential_draft(prefix, origin):
    return grading.MathAssessment(
        route_id="main", route_reason="Deduct for independent errors.",
        claims=[
            grading.Claim(id=f"{prefix}-a", quote="a=2+2=5", verdict="refuted", reason="First error."),
            grading.Claim(id=f"{prefix}-b", quote="b=3+3=7", verdict="refuted", reason="Second error."),
            grading.Claim(id=f"{prefix}-later", quote="a+b=12",
                          depends_on=[f"{prefix}-a", f"{prefix}-b"], verdict="refuted",
                          reason="Affected by an earlier value."),
        ],
        deductions=[grading.Deduction(error_id=f"{prefix}-error", rule_id="error",
            claim_id=f"{prefix}-later", caused_by=f"{prefix}-{origin}" if origin else None,
            reason="Apply the teacher's rule to the earlier error's effect.")],
        confidence=.95, comment="One deduction under the error rule.",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("second_origin", ["b", None])
async def test_same_score_different_error_origin_gets_one_review(monkeypatch, second_origin):
    p, a, plan = problem(), answer("a=2+2=5; b=3+3=7; a+b=12"), plan_for(mode="deductions")
    experts = [expert_for(_consequential_draft(prefix, origin), plan, a.content)
               for prefix, origin in (("one", "a"), ("two", second_origin))]
    call = AsyncMock(return_value=(grading.ExpertReview(revisions=[]), SimpleNamespace(duration_ms=1)))
    monkeypatch.setattr(grading, "structured_llm_call", call)
    with math_plans_context({"q1": plan}), grading.math_review_scope():
        reviewed = await grading.review_math_experts(p, a, experts, SimpleNamespace(provider_id="mock"))
        # A residual disagreement cannot start another model discussion.
        reviewed = await grading.review_math_experts(p, a, reviewed, SimpleNamespace(provider_id="mock"))
    correction = multi._weighted_average_fallback(p, reviewed)
    assert call.await_count == 1
    assert correction.score == sum(step.score for step in correction.steps) == 9
    assert correction.review_reasons == ["math_scoring_uncertainty"]


@pytest.mark.asyncio
async def test_same_error_origin_with_different_local_ids_needs_no_review(monkeypatch):
    p, a, plan = problem(), answer("a=2+2=5; b=3+3=7; a+b=12"), plan_for(mode="deductions")
    experts = [expert_for(_consequential_draft(prefix, "a"), plan, a.content) for prefix in ("one", "two")]
    call = AsyncMock()
    monkeypatch.setattr(grading, "structured_llm_call", call)
    with math_plans_context({"q1": plan}), grading.math_review_scope():
        reviewed = await grading.review_math_experts(p, a, experts, SimpleNamespace(provider_id="mock"))
    correction = multi._weighted_average_fallback(p, reviewed)
    call.assert_not_called()
    assert correction.score == 9 and correction.review_reasons == []
