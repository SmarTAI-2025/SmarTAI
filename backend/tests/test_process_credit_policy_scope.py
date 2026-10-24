"""Negation and conditional scope regressions at PR262 head 0685801d."""
from unittest.mock import AsyncMock

import pytest

from backend.models import StudentAnswerInfo
from backend.services.process_credit_policy import process_credit_policy
from backend.services.math_rubric_plan import prepare_math_rubric_plan
from backend.skills import objective
from backend.skills.objective import ObjectiveSkill
from backend.tests.test_process_credit_policy_acceptance import (
    RawReplayProvider, objective_response, plan_item, plan_response, question,
)


@pytest.fixture(autouse=True)
def no_external_knowledge(monkeypatch):
    monkeypatch.setattr(objective.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))


@pytest.mark.asyncio
@pytest.mark.parametrize("criterion", [
    "不计过程分（只看最终答案）",
    "评分时不计过程分",
    "只有答案正确才给过程2分",
])
@pytest.mark.parametrize("kind", ["选择题", "填空题"])
async def test_explicit_restriction_cannot_be_bypassed_by_an_unresolved_parse(criterion, kind):
    p = question(criterion, kind)
    provider = RawReplayProvider(objective_response())
    result = await ObjectiveSkill(provider).grade(
        p, StudentAnswerInfo(q_id="q1", number="1", type=kind, content="3"))
    assert not result.error_kind
    assert result.score == 0
    assert p.criterion == criterion
    assert criterion in provider.messages[0][1].content
    assert len(provider.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("criterion", [
    "不计过程分(只看最终答案)",
    "本题评分时不计过程分",
    "评分时：不计过程分（只看最终答案）",
    "只有答案正确，才给过程2分",
    "仅当最终答案正确时才给计算过程2分",
    "若答案正确则给过程2分",
])
@pytest.mark.parametrize("correct", [False, True])
async def test_presentation_and_conditional_connectors_preserve_scope(criterion, correct):
    conditional = "不计" not in criterion
    provider = RawReplayProvider(objective_response(correct=correct))
    result = await ObjectiveSkill(provider).grade(
        question(criterion), StudentAnswerInfo(q_id="q1", number="1", type="选择题", content="B" if correct else "A"))
    assert not result.error_kind
    assert result.score == ((2 if conditional else 4) if correct else 0)
    assert len(provider.messages) == 1


UNRESOLVED = "计算过程酌情计分，具体限制详见本题要求"


def resolved_response(criterion=UNRESOLVED, *, correct=False, **changes):
    resolution = {
        "criterion_quote": criterion, "process_credit": "allowed",
        "correct_answer_required": False, "other_conditions_met": True,
        "separate_result_parts": False, "result_score": 0, "process_score": 2,
        "rationale": "按完整原文核对所列限制及其适用范围后计分。",
        **changes,
    }
    return {**objective_response(correct=correct), "policy_resolution": resolution,
            "score": resolution["result_score"] + resolution["process_score"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("correct", [False, True])
@pytest.mark.parametrize("mode,requires_correct,conditions_met", [
    ("allowed", False, True), ("allowed", True, True),
    ("allowed", False, False), ("forbidden", False, True), ("unspecified", False, True),
])
async def test_unresolved_requires_an_explicit_policy_before_settlement(correct, mode, requires_correct, conditions_met):
    policy = process_credit_policy(UNRESOLVED)
    assert policy.unresolved and not policy.uses_rubric_score
    provider = RawReplayProvider(resolved_response(
        correct=correct, process_credit=mode, correct_answer_required=requires_correct,
        other_conditions_met=conditions_met))
    result = await ObjectiveSkill(provider).grade(
        question(UNRESOLVED), StudentAnswerInfo(q_id="q1", number="1", type="选择题", content="部分正确"))
    assert not result.error_kind
    assert result.score == (2 if mode == "allowed" and conditions_met and (correct or not requires_correct) else 0)
    assert len(provider.messages) == 1
    assert "NOT permission" in provider.messages[0][0].content
    assert UNRESOLVED in provider.messages[0][1].content
    assert '"criterion_quote"' in provider.messages[0][1].content
    assert '"other_conditions_met"' in provider.messages[0][1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("criterion", [
    "不计过程分（仅限第二空），第一空按解释完整性计分",
    "只有第一空答案正确才给该空过程分，第二空按解释完整性计分",
])
async def test_subpart_restriction_is_not_promoted_to_a_global_prohibition(criterion):
    policy = process_credit_policy(criterion)
    assert policy.unresolved and policy.mode != "forbidden"
    provider = RawReplayProvider(resolved_response(criterion))
    result = await ObjectiveSkill(provider).grade(
        question(criterion, "填空题"), StudentAnswerInfo(q_id="q1", number="1", type="填空题", content="部分正确"))
    assert not result.error_kind
    assert result.score == 2 and len(provider.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("correct", [False, True])
async def test_exception_qualifies_the_preceding_prohibition(correct):
    criterion = "不计过程分，除非答案正确，此时过程2分"
    policy = process_credit_policy(criterion)
    assert policy.unresolved and policy.mode != "forbidden"
    provider = RawReplayProvider(resolved_response(criterion, correct=correct, correct_answer_required=True))
    result = await ObjectiveSkill(provider).grade(
        question(criterion), StudentAnswerInfo(q_id="q1", number="1", type="选择题", content="B" if correct else "A"))
    assert not result.error_kind
    assert result.score == (2 if correct else 0) and len(provider.messages) == 1


@pytest.mark.asyncio
async def test_explicit_prohibition_still_wins_over_resolution_but_not_result_parts():
    criterion = UNRESOLVED + "；不计过程分；每空2分"
    provider = RawReplayProvider(resolved_response(criterion, result_score=2, separate_result_parts=True))
    result = await ObjectiveSkill(provider).grade(
        question(criterion, "填空题"), StudentAnswerInfo(q_id="q1", number="1", type="填空题", content="4；7"))
    assert not result.error_kind
    assert result.score == 2 and len(provider.messages) == 1


@pytest.mark.asyncio
async def test_resolution_cannot_remove_an_explicit_final_correctness_condition():
    criterion = "只有答案正确才给过程2分；" + UNRESOLVED
    provider = RawReplayProvider(resolved_response(criterion, correct_answer_required=False))
    result = await ObjectiveSkill(provider).grade(
        question(criterion), StudentAnswerInfo(q_id="q1", number="1", type="选择题", content="A"))
    assert not result.error_kind
    assert result.score == 0 and len(provider.messages) == 1


@pytest.mark.asyncio
async def test_resolution_cannot_override_an_explicit_whole_answer_zero_rule():
    criterion = "答案错误就是0分；" + UNRESOLVED
    provider = RawReplayProvider(resolved_response(
        criterion, result_score=2, process_score=0, separate_result_parts=True))
    result = await ObjectiveSkill(provider).grade(
        question(criterion), StudentAnswerInfo(q_id="q1", number="1", type="选择题", content="A"))
    assert not result.error_kind
    assert result.score == 0 and len(provider.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("correct,separate_parts", [(True, False), (False, True)])
async def test_unmet_process_conditions_do_not_erase_earned_result_marks(correct, separate_parts):
    provider = RawReplayProvider(resolved_response(
        correct=correct, result_score=2, other_conditions_met=False, separate_result_parts=separate_parts))
    result = await ObjectiveSkill(provider).grade(
        question(UNRESOLVED, "填空题"),
        StudentAnswerInfo(q_id="q1", number="1", type="填空题", content="已有正确结果，过程条件未满足"))
    assert not result.error_kind
    assert result.score == 2 and len(provider.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("criterion", ["不计过程分（只看最终答案）", "评分时不计过程分"])
async def test_mathematical_plan_uses_the_same_normalized_prohibition(criterion):
    provider = RawReplayProvider(plan_response([
        plan_item("work", "计算过程", 2, "ai"), plan_item("result", "最终答案", 2, "ai"),
    ]))
    plan = await prepare_math_rubric_plan(question(criterion, "计算题"), provider)
    assert plan.original_criterion == criterion
    assert all(route.mode == "holistic" for route in plan.routes)
    assert criterion in plan.effective_criterion and len(provider.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("criterion", [
    "每空2分；不计过程分（只看最终答案）", "每空2分；评分时不计过程分",
])
async def test_normalized_prohibition_keeps_separately_allocated_results(criterion):
    provider = RawReplayProvider(objective_response())
    result = await ObjectiveSkill(provider).grade(
        question(criterion, "填空题"), StudentAnswerInfo(q_id="q1", number="1", type="填空题", content="4；7"))
    assert not result.error_kind
    assert result.score == 2 and len(provider.messages) == 1


@pytest.mark.asyncio
async def test_existing_format_repair_can_supply_missing_policy_evidence():
    provider = RawReplayProvider(objective_response(), resolved_response(process_credit="forbidden"))
    result = await ObjectiveSkill(provider).grade(
        question(UNRESOLVED), StudentAnswerInfo(q_id="q1", number="1", type="选择题", content="A"))
    assert not result.error_kind
    assert result.score == 0 and len(provider.messages) == 2


@pytest.mark.asyncio
async def test_fractional_components_at_the_maximum_do_not_fail_from_float_rounding():
    p = question(UNRESOLVED)
    p.max_score = .3
    provider = RawReplayProvider(resolved_response(correct=True, result_score=.1, process_score=.2))
    result = await ObjectiveSkill(provider).grade(
        p, StudentAnswerInfo(q_id="q1", number="1", type="选择题", content="B"))
    assert not result.error_kind
    assert result.score == .3


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["missing", "source", "sum", "rationale"])
async def test_incomplete_interpretation_is_a_failed_grade_not_a_successful_score(defect):
    payload = resolved_response()
    if defect == "missing":
        del payload["policy_resolution"]
    elif defect == "source":
        payload["policy_resolution"]["criterion_quote"] = "计算过程酌情计分"
    elif defect == "sum":
        payload["score"] = 4
    else:
        payload["policy_resolution"]["rationale"] = " "
    # Missing schema fields use only the existing one format-repair attempt.
    provider = RawReplayProvider(payload, payload)
    result = await ObjectiveSkill(provider).grade(
        question(UNRESOLVED), StudentAnswerInfo(q_id="q1", number="1", type="选择题", content="部分正确"))
    assert result.error_kind
    assert len(provider.messages) == (2 if defect == "missing" else 1)
