"""Raw JSON policy quotes preserve full source scope and sentence boundaries."""
from copy import deepcopy

import pytest

from backend.services import math_rubric_plan as rubric
from backend.tests.test_math_rubric_plan import question
from backend.tests.test_math_rubric_plan_reliability import candidate, raw_provider


RULE = "每错扣1分"


def payload(exemption=None, policy=None):
    result = candidate(RULE)
    if exemption is not None:
        result["routes"][0]["exemptions"] = [dict(id="minor", description=exemption, source_quote=exemption)]
    if policy is not None:
        result["routes"][0].update(error_policy="no_repeat_deduction", error_policy_source_quote=policy)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["。", ".", "；", ";", "\n", " \n", "。\n"])
@pytest.mark.parametrize("kind", ["exemption", "policy"])
async def test_complete_policy_with_original_trailing_sentence_separator_compiles(ending, kind):
    policy = "仅在不影响结果时，小错不扣分" if kind == "exemption" else "同源后续错误不重复扣分"
    source = RULE + "；" + policy + ending + "单位须正确。"
    provider = raw_provider(payload(**{kind: policy + ending}))
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), provider)
    assert plan.compilation_status == "compiled"
    assert len(provider.messages) == 1
    target = plan.routes[0]
    actual = target.exemptions[0].source_quote if kind == "exemption" else target.error_policy_source_quote
    assert actual == policy
    locations = [loc for loc in plan.source_locations if loc.kind == ("exemption" if kind == "exemption" else "error_policy")]
    assert len(locations) == 1
    assert source[locations[0].start:locations[0].end] == policy
    assert target.items[0].points == 1 and target.items[0].max_occurrences is None


@pytest.mark.asyncio
async def test_english_exemption_period_is_aligned_without_rewriting_words():
    policy = "Minor slips receive comments only"
    plan = await rubric.prepare_math_rubric_plan(question(criterion=RULE+"；"+policy+"."),
                                                raw_provider(payload(exemption=policy+".")))
    assert plan.compilation_status == "compiled"
    assert plan.routes[0].exemptions[0].source_quote == policy


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["若最终结果正确", "若最终结果不正确", "仅在不影响结果时", "除非影响结果"])
@pytest.mark.parametrize("supplied", [False, True])
async def test_zero_deduction_retains_the_entire_condition_without_interpreting_it(condition, supplied):
    policy = condition + "，小错扣0分"
    source = RULE + "；" + policy + "。"
    provider = raw_provider(payload(exemption=policy+"。" if supplied else None))
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), provider)
    assert plan.compilation_status == "compiled"
    assert len(provider.messages) == 1
    assert [e.source_quote for e in plan.routes[0].exemptions] == [policy]
    assert condition in plan.routes[0].exemptions[0].description


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", [False, True])
async def test_mixed_amounts_with_detached_condition_do_not_create_blanket_exemption(supplied):
    source = RULE + "，若最终结果正确，小错扣0分。"
    provider = raw_provider(payload(exemption="小错扣0分" if supplied else None))
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), provider)
    assert plan.routes[0].mode == "holistic"
    assert plan.compilation_reason == "unsupported_exemption_scope"
    assert len(provider.messages) == 0
    assert source in plan.effective_criterion


@pytest.mark.asyncio
async def test_simple_independent_numeric_clauses_remain_supported():
    source = RULE + "，小错扣0分。"
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source),
                                                raw_provider(payload(exemption="小错扣0分。")))
    assert plan.compilation_status == "compiled"
    assert plan.routes[0].exemptions[0].source_quote == "小错扣0分"


@pytest.mark.asyncio
@pytest.mark.parametrize(("policy", "quote"), [
    ("仅在不影响结果时，小错扣0分。", "小错扣0分"),
    ("仅在影响结果时，小错扣0分。", "仅在不影响结果时，小错扣0分。"),
    ("小错不扣分。", "小错不扣分；"),
    ("小错不扣分。", "小错无需扣分。"),
    ("小错不扣分；结论不另扣。", "小错不扣分；结论不另扣。"),
    ("条件另述。小错扣0分。", "条件另述。小错扣0分。"),
    ("小错扣0分。", "小错扣1分。"),
    ("小错不扣分；记号小错不扣分。", "小错不扣分"),
    ("小错不扣分；记号小错不扣分。", "小错不扣分；"),
])
async def test_alignment_rejects_missing_conditions_changed_text_cross_sentences_and_ambiguity(policy, quote):
    provider = raw_provider(payload(exemption=quote))
    plan = await rubric.prepare_math_rubric_plan(question(criterion=RULE+"；"+policy), provider)
    assert plan.routes[0].mode == "holistic"
    assert plan.compilation_reason == "exemption_not_in_source"
    assert len(provider.messages) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", ["同源错误不重复扣分；", "同源错误不再扣分。", "不重复扣分。",
    "同源错误不重复扣分。结论不另扣。"])
async def test_causal_policy_alignment_requires_the_complete_original_policy(quote):
    source = RULE + "；同源错误不重复扣分。结论不另扣。"
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), raw_provider(payload(policy=quote)))
    assert plan.routes[0].mode == "holistic"
    assert plan.compilation_reason == "error_policy_not_in_source"


@pytest.mark.asyncio
async def test_duplicate_causal_source_still_requires_unambiguous_location():
    policy = "同源错误不重复扣分"
    source = RULE + "；" + policy + "；" + policy + "。"
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), raw_provider(payload(policy=policy+"。")))
    assert plan.routes[0].mode == "holistic"
    assert plan.compilation_reason == "error_policy_not_in_source"


@pytest.mark.asyncio
async def test_punctuation_alignment_does_not_override_amount_validation():
    data = payload(exemption="小错不扣分。")
    data["routes"][0]["items"][0]["points"] = 9
    plan = await rubric.prepare_math_rubric_plan(question(criterion=RULE+"；小错不扣分。"), raw_provider(data))
    assert plan.routes[0].mode == "holistic"
    assert plan.compilation_reason == "source_amount_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [
    "不同的正确思路、简洁等价证明均接受，不按参考答案逐步对照扣分",
    "正确的替代解法均接受，不按标准解法对照扣分",
    "不因解法不同而扣分", "不能因为证明简洁就扣分", "不因为使用等价方法扣分",
    "仅在论证完整时，不因步骤与参考答案不同而扣分",
])
async def test_explicit_non_penalty_actions_preserve_full_rule(policy):
    provider = raw_provider(payload(exemption=policy+"。"))
    plan = await rubric.prepare_math_rubric_plan(question(criterion=RULE+"；"+policy+"。"), provider)
    assert plan.compilation_status == "compiled"
    assert len(provider.messages) == 1
    assert [e.source_quote for e in plan.routes[0].exemptions] == [policy]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [
    "不正确则扣分", "不按题目要求则扣分", "不按参考答案逐步对照则扣分",
    "不能因为方法正确就不扣分", "不因解法不同而不扣分", "并非不因解法不同而扣分",
])
async def test_negated_conditions_or_double_negation_do_not_become_exemptions(policy):
    plan = await rubric.prepare_math_rubric_plan(question(criterion=RULE+"；"+policy+"。"),
                                                raw_provider(payload(exemption=policy+"。")))
    assert plan.routes[0].mode == "holistic"
    assert plan.compilation_reason in {"exemption_not_in_source", "unsupported_exemption_scope"}


@pytest.mark.asyncio
async def test_alternative_method_condition_cannot_be_removed_from_supplied_source():
    source = RULE + "；仅在论证完整时，不因步骤与参考答案不同而扣分。"
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source),
        raw_provider(payload(exemption="不因步骤与参考答案不同而扣分。")))
    assert plan.routes[0].mode == "holistic"
    assert plan.compilation_reason == "exemption_not_in_source"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["不因步骤与参考答案不同而扣分", "不按参考答案逐步对照扣分"])
async def test_mixed_numeric_and_verbal_policy_does_not_drop_detached_condition(action):
    source = RULE + "，仅在论证完整时，" + action + "。"
    provider = raw_provider(payload(exemption=action+"。"))
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), provider)
    assert plan.routes[0].mode == "holistic"
    assert plan.compilation_reason == "unsupported_exemption_scope"
    assert len(provider.messages) == 0


@pytest.mark.asyncio
async def test_two_complete_teacher_exemptions_leave_one_repair_for_duplicate_deduction_items():
    # Publicly supplied source excerpts, not the private live model payload.
    rule = "每一处独立的重要理解错误、推导错误或未完成的明确主要证明要求扣1分"
    policies = ["仅遗漏特殊情况、局部符号笔误或端点问题等小错误，只指出，不扣分。",
                "不同的正确思路、简洁等价证明均接受，不按参考答案逐步对照扣分。"]
    fixed = candidate(rule)
    fixed["routes"][0]["exemptions"] = [dict(id=f"e{i}", description=policy, source_quote=policy)
                                        for i, policy in enumerate(policies)]
    duplicate = deepcopy(fixed)
    duplicate["routes"][0]["items"] = [{**fixed["routes"][0]["items"][0], "id": f"r{i}"} for i in range(3)]
    provider = raw_provider(duplicate, fixed)
    source = rule + "；" + "".join(policies)
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), provider)
    assert plan.compilation_status == "repaired"
    assert plan.compilation_reason == "ambiguous_source_allocation"
    assert len(provider.messages) == 2
    assert len(plan.routes[0].items) == 1
    assert [e.source_quote for e in plan.routes[0].exemptions] == [p[:-1] for p in policies]
