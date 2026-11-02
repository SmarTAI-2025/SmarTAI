"""The compiler locates model-supplied rules; it must not invent scoring policy."""
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from backend.services import math_rubric_plan as rubric
from backend.skills.math_grading import Claim, Deduction, MathAssessment, score_assessment
from backend.tests.test_math_rubric_plan import question
from backend.tests.test_math_rubric_plan_reliability import candidate, raw_provider


RULE = "每个错误扣1分"


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["小错严禁免扣分", "小错杜绝免扣分"])
async def test_keyword_does_not_create_an_exemption_or_change_a_real_deduction(policy):
    source = RULE + "；" + policy + "。"
    provider = raw_provider(candidate(RULE))
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), provider)
    assert plan.compilation_status == "compiled"
    assert plan.routes[0].exemptions == ()
    assert plan.original_criterion == source and source in plan.effective_criterion
    assert len(provider.messages) == 1
    assessment = MathAssessment(route_id="goals", route_reason="按教师标准每错扣1分",
        claims=[Claim(id="c", quote="1+1=3", verdict="refuted", reason="加法错误")],
        deductions=[Deduction(error_id="e", rule_id="major", claim_id="c", reason="扣1分")],
        comment="加法错误扣1分")
    assert score_assessment(assessment, plan, "1+1=3")[0] == 9


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["小错不扣分", "同源后续错误不重复扣分"])
async def test_uncompiled_text_policy_is_preserved_without_inventing_rules_or_repair_calls(policy):
    source = RULE + "；" + policy
    provider = raw_provider(candidate(RULE))
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), provider)
    target = plan.routes[0]
    assert plan.compilation_status == "compiled"
    assert target.exemptions == () and target.error_policy == "rubric"
    assert target.error_policy_source_quote == ""
    assert source in plan.effective_criterion and len(provider.messages) == 1
    # The full teacher standard still decides which errors are chargeable.
    # Recording a minor error does not force a deduction or a direct-grade fallback.
    if policy != "小错不扣分":
        assessment = MathAssessment(route_id="goals", route_reason="按原文同源错误只扣一次",
            claims=[Claim(id="first", quote="6/2=2", verdict="refuted", reason="除法首错"),
                    Claim(id="next", quote="2*2=4", depends_on=["first"], verdict="supported",
                          reason="正确承接错误中间值，按原文不重复扣分")],
            deductions=[Deduction(error_id="e", rule_id="major", claim_id="first", reason="首错扣1分")],
            comment="首错扣1分，后续正确承接不再扣")
        assert score_assessment(assessment, plan, "6/2=2；2*2=4")[0] == 9
        return
    assessment = MathAssessment(route_id="goals", route_reason="教师规则适用",
        claims=[Claim(id="c", quote="1+1=3", verdict="refuted",
                      reason="按题目原始标准，此处仅提示，不扣分")],
        rationale="教师明确免扣的错误没有列入扣分账本", comment="按原始标准不扣分")
    assert score_assessment(assessment, plan, "1+1=3")[0] == 10


@pytest.mark.asyncio
async def test_a_policy_supplied_for_one_route_is_not_injected_into_another():
    source = RULE + "；小错不扣分；同源后续错误不重复扣分"
    result = candidate(RULE)
    first = result["routes"][0]
    second = deepcopy(first)
    second.update(id="other", applies_when="另一种教师规定的适用情况")
    first.update(exemptions=[dict(id="minor", description="小错不扣分", source_quote="小错不扣分")],
                 error_policy="no_repeat_deduction", error_policy_source_quote="同源后续错误不重复扣分")
    result["routes"].append(second)
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), raw_provider(result))
    assert plan.compilation_status == "compiled"
    assert [e.id for e in plan.routes[0].exemptions] == ["minor"]
    assert plan.routes[0].error_policy == "no_repeat_deduction"
    assert plan.routes[1].exemptions == () and plan.routes[1].error_policy == "rubric"
    assert plan.routes[1].error_policy_source_quote == ""


@pytest.mark.asyncio
async def test_policy_quote_does_not_silently_enable_an_unselected_switch():
    result = candidate(RULE)
    result["routes"][0]["error_policy_source_quote"] = "同源后续错误不重复扣分"
    provider = raw_provider(result)
    source = RULE + "；同源后续错误不重复扣分"
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), provider)
    assert plan.routes[0].mode == "holistic"
    assert plan.compilation_reason == "error_policy_not_in_source"
    assert source in plan.effective_criterion


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["计算题", "推理题", "概念题"])
@pytest.mark.parametrize("policy,expected", [("小错严禁免扣分", 9), ("小错不扣分", 10)])
async def test_original_rule_reaches_all_three_skills_without_direct_fallback(monkeypatch, kind, policy, expected):
    from backend.models import ProblemInfo, StudentAnswerInfo
    from backend.services.math_grading_context import math_plans_context
    from backend.skills import math_grading as grading
    from backend.skills.calculation import CalculationSkill
    from backend.skills.concept import ConceptSkill
    from backend.skills.proof import ProofSkill

    source = RULE + "；" + policy
    problem = ProblemInfo(**question(type=kind, stem="检查此计算。", criterion=source))
    answer = StudentAnswerInfo(q_id="q1", number="1", type=kind, content="1+1=3")
    draft = MathAssessment(route_id="goals", route_reason="原始教师标准适用",
        claims=[Claim(id="c", quote=answer.content, verdict="refuted", reason="加法错误；" + policy)],
        deductions=([Deduction(error_id="e", rule_id="major", claim_id="c", reason="加法错误扣1分")]
                    if expected == 9 else []),
        rationale="原文明确小错不扣分" if expected == 10 else "加法错误扣1分",
        comment="加法错误，依教师标准处理")
    provider = raw_provider(candidate(RULE), draft.model_dump(mode="json"))
    plan = await rubric.prepare_math_rubric_plan(problem, provider)
    monkeypatch.setattr(grading.kb_tool, "retrieve_for_grading", AsyncMock(return_value=[]))
    # This test covers plan compilation, source transfer, real JSON validation
    # and accounting; mathematical verification has its own independent suite.
    monkeypatch.setattr(grading.MathGradingSkill, "_verify", AsyncMock(return_value=[]))
    skill = {"计算题": CalculationSkill, "推理题": ProofSkill, "概念题": ConceptSkill}[kind]
    with math_plans_context({problem.q_id: plan}):
        result = await skill(provider).grade(problem, answer)
    trace = grading.read_math_trace(result)
    assert result.score == expected and not result.error_kind
    assert plan.compilation_status == "compiled" and plan.routes[0].exemptions == ()
    assert "assessment" in trace and "direct" not in trace
    assert len(trace["assessment"]["claims"]) == 1
    assert len(provider.messages) == 2  # One shared preparation + one detailed assessment.
    assert source in provider.messages[-1][-1].content
