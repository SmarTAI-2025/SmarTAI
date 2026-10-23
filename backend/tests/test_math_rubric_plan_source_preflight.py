"""Source-only impossibilities must not consume model repair requests."""
import pytest

from backend.services import math_rubric_plan as rubric
from backend.tests.test_math_rubric_plan import ReplayProvider, explanation, item, question, route


@pytest.mark.asyncio
@pytest.mark.parametrize("precision", [
    "绝对误差不超过1e-6", "绝对误差小于等于0.000001", "相对误差不超过1%",
    "absolute error at most 1e-6", "relative error must be at most 1%",
])
async def test_precision_bound_is_not_a_score_cap_or_extra_allocation(precision):
    criterion = f"主要过程8分；{precision}得2分"
    provider = ReplayProvider(explanation(routes=[route(items=[
        item("process", "主要过程8分", 8), item("precision", f"{precision}得2分", 2),
    ])]))
    plan = await rubric.prepare_math_rubric_plan(question(criterion=criterion), provider)
    assert plan.routes[0].mode == "points"
    assert [entry.points for entry in plan.routes[0].items] == [8, 2]
    assert plan.routes[0].items[1].source_quote == f"{precision}得2分"
    assert len(provider.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", [
    "只写最终式最多2分", "方法不合要求最高3分", "每错最多扣2分",
    "deduct up to 2 points for each error", "只要方法正确最低6分",
    "小错不能免扣分", "同源后续错误不重复扣分，但最终答案错误时例外",
])
async def test_unrepresentable_source_reaches_grading_without_calling_plan_model(restriction):
    criterion = "每个错误扣1分；" + restriction
    provider = ReplayProvider()  # An unintended call has no response to consume.
    plan = await rubric.prepare_math_rubric_plan(question(criterion=criterion), provider)
    assert plan.routes[0].mode == "holistic"
    assert criterion == plan.original_criterion and criterion in plan.effective_criterion
    assert plan.compilation_reason.startswith("unsupported_")
    assert provider.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", ["2 points", "20%"])
@pytest.mark.parametrize("subject", ["numerical error", "relative error"])
async def test_penalty_for_error_is_a_real_score_cap_not_numerical_tolerance(bound, subject):
    criterion = f"deduct 1 point for each error; The total penalty for {subject} is at most {bound}."
    provider = ReplayProvider()
    plan = await rubric.prepare_math_rubric_plan(question(criterion=criterion), provider)
    assert plan.routes[0].mode == "holistic" and plan.compilation_reason == "unsupported_score_cap"
    assert criterion in plan.effective_criterion and provider.messages == []


@pytest.mark.asyncio
async def test_valid_source_still_uses_one_bounded_repair_for_changed_model_points():
    source = "方法8分；结果2分"
    def candidate(method, result):
        return explanation(routes=[route(items=[
            item("method", "方法8分", method), item("result", "结果2分", result),
        ])])
    provider = ReplayProvider(candidate(7, 3), candidate(8, 2))
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), provider)
    assert plan.routes[0].mode == "points" and plan.compilation_status == "repaired"
    assert len(provider.messages) == 2


def test_previous_interpretation_plan_is_not_reused_after_policy_fix(monkeypatch):
    from backend.models import ProblemInfo
    from backend.services.math_grading_context import get_math_plan, math_plans_context

    problem = ProblemInfo(**question())
    current_version = rubric.INTERPRETATION_VERSION
    monkeypatch.setattr(rubric, "INTERPRETATION_VERSION", "math_rubric_interpretation_v3")
    old = rubric.fallback_math_rubric_plan(problem)
    monkeypatch.setattr(rubric, "INTERPRETATION_VERSION", current_version)
    with math_plans_context({problem.q_id: old}):
        assert get_math_plan(problem) is None
