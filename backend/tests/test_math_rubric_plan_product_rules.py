"""Score context must not discard executable teacher deductions."""
import pytest

from backend.services import math_rubric_plan as rubric
from backend.tests.test_math_rubric_plan import ReplayProvider, explanation, item, question, route


RULE_ZH = "每一处独立的重要理解错误、推导错误或未完成的明确主要要求扣1分"
RULE_EN = "deduct 1 point for each independent major error"


def deduction_explanation(rule):
    return explanation(routes=[route(mode="deductions", items=[
        item("major_error", rule, 1, max_occurrences=None),
    ])])


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [
    "满分10分", "本题总分10分", "每题满分10分", "指定七题每题满分10分，合计70分",
    "共12题每题满分10分，合计120分", "从10分起计", "空白0分", "空白2分", "得分范围0至10分",
    "得分范围0至100%",
])
async def test_context_amounts_do_not_become_extra_deductions(context):
    source = context + "；" + RULE_ZH
    provider = ReplayProvider(deduction_explanation(RULE_ZH))
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), provider)
    assert plan.routes[0].mode == "deductions"
    assert [(i.points, i.max_occurrences) for i in plan.routes[0].items] == [(1, None)]
    assert source in plan.effective_criterion
    assert len(provider.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stem", ["证明一个群论命题。", "计算定积分并写出过程。", "求带电粒子受到的力，核对单位。"])
async def test_full_lenient_teacher_rule_preserves_computable_ledger_across_subjects(stem):
    source = (
        "指定七题每题满分10分，合计70分；奖励题不纳入。本次采用宽松扣分制，从10分起计。"
        + RULE_ZH + "；同一根因造成的后续错误不重复扣分。"
        "仅遗漏特殊情况、局部符号笔误或端点问题等小错误，只指出，不扣分。"
        "不同的正确思路、简洁等价证明均接受，不按参考答案逐步对照扣分。空白0分，得分范围0至10分。"
    )
    plan = await rubric.prepare_math_rubric_plan(
        question(stem=stem, criterion=source), ReplayProvider(deduction_explanation(RULE_ZH)),
    )
    assert plan.routes[0].mode == "deductions" and plan.routes[0].items[0].points == 1
    assert plan.original_criterion == source
    # The retained teacher rule drives real programmatic arithmetic; model score
    # guesses cannot decide the amount of a supported single deduction.
    from backend.skills.math_grading import Claim, Deduction, MathAssessment, score_assessment
    draft = MathAssessment(route_id="goals", route_reason="Teacher rule applies.",
        claims=[Claim(id="step", quote="x=1", verdict="refuted", reason="One independent error.")],
        deductions=[Deduction(error_id="first", rule_id="major_error", claim_id="step", reason="One error.")],
        comment="One independent error.")
    score, steps = score_assessment(draft, plan, "x=1")
    assert score == sum(step.score for step in steps) == 9


@pytest.mark.asyncio
@pytest.mark.parametrize("stem", ["Find the value of the definite integral.", "Find the electric field, including units."])
async def test_english_context_keeps_the_same_teacher_deduction(stem):
    source = (
        "5 questions, each question is worth 10 points; total 50 points; start from 10 points; "
        + RULE_EN + "; blank answers receive 0 points; scores range from 0 to 10 points. "
        "Minor notation slips receive comments only."
    )
    plan = await rubric.prepare_math_rubric_plan(
        question(stem=stem, criterion=source), ReplayProvider(deduction_explanation(RULE_EN)),
    )
    assert plan.routes[0].mode == "deductions"
    assert plan.routes[0].items[0].points == 1
    assert source in plan.effective_criterion


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [
    "每题满分8分", "合计70分", "从8分起计", "空白11分", "得分范围0至6分",
    "方法正确时最高6分", "方法正确时从8分起计", "未检查单位扣2分", "每错最多扣2分",
    "共7题每题满分10分，合计70分，本题总分8分", "得分范围0至10%",
])
async def test_real_conflicts_caps_or_missing_item_amounts_still_fall_back(context):
    source = context + "；" + RULE_ZH
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), ReplayProvider(deduction_explanation(RULE_ZH)))
    assert plan.routes[0].mode == "holistic"
    assert source in plan.effective_criterion


@pytest.mark.asyncio
async def test_context_does_not_hide_changed_deduction_or_method_points():
    source = "共三题每题满分10分，合计30分；" + RULE_ZH
    changed = deduction_explanation(RULE_ZH)
    changed["routes"][0]["items"][0]["points"] = 3
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), ReplayProvider(changed))
    assert plan.routes[0].mode == "holistic" and source in plan.effective_criterion

    source = "本题满分10分；方法满分4分，结论6分"
    valid = explanation(routes=[route(items=[item("method", "方法满分4分", 4), item("result", "结论6分", 6)])])
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), ReplayProvider(valid))
    assert plan.routes[0].mode == "points"
    assert [i.points for i in plan.routes[0].items] == [4, 6]
