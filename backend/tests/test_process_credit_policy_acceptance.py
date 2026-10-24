"""Independent teacher-policy acceptance with raw JSON replay, never paid models.

The deliberately inconsistent objective response is a program-branch probe;
these tests make no claim about how often a real model produces that response.
"""
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from backend.llm.providers import LLMResponse
from backend.models import ProblemInfo, StudentAnswerInfo
from backend.services.math_grading_context import math_plans_context
from backend.services.math_rubric_plan import prepare_math_rubric_plan
from backend.skills import math_grading, objective
from backend.skills.calculation import CalculationSkill
from backend.skills.objective import ObjectiveSkill


class RawReplayProvider:
    provider_id = "process-policy-acceptance"

    def __init__(self, *responses):
        self.responses = [json.dumps(value, ensure_ascii=False) for value in responses]
        self.messages = []

    async def ainvoke(self, messages):
        self.messages.append(messages)
        assert self.responses, "Unexpected extra model request"
        return LLMResponse(content=self.responses.pop(0), provider="test",
                           model=self.provider_id, duration_ms=0)


@pytest.fixture(autouse=True)
def no_external_knowledge(monkeypatch):
    monkeypatch.setattr(objective.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))


def question(criterion, kind="选择题"):
    return ProblemInfo(q_id="q1", number="1", type=kind,
                       stem="计算 2+2。A. 3；B. 4。", criterion=criterion,
                       reference_answer="B，2+2=4", max_score=4)


def objective_response(*, correct=False):
    return {"student_answer": "B" if correct else "A", "reference_answer": "B",
            "correct": correct, "partial_credit_applicable": False, "score": 2,
            "confidence": .9, "comment": "固定的分支验收响应。"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["选择题", "填空题"])
@pytest.mark.parametrize(("criterion", "expected"), [
    ("每步 1 分", 2),
    ("不计过程分", 0),
    ("只看最终答案", 0),
    ("选项 2 分、解释 2 分", 2),
    ("不计过程分，只看最终答案；答案错误就是 0 分。", 0),
    ("", 0),
    ("请写出过程，便于教师了解解题思路。", 0),
    ("若结论正确，过程2分。", 0),
    ("如果教师明确要求，按步骤给分。", 0),
    ("并非不计过程分。", 0),
])
async def test_wrong_final_answer_obeys_explicit_teacher_policy(kind, criterion, expected):
    p = question(criterion, kind)
    before = p.model_dump()
    provider = RawReplayProvider(objective_response())
    result = await ObjectiveSkill(provider, language="zh").grade(
        p, StudentAnswerInfo(q_id="q1", number="1", type=kind, content="A；前两步正确。"))

    assert not result.error_kind
    assert result.score == expected
    assert result.max_score == 4 and result.steps == []
    assert json.loads(result.raw_output)["score"] == 2
    assert p.model_dump() == before
    assert len(provider.messages) == 1
    assert criterion in provider.messages[0][1].content


@pytest.mark.asyncio
@pytest.mark.parametrize(("criterion", "expected"), [
    ("不计过程分", 4), ("只看最终答案", 4), ("", 4),
    ("选项 2 分、解释 2 分", 2),
])
async def test_correct_answer_keeps_the_teacher_scoring_contract(criterion, expected):
    provider = RawReplayProvider(objective_response(correct=True))
    result = await ObjectiveSkill(provider).grade(
        question(criterion), StudentAnswerInfo(q_id="q1", number="1", type="选择题", content="B"))
    assert not result.error_kind
    assert result.score == expected
    assert len(provider.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("criterion", ["每步 1 分", "选项 2 分、解释 2 分"])
async def test_allowed_policy_is_not_contradicted_by_unconditional_prompt_bans(criterion):
    provider = RawReplayProvider(objective_response())
    await ObjectiveSkill(provider).grade(
        question(criterion), StudentAnswerInfo(q_id="q1", number="1", type="选择题", content="A"))
    system, user = (message.content for message in provider.messages[0])
    assert "never award process credit" not in system.lower()
    assert "Never award credit for reasoning, working, or process." not in user


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "不计过程分；"])
async def test_no_process_credit_does_not_remove_explicit_multi_choice_subset_credit(prefix):
    p = question(prefix + "全部选对得4分，部分选对得2分，错选不得分。", "多选题")
    payload = {**objective_response(), "student_answer": "A", "reference_answer": "AB",
               "partial_credit_applicable": True}
    provider = RawReplayProvider(payload)
    result = await ObjectiveSkill(provider).grade(
        p, StudentAnswerInfo(q_id="q1", number="1", type="多选题", content="A"))
    assert not result.error_kind
    assert result.score == 2 and result.steps == []
    assert len(provider.messages) == 1


@pytest.mark.asyncio
async def test_multi_choice_model_flag_cannot_override_explicit_zero_rule():
    provider = RawReplayProvider({**objective_response(), "partial_credit_applicable": True})
    result = await ObjectiveSkill(provider).grade(
        question("不计过程分；答案错误就是0分", "多选题"),
        StudentAnswerInfo(q_id="q1", number="1", type="多选题", content="A"))
    assert not result.error_kind
    assert result.score == 0
    assert len(provider.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("criterion", [
    "过程正确得2分，答案2分", "给过程2分，答案2分", "过程分为2分，答案2分", "步骤：2分，答案2分",
    "1. 过程2分\n2. 答案2分", "(1)过程2分；(2)答案2分",
    "①过程2分；②答案2分", "- 过程2分\n- 答案2分",
    "过程0分；解释2分；选项2分", "过程2分；解释0分；选项2分",
])
async def test_scoring_clause_format_does_not_erase_other_earned_process_items(criterion):
    provider = RawReplayProvider(objective_response())
    result = await ObjectiveSkill(provider).grade(
        question(criterion),
        StudentAnswerInfo(q_id="q1", number="1", type="选择题", content="A；解释正确。"))
    assert not result.error_kind
    assert result.score == 2
    assert len(provider.messages) == 1


def plan_response(items):
    return {
        "effective_criterion": "依据教师原文逐项评分。",
        "routes": [{"id": "main", "description": "适用的教师评分规则",
                    "applies_when": "全部作答", "mode": "points", "items": items,
                    "minimum_score": 0, "minimum_condition": "always"}],
        "follow_through": "以教师原文为准。", "numerical_policy": "采用精确算术。",
        "advisories": [],
    }


def plan_item(item_id, description, points, source="provided"):
    return {"id": item_id, "description": description, "points": points,
            "source": source, "max_occurrences": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("description", ["正确方法得2分", "method", "working"])
async def test_generated_plan_cannot_add_process_points_against_teacher_prohibition(description):
    p = question("不计过程分，只看最终答案", "计算题")
    before = deepcopy(p.model_dump())
    provider = RawReplayProvider(plan_response([
        plan_item("method", description, 2, "ai"),
        plan_item("result", "正确结果得2分", 2, "ai"),
    ]))
    plan = await prepare_math_rubric_plan(p, provider)
    assert p.model_dump() == before
    assert plan.original_criterion == p.criterion
    assert p.criterion in plan.effective_criterion
    assert all(route.mode == "holistic" for route in plan.routes)
    assert len(provider.messages) == 1


@pytest.mark.asyncio
async def test_holistic_interpretation_cannot_reverse_explicit_teacher_prohibition():
    p = question("不计过程分，只看最终答案", "计算题")
    response = plan_response([])
    response["routes"][0]["mode"] = "holistic"
    response["effective_criterion"] = "过程2分、答案2分"
    provider = RawReplayProvider(response)
    plan = await prepare_math_rubric_plan(p, provider)
    assert plan.original_criterion == p.criterion
    assert p.criterion in plan.effective_criterion
    assert "过程2分、答案2分" not in plan.effective_criterion
    assert len(provider.messages) == 1


@pytest.mark.asyncio
async def test_no_process_credit_preserves_valid_result_only_structured_plan():
    p = question("不计过程分；最终答案正确4分", "计算题")
    provider = RawReplayProvider(plan_response([
        plan_item("result", "最终答案正确4分", 4),
    ]))
    plan = await prepare_math_rubric_plan(p, provider)
    assert plan.routes[0].mode == "points"
    assert [(item.description, item.points) for item in plan.routes[0].items] == [
        ("最终答案正确4分", 4),
    ]
    assert plan.original_criterion == p.criterion
    assert len(provider.messages) == 1


@pytest.mark.asyncio
async def test_calculation_explicit_step_points_survive_real_plan_and_grading_parsers():
    p = question("每步1分", "计算题")
    items = [plan_item(f"step{i}", "每步1分", 1) for i in range(1, 5)]
    plan_provider = RawReplayProvider(plan_response(items))
    plan = await prepare_math_rubric_plan(p, plan_provider)
    assert plan.routes[0].mode == "points"
    text = "第一步正确；第二步正确；第三步错误；最终答案3。"
    assessment = {
        "route_id": "main", "route_reason": "按教师规定每步1分。",
        "claims": [{"id": "work", "quote": text, "item_ids": [i["id"] for i in items],
                    "verdict": "refuted", "reason": "最终结果有误，前两步正确。"}],
        "awards": [{"item_id": item["id"], "score": 1 if i < 2 else 0,
                    "claim_ids": ["work"], "reason": "依据这一步的实际作答。"}
                   for i, item in enumerate(items)],
        "confidence": .9, "comment": "前两步得2分。",
    }
    provider = RawReplayProvider(assessment)
    with math_plans_context({"q1": plan}):
        result = await CalculationSkill(provider).grade(
            p, StudentAnswerInfo(q_id="q1", number="1", type="计算题", content=text))
    assert not result.error_kind
    assert result.score == sum(step.score for step in result.steps) == 2
    assert math_grading.read_math_trace(result)["plan_id"] == plan.plan_id
    assert p.criterion in provider.messages[0][1].content
    assert len(provider.messages) == 1
