"""Source constraints retain their target, monetary role and counting unit."""
from copy import deepcopy

import pytest

from backend.services.rubric_drafts import RubricAllocationError, RubricDefinition, generated_rubric
from backend.skills.part_scoring import PartDecision, settle_parts
from backend.tests.test_teacher_frozen_allocations import make_row, proposed


def freeze(text, raw):
    return generated_rubric(make_row(text), RubricDefinition(part_rubric=raw))[1].plan.part_rubric


def penalty_proposal(*, limit=None):
    raw = proposed()
    part = raw["parts"][0]
    part.pop("steps")
    part["deductions"] = [dict(id="error", criterion="独立错误", deduct_pts=1, limit=limit)]
    return raw


@pytest.mark.parametrize("text", ["每错扣1分", "每个独立错误扣1分", "Deduct 1 point per independent error"])
def test_model_cannot_silently_cap_teacher_error_count(text):
    with pytest.raises(RubricAllocationError, match="count|次数"):
        freeze(text, penalty_proposal(limit=1))
    rules = freeze(text, penalty_proposal())
    decision = PartDecision(part_id="p", target_quote="part:p", student_quote="student:1",
        method="reference", relevance="partial", progress="两处独立错误", errors=[
            dict(id="e1", rule_id="error", evidence="student:1", origin_quote="x=4", reason="第一处错误"),
            dict(id="e2", rule_id="error", evidence="student:2", origin_quote="y=5", reason="第二处错误"),
        ])
    assert settle_parts(rules, [decision], "x=4\ny=5", rules.stem)[0] == 8


@pytest.mark.parametrize("text", ["每错扣1分，最多计2次", "每错扣1分，最多2次", "Deduct 1 point per error, at most 2 times"])
def test_explicit_count_limit_is_preserved_and_cannot_be_omitted(text):
    assert freeze(text, penalty_proposal(limit=2)).parts[0].deductions[0].limit == 2
    for limit in (None, 1, 3):
        with pytest.raises(RubricAllocationError, match="count|次数"):
            freeze(text, penalty_proposal(limit=limit))


def test_count_limit_is_bound_to_one_of_two_equal_rate_rules():
    text = "每个计算错误扣1分。每个单位错误扣1分，最多计2次"
    raw = penalty_proposal()
    raw["parts"][0]["deductions"] = [
        dict(id="arithmetic", criterion="每个计算错误", deduct_pts=1, source_quote="每个计算错误扣1分"),
        dict(id="units", criterion="每个单位错误", deduct_pts=1, limit=2, source_quote="每个单位错误扣1分"),
    ]
    rules = freeze(text, raw)
    assert [rule.limit for rule in rules.parts[0].deductions] == [None, 2]
    wrong = deepcopy(raw)
    wrong["parts"][0]["deductions"][0]["limit"] = 2
    wrong["parts"][0]["deductions"][1]["limit"] = None
    with pytest.raises(RubricAllocationError, match="count"):
        freeze(text, wrong)


def test_per_target_uniqueness_is_not_a_one_error_cap_for_the_part():
    from backend.tests.test_frozen_part_rubric import fixture, proposed_rates, question_row

    raw = proposed_rates(fixture()["questions"][0])
    row = question_row(raw)
    row["criterion"] = "第三问满分4分，每个错误的函数值扣2分，每个函数值最多扣一次。"
    rules = generated_rubric(row, RubricDefinition(part_rubric=raw))[1].plan.part_rubric
    assert rules.parts[2].deductions[0].count_by == "target_item"
    assert rules.parts[2].deductions[0].limit is None
    raw["parts"][2]["deductions"][0]["limit"] = 1
    with pytest.raises(RubricAllocationError, match="per-target"):
        generated_rubric(row, RubricDefinition(part_rubric=raw))


@pytest.mark.parametrize("names,clause", [
    (("列式", "计算结果"), "计算结果沿用错值保留一半分"),
    (("建立能量关系", "求得速度"), "求得速度沿用前面的错误数值保留一半分"),
    (("mass balance", "concentration"), "concentration: follow-through retains 50%"),
])
def test_follow_through_fraction_belongs_to_named_step(names, clause):
    raw = proposed(names=names, points=(2, 8))
    text = f"{names[0]}2分，{names[1]}8分。{clause}"
    raw["parts"][0]["steps"][0]["follow_through_keep"] = .5
    with pytest.raises(RubricAllocationError, match="follow-through"):
        freeze(text, raw)
    raw["parts"][0]["steps"][0].pop("follow_through_keep")
    raw["parts"][0]["steps"][1]["follow_through_keep"] = .5
    rules = freeze(text, raw)
    assert rules.parts[0].steps[1].follow_through_award() == 4


def test_follow_through_uses_authoritative_anchor_when_model_paraphrases_names():
    raw = proposed(names=("建模", "数值答案"), points=(2, 8), quotes=("列式2分", "计算结果8分"))
    raw["parts"][0]["steps"][1]["follow_through_keep"] = .5
    rules = freeze("列式2分，计算结果8分。计算结果沿用错值保留一半分", raw)
    assert rules.parts[0].steps[1].follow_through_award() == 4
    wrong = deepcopy(raw)
    wrong["parts"][0]["steps"][0]["follow_through_keep"] = .5
    wrong["parts"][0]["steps"][1].pop("follow_through_keep")
    with pytest.raises(RubricAllocationError, match="follow-through"):
        freeze("列式2分，计算结果8分。计算结果沿用错值保留一半分", wrong)


@pytest.mark.parametrize("intro", ["", "其中", "其中："])
@pytest.mark.parametrize("separator", ["，", "、", "; "])
def test_parenthetical_subtotal_funds_only_its_children(intro, separator):
    raw = proposed()
    raw["parts"][0]["steps"][:1] = [dict(id="var", criterion="设未知数", total_pts=1),
                                     dict(id="eq", criterion="建立方程", total_pts=2)]
    text = f"列式3分（{intro}设未知数1分{separator}建立方程2分），计算结果7分"
    rules = freeze(text, raw)
    assert [step.total_pts for step in rules.parts[0].steps] == [1, 2, 7]
    with pytest.raises(RubricAllocationError):
        freeze(text.replace("设未知数1分", "设未知数2分"), raw)


@pytest.mark.parametrize("phrase", ["非空作答最低得2分", "非空作答最低给2分", "最低给2分", "非空作答至少给2分"])
def test_nonblank_minimum_does_not_disappear_when_award_verb_changes(phrase):
    raw = proposed()
    raw["nonblank_floor"] = {"pts": 2}
    assert freeze(phrase, raw).nonblank_floor.resolve(10) == 2
    raw["nonblank_floor"] = {"pts": 0}
    with pytest.raises(RubricAllocationError, match="floor"):
        freeze(phrase, raw)


@pytest.mark.parametrize("text", ["列式最低给2分", "若列式正确，最低给2分"])
def test_conditional_minimum_does_not_become_question_floor(text):
    raw = proposed()
    assert freeze(text, raw).nonblank_floor.resolve(10) == 0
    raw["nonblank_floor"] = {"pts": 2}
    with pytest.raises(RubricAllocationError, match="floor"):
        freeze(text, raw)


def test_unresolved_explicit_nonblank_minimum_is_not_equivalent_to_no_minimum():
    from backend.services.teacher_point_allocations import TeacherPolicyUnresolved

    with pytest.raises(TeacherPolicyUnresolved, match="floor"):
        freeze("非空作答最低分按满分的五分之一计算", proposed())


def test_unresolved_policies_keep_full_condition_and_exact_candidate_identity():
    from backend.services.part_rubric import PartRubric, verify_teacher_numbers
    from backend.services.teacher_point_allocations import TeacherPolicyUnresolved

    text = "若沿用前一步算错的结果，求最终数值这一步保留一半分。非空作答最低分按满分的五分之一计算"
    raw = proposed(names=("建立模型", "读取末值"), points=(2, 8))
    raw["parts"][0]["steps"][1]["follow_through_keep"] = .5
    raw["nonblank_floor"] = {"pts": 2}
    rules = PartRubric.model_validate(raw)
    with pytest.raises(TeacherPolicyUnresolved) as caught:
        verify_teacher_numbers(text, rules, 10)
    issues = caught.value.issues
    assert {item["type"] for item in issues} == {"follow_through", "nonblank_floor"}
    follow, = [item for item in issues if item["type"] == "follow_through"]
    assert follow["source_quote"] == "若沿用前一步算错的结果，求最终数值这一步保留一半分"
    assert [item["follow_through_pts"] for item in follow["candidates"]] == [0, 4]
    verified = {item["policy_id"] for item in issues}
    verify_teacher_numbers(text, rules, 10, verified_policy_ids=verified)
    raw["parts"][0]["steps"][1]["follow_through_keep"] = .25
    with pytest.raises(TeacherPolicyUnresolved) as changed:
        verify_teacher_numbers(text, PartRubric.model_validate(raw), 10, verified_policy_ids=verified)
    assert len(changed.value.issues) == 1
    assert changed.value.issues[0]["policy_id"] not in verified
    # A different unknown source or amount never inherits a previous approval.
    with pytest.raises(TeacherPolicyUnresolved):
        verify_teacher_numbers(text.replace("五分之一", "四分之一"), rules, 10, verified_policy_ids=verified)
    # Approval of semantic ambiguity does not waive deterministic source facts.
    with pytest.raises(ValueError, match="teacher allocation"):
        verify_teacher_numbers(text + "。建立模型3分", rules, 10, verified_policy_ids=verified)
