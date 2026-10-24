"""PR262 reviewer counterexamples: real parsing and settlement, fixed responses."""
from unittest.mock import AsyncMock

import pytest

from backend.models import StudentAnswerInfo
from backend.services.math_grading_context import math_plans_context
from backend.services.math_rubric_plan import prepare_math_rubric_plan
from backend.services.process_credit_policy import process_credit_policy
from backend.skills import objective
from backend.skills.objective import ObjectiveSkill
from backend.skills.calculation import CalculationSkill
from backend.tests.test_process_credit_policy_acceptance import (
    RawReplayProvider, objective_response, plan_item, plan_response, question,
)


@pytest.fixture(autouse=True)
def no_external_knowledge(monkeypatch):
    monkeypatch.setattr(objective.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))


@pytest.mark.asyncio
@pytest.mark.parametrize("correct", [False, True])
@pytest.mark.parametrize("kind", ["选择题", "填空题"])
@pytest.mark.parametrize("criterion", [
    "评分标准：过程2分，答案2分",
    "过程占50%，答案占50%",
    "评分标准：过程占 50％，答案占 50％",
    "过程2分，答案2分，若未作答则0分",
    "过程2分，答案2分；若未作答则0分",
    "过程2分，答案2分。若未作答则0分",
])
async def test_explicit_allocation_is_not_flattened_to_zero_or_full(criterion, correct, kind):
    p = question(criterion, kind)
    provider = RawReplayProvider(objective_response(correct=correct))
    result = await ObjectiveSkill(provider).grade(
        p, StudentAnswerInfo(q_id="q1", number="1", type=p.type, content="一项正确，另一项未完成。"))
    assert not result.error_kind
    assert result.score == 2
    assert p.criterion == criterion
    assert len(provider.messages) == 1
    assert criterion in provider.messages[0][1].content
    assert "Process credit policy: allowed" in provider.messages[0][0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("criterion", [
    "每空2分，不计过程分",
    "每空2分，只看最终答案",
    "两空各2分，不计过程分",
    "第一空2分，第二空2分，不计过程分",
    "每空答对得2分，答错0分，不计过程分",
    "每空占50%，不计过程分",
    "每空2分（共两空），不计过程分",
])
async def test_result_component_credit_is_independent_of_process_prohibition(criterion):
    p = question(criterion, "填空题")
    p.stem = "两个空分别填写 2+2 和 3+3 的结果。"
    p.reference_answer = "4；6"
    provider = RawReplayProvider({**objective_response(), "student_answer": "4；7",
                                  "reference_answer": "4；6"})
    result = await ObjectiveSkill(provider).grade(
        p, StudentAnswerInfo(q_id="q1", number="1", type=p.type, content="4；7"))
    assert not result.error_kind
    assert result.score == 2 and result.steps == []
    system = provider.messages[0][0].content
    assert "do not award process credit" in system.lower()
    assert "incorrect receives 0" not in system


@pytest.mark.asyncio
@pytest.mark.parametrize("description", ["计算过程", "中间步骤", "推导步骤"])
@pytest.mark.parametrize("mode", ["points", "deductions"])
async def test_forbidden_process_policy_checks_both_awards_and_deductions(description, mode):
    p = question("不计过程分，只看最终答案", "计算题")
    items = [plan_item("work", description if mode == "points" else description + "缺失扣2分", 2, "ai")]
    if mode == "points":
        items.append(plan_item("result", "最终答案2分", 2, "ai"))
    response = plan_response(items)
    response["routes"][0]["mode"] = mode
    provider = RawReplayProvider(response)
    plan = await prepare_math_rubric_plan(p, provider)
    assert all(route.mode == "holistic" for route in plan.routes)
    assert plan.original_criterion == p.criterion
    assert p.criterion in plan.effective_criterion
    assert len(provider.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["effective_criterion", "follow_through", "numerical_policy"])
@pytest.mark.parametrize("invented_rule", ["过程缺失扣2分", "如果答案正确，过程2分"])
async def test_holistic_working_rule_cannot_override_prohibition(field, invented_rule):
    response = plan_response([])
    response["routes"][0]["mode"] = "holistic"
    response[field] = invented_rule
    p = question("不计过程分，只看最终答案", "计算题")
    plan = await prepare_math_rubric_plan(p, RawReplayProvider(response))
    assert invented_rule not in plan.effective_criterion
    assert plan.original_criterion == p.criterion
    assert invented_rule not in getattr(plan, field)


def test_unparsed_scoring_instruction_is_not_claimed_to_be_unspecified():
    policy = process_credit_policy("依据计算过程的完整程度酌情给2分，答案2分")
    assert policy.mode != "unspecified" or policy.unresolved


@pytest.mark.asyncio
@pytest.mark.parametrize("correct", [False, True])
async def test_unparsed_scoring_instruction_does_not_force_a_binary_score(correct):
    p = question("依据计算过程的完整程度酌情给2分，答案2分")
    provider = RawReplayProvider(objective_response(correct=correct))
    result = await ObjectiveSkill(provider).grade(
        p, StudentAnswerInfo(q_id="q1", number="1", type=p.type, content="部分正确。"))
    assert not result.error_kind
    assert result.score == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("criterion", ["每空2分钟，不计过程分", "计算过程限时2分钟"])
async def test_time_limit_is_not_a_scoring_allocation(criterion):
    provider = RawReplayProvider(objective_response())
    result = await ObjectiveSkill(provider).grade(
        question(criterion, "填空题"),
        StudentAnswerInfo(q_id="q1", number="1", type="填空题", content="3"))
    assert not result.error_kind
    assert result.score == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("criterion", ["不计过程分", "评分标准：不计过程分，只看最终答案"])
@pytest.mark.parametrize("correct", [False, True])
async def test_known_prohibition_still_protects_single_result_questions(criterion, correct):
    provider = RawReplayProvider(objective_response(correct=correct))
    result = await ObjectiveSkill(provider).grade(
        question(criterion), StudentAnswerInfo(q_id="q1", number="1", type="选择题", content="B" if correct else "A"))
    assert result.score == (4 if correct else 0)


@pytest.mark.asyncio
async def test_valid_result_item_can_repeat_the_process_prohibition():
    criterion = "最终答案4分；不计过程分"
    provider = RawReplayProvider(plan_response([plan_item("result", criterion, 4)]))
    plan = await prepare_math_rubric_plan(question(criterion, "计算题"), provider)
    assert plan.routes[0].mode == "points"
    assert plan.routes[0].items[0].points == 4


@pytest.mark.asyncio
async def test_explicit_process_deduction_remains_valid_when_not_forbidden():
    criterion = "计算过程缺失扣2分"
    response = plan_response([plan_item("work", criterion, 2)])
    response["routes"][0]["mode"] = "deductions"
    plan = await prepare_math_rubric_plan(question(criterion, "计算题"), RawReplayProvider(response))
    assert plan.routes[0].mode == "deductions"


@pytest.mark.asyncio
async def test_numerical_working_context_does_not_invent_a_scoring_effect():
    p = question("最终答案4分；不计过程分", "计算题")
    response = plan_response([plan_item("result", "最终答案4分", 4)])
    response["numerical_policy"] = "计算过程保留2位小数，最终答案按题设精度核对。"
    plan = await prepare_math_rubric_plan(p, RawReplayProvider(response))
    assert plan.routes[0].mode == "points"


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [0, 4])
@pytest.mark.parametrize("mode", ["points", "deductions"])
async def test_rejected_math_working_rule_never_reaches_grading_ledger(mode, score):
    p = question("不计过程分，只看最终答案", "计算题")
    items = [plan_item("work", "中间步骤" if mode == "points" else "计算过程缺失扣2分", 2, "ai")]
    if mode == "points":
        items.append(plan_item("answer", "最终答案", 2, "ai"))
    proposal = plan_response(items)
    proposal["routes"][0]["mode"] = mode
    plan = await prepare_math_rubric_plan(p, RawReplayProvider(proposal))
    assert all(route.mode == "holistic" and not route.items for route in plan.routes)
    provider = RawReplayProvider({
        "route_id": plan.routes[0].id, "route_reason": "遵守教师只看最终答案的规定。",
        "score": score, "rationale": "最终答案正确，给4分。" if score else "最终答案错误，给0分。",
        "comment": "按最终答案计分。", "confidence": .9,
    })
    with math_plans_context({"q1": plan}):
        result = await CalculationSkill(provider).grade(
            p, StudentAnswerInfo(q_id="q1", number="1", type="计算题", content="4" if score else "3"))
    assert not result.error_kind
    assert result.score == sum(step.score for step in result.steps) == score
    assert p.criterion in provider.messages[0][1].content
    assert "计算过程缺失扣2分" not in provider.messages[0][1].content
    assert len(provider.messages) == 1
