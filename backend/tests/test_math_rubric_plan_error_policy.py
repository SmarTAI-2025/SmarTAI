"""Original B2 policy regressions, through raw JSON compilation and the ledger."""
import json

import pytest

from backend.services import math_rubric_plan as rubric
from backend.skills.math_grading import MathAssessment, score_assessment
from backend.tests.test_math_rubric_plan import question
from backend.tests.test_math_rubric_plan_reliability import candidate, raw_provider


def ledger(plan, *, independent=False, conclusion=False):
    """Keep causal role independent of wording and expected score."""
    claims = [dict(id="first", quote="6/2=2", verdict="refuted", reason="Division error"),
              dict(id="later", quote="2*2=5" if independent else "2*2=4",
                   depends_on=["first"], verdict="refuted" if independent else "supported",
                   reason="New multiplication error" if independent else "Correct carry-through")]
    deductions = [dict(error_id="e1", rule_id="major", claim_id="first", reason="First error"),
                  dict(error_id="e2", rule_id="major", claim_id="later",
                       caused_by=None if independent else "first",
                       reason="Independent error" if independent else "Pure consequence")]
    if conclusion:
        claims.append(dict(id="result", quote="结论为4", verdict="refuted", reason="Wrong final value"))
        deductions.append(dict(error_id="e3", rule_id=plan.routes[0].exemptions[0].id,
                               claim_id="result", reason="Conclusion carries no separate penalty"))
    draft = MathAssessment.model_validate_json(json.dumps(dict(
        route_id="goals", route_reason="Teacher policy", claims=claims,
        deductions=deductions, comment="Source-bound deductions")))
    return score_assessment(draft, plan, "；".join(c["quote"] for c in claims))


@pytest.mark.asyncio
@pytest.mark.parametrize("independent", [False, True], ids=["R13", "R14"])
async def test_original_b2_policy_keeps_causal_and_independent_errors_distinct(independent):
    source = "每个错误扣1分；同源后续错误不重复扣分。"
    result = candidate("每个错误扣1分")
    result["routes"][0].update(error_policy="no_repeat_deduction",
                              error_policy_source_quote="同源后续错误不重复扣分")
    provider = raw_provider(result)
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), provider)
    assert plan.compilation_status == "compiled"
    assert plan.routes[0].mode == "deductions"
    assert len(provider.messages) == 1
    assert ledger(plan, independent=independent)[0] == (8 if independent else 9)


@pytest.mark.asyncio
@pytest.mark.parametrize(("source", "rule", "occurrences", "independent", "expected"), [
    ("满分10；首次数值错误扣1分；正确承接错误值不再扣；结论不另扣。",
     "首次数值错误扣1分", 1, False, 9),
    ("满分10；每个独立数值错误扣1分；正确承接不再扣；结论不另扣。",
     "每个独立数值错误扣1分", None, True, 8),
], ids=["C08", "C09"])
async def test_original_t4_policies_separate_follow_through_from_conclusion_exemption(
        source, rule, occurrences, independent, expected):
    provider = raw_provider(candidate(rule, occurrences=occurrences))
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), provider)
    target = plan.routes[0]
    assert plan.compilation_status == "compiled"
    assert target.mode == "deductions"
    assert target.error_policy == "no_repeat_deduction"
    assert target.error_policy_source_quote == source.split("；")[2]
    assert [e.source_quote for e in target.exemptions] == ["结论不另扣"]
    assert len(provider.messages) == 1
    assert ledger(plan, independent=independent, conclusion=True)[0] == expected
    assert {location.kind for location in plan.source_locations} == {"item", "exemption", "error_policy"}
    for location in plan.source_locations:
        quote = source[location.start:location.end]
        assert quote in {rule, target.error_policy_source_quote, "结论不另扣"}


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [
    "同源后续错误不再扣", "同源的后续错误只扣一次", "同一错误不重复扣",
    "同一根因导致的后续错误不再扣分", "正确承接错误值不重复扣分",
    "正确承接不再扣分", "正确沿用错误中间值不再扣", "正确承接错误结果的后续计算不再扣",
    "consequential errors receive no further deduction",
])
async def test_same_cause_and_correct_continuation_roles_are_not_blanket_exemptions(policy):
    rule = "每个错误扣1分"
    plan = await rubric.prepare_math_rubric_plan(question(criterion=rule+"；"+policy),
                                                raw_provider(candidate(rule)))
    assert plan.compilation_status == "compiled"
    assert plan.routes[0].error_policy == "no_repeat_deduction"
    assert plan.routes[0].error_policy_source_quote == policy
    assert plan.routes[0].exemptions == ()
    assert ledger(plan)[0] == 9
    assert ledger(plan, independent=True)[0] == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["结论不另扣", "最终结果不另行扣分", "答案项不单独扣分"])
async def test_conclusion_exemption_does_not_enable_causal_no_repeat(policy):
    rule = "每个错误扣1分"
    plan = await rubric.prepare_math_rubric_plan(question(criterion=rule+"；"+policy),
                                                raw_provider(candidate(rule)))
    assert plan.compilation_status == "compiled"
    assert plan.routes[0].error_policy == "rubric"
    assert plan.routes[0].error_policy_source_quote == ""
    assert [e.source_quote for e in plan.routes[0].exemptions] == [policy]
    assert ledger(plan, conclusion=True)[0] == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [
    "有特殊情况时同源后续错误不重复扣分", "错误承接错误值不再扣",
    "正确承接时后续独立错误也不再扣", "同源后续错误并非不重复扣分",
    "同源后续错误不重复扣分，但结论仍扣", "后续所有错误不再扣",
    "同源错误只扣一次，但独立错误也算同一次", "结论并非不另扣",
    "结论不另扣，除非方法错误", "consequential errors receive no further deduction unless repeated",
])
async def test_conditional_negated_or_broader_policies_are_not_silently_relaxed(policy):
    source = "每个错误扣1分；" + policy
    provider = raw_provider(candidate("每个错误扣1分"))
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), provider)
    assert plan.routes[0].mode == "holistic"
    assert plan.compilation_reason == "unsupported_error_policy"
    assert source in plan.effective_criterion
    assert len(provider.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["wrong_source", "invent_no_repeat", "invent_exemption"])
async def test_policy_role_and_source_cannot_be_changed_by_model(tamper):
    rule = "每个错误扣1分"
    policy = "同源后续错误不重复扣分" if tamper == "wrong_source" else "同源后续错误仍要扣分"
    result = candidate(rule)
    target = result["routes"][0]
    if tamper == "invent_exemption":
        target["exemptions"] = [dict(id="fake", description="No cost", source_quote=policy)]
    else:
        target.update(error_policy="no_repeat_deduction", error_policy_source_quote=rule if tamper == "wrong_source" else policy)
    provider = raw_provider(result)
    plan = await rubric.prepare_math_rubric_plan(question(criterion=rule+"；"+policy), provider)
    assert plan.routes[0].mode == "holistic"
    assert plan.compilation_reason == ("exemption_not_in_source" if tamper == "invent_exemption" else "error_policy_not_in_source")
    assert len(provider.messages) == 2
