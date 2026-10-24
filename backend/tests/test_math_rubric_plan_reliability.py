"""Synthetic JSON replays: compiler acceptance must reach the actual ledger."""
import asyncio
import json
from copy import deepcopy

import pytest

from backend.services import math_rubric_plan as rubric
from backend.tests.test_math_rubric_plan import ReplayProvider, explanation, item, question, route
from backend.skills.math_grading import Claim, Deduction, MathAssessment, score_assessment


def candidate(rule, points=1, occurrences=None, *, exemptions=(), error_policy=None, **changes):
    policies = {"exemptions": [dict(id=f"exemption_{n}", description=quote, source_quote=quote)
                               for n, quote in enumerate(exemptions)]}
    if error_policy is not None:
        policies.update(error_policy="no_repeat_deduction", error_policy_source_quote=error_policy)
    return explanation(routes=[route(mode="deductions", items=[item(
        "major", "Independent major error", points, source_quote=rule,
        max_occurrences=occurrences, **changes,
    )], **policies)])


def raw_provider(*payloads):
    return ReplayProvider(*(json.dumps(p, ensure_ascii=False) for p in payloads))


def score_errors(plan, count):
    texts = [f"{n}+1={n+2}" for n in range(count)]
    draft = MathAssessment(route_id="goals", route_reason="Shared rule applies",
        claims=[Claim(id=f"c{n}", quote=t, verdict="refuted", reason="Independent error")
                for n, t in enumerate(texts)] or [Claim(id="valid", quote="Valid argument",
                    verdict="supported", reason="Submitted argument has no identified error.")],
        deductions=[Deduction(error_id=f"e{n}", rule_id="major", claim_id=f"c{n}",
                              reason="Independent error") for n in range(count)],
        comment="Explicit teacher rule", rationale="No independent errors" if not count else "")
    return score_assessment(draft, plan, "\n".join(texts) or "Valid argument")


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["1", "一", "１", "1.0", "一点零"])
@pytest.mark.parametrize("count", [0, 1, 2, 3])
async def test_explicit_amount_spellings_reach_ledger(amount, count):
    rule = f"每个重要错误扣{amount}分"
    provider = raw_provider(candidate(rule), candidate(rule))
    plan = await rubric.prepare_math_rubric_plan(question(criterion=rule), provider)
    assert plan.routes[0].mode == "deductions"
    assert len(provider.messages) == 1
    score, steps = score_errors(plan, count)
    assert score == sum(s.score for s in steps) == 10-count


@pytest.mark.asyncio
@pytest.mark.parametrize(("prefix", "suffix", "occurrences"), [
    ("每题10分；", "", None), ("每题十分；", "", None),
    ("", "；小错扣0分", None), ("", "；小错不扣", None),
    ("", "最多三次", 3), ("", "最多３次", 3),
])
async def test_simple_context_and_exemptions_keep_ledger(prefix, suffix, occurrences):
    rule = "每个重要错误扣一分" + (suffix if occurrences else "")
    source = prefix + rule + (suffix if not occurrences else "")
    result = candidate(rule, occurrences=occurrences, exemptions=[suffix[1:]] if "小错" in source else [])
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source),
        raw_provider(result))
    assert plan.routes[0].mode == "deductions"
    assert score_errors(plan, 3)[0] == 7
    if "小错" in source:
        assert plan.routes[0].exemptions[0].source_quote == suffix[1:]


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", [
    "deduct 1 point for each independent major error at most 3 times",
    "deduct one point for each independent major error at most three times",
])
async def test_english_occurrence_cap_is_not_score_cap(rule):
    plan = await rubric.prepare_math_rubric_plan(question(criterion=rule),
        raw_provider(candidate(rule, occurrences=3), candidate(rule, occurrences=3)))
    assert plan.routes[0].mode == "deductions"
    assert plan.routes[0].items[0].max_occurrences == 3
    assert score_errors(plan, 3)[0] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["provided", "ai"])
async def test_source_label_never_allows_changed_chinese_amount(origin):
    rule = "每个重要错误扣一分"
    bad = candidate(rule, points=9, source=origin)
    plan = await rubric.prepare_math_rubric_plan(question(criterion=rule), raw_provider(bad, bad))
    assert plan.routes[0].mode == "holistic"
    assert rule in plan.effective_criterion


@pytest.mark.asyncio
async def test_display_is_not_source_and_raw_math_quote_survives_real_parser():
    rule = r"每次错误使用\(x^2=1\)扣1分"
    result = candidate(rule)
    result["routes"][0]["items"][0]["description"] = r"错误使用\(x^2=1\)"
    plan = await rubric.prepare_math_rubric_plan(question(criterion=rule), raw_provider(result, result))
    assert plan.routes[0].mode == "deductions"
    assert plan.routes[0].items[0].source_quote == rule
    assert "$x^2=1$" in plan.routes[0].items[0].description
    assert score_errors(plan, 1)[0] == 9


@pytest.mark.asyncio
async def test_explicit_no_repeat_policy_compiles_for_mainchain():
    rule = "每个重要错误扣1分"
    policy = "同一根因造成的后续错误不重复扣分"
    plan = await rubric.prepare_math_rubric_plan(question(criterion=rule+"；"+policy),
        raw_provider(candidate(rule, error_policy=policy)))
    assert plan.routes[0].mode == "deductions"
    assert plan.routes[0].error_policy == "no_repeat_deduction"
    assert plan.routes[0].error_policy_source_quote == policy


@pytest.mark.asyncio
async def test_one_shared_parser_repair_can_fix_amount_without_changing_teacher_rule():
    rule = "每个重要错误扣一分"
    provider = raw_provider(candidate(rule, 9, source="ai"), candidate(rule))
    plan = await rubric.prepare_math_rubric_plan(question(criterion=rule), provider)
    assert len(provider.messages) == 2
    assert plan.routes[0].mode == "deductions"
    assert score_errors(plan, 1)[0] == 9
    assert plan.compilation_status == "repaired"


@pytest.mark.asyncio
async def test_correction_does_not_swallow_cancellation():
    rule = "每个重要错误扣一分"
    provider = raw_provider(candidate(rule, 9, source="ai"))
    provider.responses[-1] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await rubric.prepare_math_rubric_plan(question(criterion=rule), provider)


@pytest.mark.asyncio
async def test_prepared_event_storage_failure_is_not_downgraded():
    from backend.services.grading_checkpoints import CheckpointWriteError
    class Reporter:
        async def _emit_message(self, message):
            if message == "Shared question scoring rules prepared":
                raise OSError("synthetic storage failure")

    rule = "每个重要错误扣一分"
    with pytest.raises(CheckpointWriteError, match="math_progress_write_failed") as caught:
        await rubric.prepare_math_rubric_plan(question(criterion=rule),
            raw_provider(candidate(rule)), reporter=Reporter())
    assert isinstance(caught.value.__cause__, OSError)


@pytest.mark.asyncio
@pytest.mark.parametrize(("source", "change", "reason"), [
    ("每错扣1分", "duplicate", "ambiguous_source_allocation"),
    ("每错扣1分", "extra", "unsourced_additional_allocation"),
    ("每错扣1分", "exemption", "exemption_not_in_source"),
    ("每错扣1分", "policy", "error_policy_not_in_source"),
    ("每错扣1分最多3次", "count", "source_repetition_changed"),
    ("每错扣1分", "floor", "invented_minimum"),
    ("每错扣1分；漏单位扣2分", "omit", "source_allocation_changed_or_omitted"),
])
async def test_untrusted_rule_changes_never_enter_executable_plan(source, change, reason):
    quote = source.split("；")[0]
    result = candidate(quote, occurrences=3 if "最多" in quote else None)
    target = result["routes"][0]
    if change == "duplicate":
        duplicate = deepcopy(target["items"][0])
        duplicate["id"] = "second"
        target["items"].append(duplicate)
    elif change == "extra":
        target["items"].append(item("invented", "AI additional penalty", 9, source="ai"))
    elif change == "exemption":
        target["exemptions"] = [{"id": "all", "description": "all errors", "source_quote": quote}]
    elif change == "policy":
        target["error_policy"] = "no_repeat_deduction"
    elif change == "count":
        target["items"][0]["max_occurrences"] = 2
    elif change == "floor":
        target["minimum_score"] = 6
    provider = raw_provider(result, result)
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), provider)
    assert plan.routes[0].mode == "holistic"
    assert plan.compilation_reason == reason
    assert len(provider.messages) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("amount", "value"), [("半", .5), ("零点五", .5), ("两", 2), ("２．５", 2.5)])
async def test_fractional_and_chinese_rates_use_decimal_values(amount, value):
    rule = f"每错扣{amount}分"
    plan = await rubric.prepare_math_rubric_plan(question(criterion=rule), raw_provider(candidate(rule, value)))
    assert plan.routes[0].mode == "deductions"
    assert score_errors(plan, 2)[0] == 10-2*value


@pytest.mark.asyncio
@pytest.mark.parametrize("floor", ["非空最低六分", "非空最低６分", "非空最低60%"])
async def test_explicit_nonblank_floor_reaches_real_ledger(floor):
    rule = "每错扣一分"
    result = candidate(rule)
    result["routes"][0].update(minimum_score=6, minimum_condition="nonblank")
    plan = await rubric.prepare_math_rubric_plan(question(criterion=rule+"；"+floor), raw_provider(result))
    assert plan.routes[0].mode == "deductions"
    assert score_errors(plan, 7)[0] == 6
    with pytest.raises(ValueError, match="blank_deduction"):
        score_assessment(MathAssessment(route_id="goals", route_reason="Blank", comment="Blank"), plan, "")


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["方法正确时最高6分", "只要方法正确最低6分", "有特殊情况时同源错误不重复扣分"])
async def test_truly_conditional_policies_retain_source_without_another_call(suffix):
    rule = "每错扣1分"
    provider = raw_provider(candidate(rule))
    source = rule+"；"+suffix
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), provider)
    assert plan.routes[0].mode == "holistic"
    assert plan.compilation_status == "holistic"
    assert plan.compilation_reason.startswith("unsupported_")
    assert source in plan.effective_criterion
    assert len(provider.messages) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["同源错误不重复扣分", "同一错误只扣一次",
    "同一根因造成的后续错误不重复扣分", "do not deduct twice for the same error"])
async def test_explicit_error_policy_separate_from_numeric_amount(policy):
    rule = "每错扣1分"
    plan = await rubric.prepare_math_rubric_plan(question(criterion=rule+","+policy),
                                               raw_provider(candidate(rule, error_policy=policy)))
    assert plan.routes[0].mode == "deductions"
    assert plan.routes[0].error_policy == "no_repeat_deduction"
    assert plan.routes[0].error_policy_source_quote == policy


@pytest.mark.asyncio
async def test_unique_math_format_match_restores_original_quote():
    original = r"每次错误使用\(x^2=1\)扣1分"
    result = candidate("每次错误使用$x^2=1$扣1分")
    plan = await rubric.prepare_math_rubric_plan(question(criterion=original), raw_provider(result))
    assert plan.routes[0].mode == "deductions"
    assert plan.routes[0].items[0].source_quote == original
    location, = plan.source_locations
    assert (location.route_id, location.rule_id, location.kind) == ("goals", "major", "item")
    assert original[location.start:location.end] == original


@pytest.mark.asyncio
async def test_preparation_and_correction_never_see_student_fields():
    rule = "每错扣1分"
    provider = raw_provider(candidate(rule, 9), candidate(rule))
    plan = await rubric.prepare_math_rubric_plan(question(criterion=rule, student_answer="PRIVATE_ANSWER"), provider)
    assert plan.routes[0].mode == "deductions"
    assert all("PRIVATE_ANSWER" not in message.content for batch in provider.messages for message in batch)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["每错扣壹分", "每错扣一万分", "deduct several points for each error",
    "每错扣-1分", "每错扣一百一分"])
async def test_unsupported_or_ambiguous_explicit_amount_cannot_be_bypassed_by_ai_label(source):
    bad = candidate("", 9, occurrences=1, source="ai")
    plan = await rubric.prepare_math_rubric_plan(question(criterion=source), raw_provider(bad, bad))
    assert plan.routes[0].mode == "holistic"
    assert source in plan.effective_criterion


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", ["每错最多扣2分", "deduct up to 2 points for each error"])
async def test_variable_deduction_cap_does_not_become_fixed_rate(rule):
    provider = raw_provider(candidate(rule, 2))
    plan = await rubric.prepare_math_rubric_plan(question(criterion=rule), provider)
    assert plan.routes[0].mode == "holistic"
    assert plan.compilation_reason == "unsupported_score_cap"
    assert len(provider.messages) == 0
