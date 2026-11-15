"""Exercise real parsing and multi-model orchestration with fixed model replies."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents.grading_agent import _apply_low_confidence_policy, _grade_single_answer
from backend.progress.tracker import ProgressReporter
from backend.models import ProblemInfo, StudentAnswerInfo
from backend.services.math_grading_context import math_plans_context
from backend.services.rubric_drafts import RubricDefinition, bind_draft, generated_rubric
from backend.skills import math_grading as grading
from backend.skills.calculation import CalculationSkill
from backend.tests.test_frozen_part_rubric import fixture, proposed_rates, question_row
from backend.tests.test_math_evidence_reliability import raw_provider


def frozen_case():
    raw = proposed_rates(fixture()["questions"][2])
    row = question_row(raw)
    _, draft = generated_rubric(row, RubricDefinition(part_rubric=raw))
    problem = ProblemInfo(**row)
    problem._math_rubric_source = {"presentation": {"question_structure": row["question_structure"]}}
    plan = bind_draft(draft, problem)
    answer = StudentAnswerInfo(q_id=problem.q_id, number=problem.number, type=problem.type, content="step A\nstep B")
    payload = {"part_decisions": [dict(part_id=part.id, target_quote="part:" + part.id,
        student_quote="student:1", relevance="complete", method="reference",
        progress="The cited statement establishes the goal.") for part in plan.part_rubric.parts],
        "comment": "Saved grading judgment"}
    return problem, answer, plan, payload


@pytest.mark.asyncio
@pytest.mark.parametrize("concern", ["none", "requested", "relevance", "disagreement"])
async def test_only_concrete_concerns_trigger_joint_review(monkeypatch, concern):
    monkeypatch.setattr(grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    p, a, plan, first = frozen_case()
    second = json.loads(json.dumps(first))
    if concern == "requested":
        first["needs_review"] = True
    elif concern == "relevance":
        first["part_decisions"][0].update(relevance="uncertain", method="undetermined")
        second = json.loads(json.dumps(first))
    elif concern == "disagreement":
        second["part_decisions"][0]["relevance"] = "incorrect"
    primary, first_calls = raw_provider(json.dumps(first), '{"revisions": []}')
    sibling, second_calls = raw_provider(json.dumps(second))
    primary.provider_id, sibling.provider_id = "first", "second"
    registry = SimpleNamespace(list_available=lambda: [primary, sibling], pick_default=lambda: primary)
    reporter = ProgressReporter("review-trigger", 1, 1)
    with math_plans_context({p.q_id: plan}):
        result = await _grade_single_answer(problem=p, answer=a, student_id="synthetic", registry=registry,
                                           reporter=reporter, aggregation_method="weighted_average")
    assert not any(expert.error_kind for expert in result.expert_results)
    assert len(first_calls) + len(second_calls) == (2 if concern == "none" else 3)
    # A waived AI check still finishes the same one response; no denominator change.
    progress = await reporter.snapshot()
    assert (progress.completed_units, progress.total_questions) == (1, 1)
    # Omitted confidence survives the actual structured parser, review and merge.
    assert result.confidence is None
    assert all(expert.confidence is None for expert in result.expert_results)
    assert "low_confidence" not in _apply_low_confidence_policy(result, None).review_reasons
    if concern == "none":
        assert result.score == p.max_score
        assert result.review_reasons == ["confidence_unavailable"]
        assert all(not grading.read_math_trace(e)["review_pending"] for e in result.expert_results)


@pytest.mark.asyncio
@pytest.mark.parametrize("needs_review,expected_calls", [(False, 1), (True, 2)])
async def test_single_model_keeps_requested_review_but_skips_blanket_second_call(monkeypatch, needs_review, expected_calls):
    monkeypatch.setattr(grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    p, a, plan, payload = frozen_case()
    payload["needs_review"] = needs_review
    reviewed = {**payload, "needs_review": False}
    provider, calls = raw_provider(json.dumps(payload), json.dumps(reviewed))
    with math_plans_context({p.q_id: plan}):
        result = await CalculationSkill(provider).grade(p, a)
    assert result.score == p.max_score and result.error_kind is None
    assert len(calls) == expected_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,expected", [(None, None), ("95%", None), (95, None), (True, None),
                                         (.82, .82), ("0.82", .82), (0, 0)])
async def test_confidence_metadata_never_discards_or_repeats_valid_initial_grading(monkeypatch, raw, expected):
    monkeypatch.setattr(grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    p, a, plan, payload = frozen_case()
    payload["confidence"] = raw
    provider, calls = raw_provider(json.dumps(payload))
    with math_plans_context({p.q_id: plan}):
        result = await CalculationSkill(provider).grade(p, a)
    assert len(calls) == 1 and result.error_kind is None
    assert result.score == p.max_score and result.confidence == expected
    assert grading.read_math_trace(result)["assessment"]["confidence"] == expected
    assert '"confidence"' in calls[0][0].content
    assert "not a percentage, string, boolean or omitted field" in calls[0][0].content
