import json

import pytest

from backend.services.rubric_drafts import (
    RubricAllocationError, RubricDefinition, bind_draft, compile_parts_draft, generated_rubric, saved_draft,
)
from backend.skills.part_scoring import PartDecision, settle_parts
from backend.tests.test_frozen_part_rubric import _factorization_score
from backend.tests.test_question_material_format_repair import Provider
from backend.tests.test_teacher_frozen_allocations import make_row, proposed


def freeze(text, raw, **fields):
    row = {**make_row(text), **fields}
    _, draft = generated_rubric(row, RubricDefinition(part_rubric=raw))
    return draft.plan.part_rubric


def vote(**changes):
    base = dict(part_id="p", target_quote="part:p", student_quote="student:1", method="reference",
                relevance="partial", progress="列式并计算", steps=[
                    dict(id=key, outcome="earned", evidence="student:1", reason=key) for key in ("eq", "val")
                ])
    return PartDecision(**{**base, **changes})


@pytest.mark.parametrize("stem", ["不要求使用配方法求根。", "请用任意方法解方程。"])
def test_open_stem_does_not_impose_reference_method(stem):
    assert _factorization_score(freeze("", proposed(), stem=stem)) == 10


def test_alternative_ceiling_respects_negation_and_explicit_full_value():
    text = "其他解法不应给予满分"
    with pytest.raises(RubricAllocationError, match="below full"):
        freeze(text, proposed())
    raw = proposed()
    raw["parts"][0]["alternative"]["maximum"] = {"points": 8}
    assert _factorization_score(freeze(text, raw)) == 8
    assert _factorization_score(freeze("其他解法满分为10分", raw)) == 10


def test_teacher_per_error_rate_cannot_gain_a_candidate_loss_cap():
    raw = proposed()
    part = raw["parts"][0]
    part.pop("steps")
    part["deductions"] = [dict(id="err", criterion="每个错误", deduct_pts=1, max_deduction=.5)]
    with pytest.raises(RubricAllocationError, match="cumulative"):
        freeze("每错扣1分", raw)
    raw = proposed()
    raw["parts"][0]["alternative"].update(max_deduction=.5, field_sources={"max_deduction": "每错扣1分"})
    alt = freeze("其他正确解法每错扣1分", raw).parts[0].alternative
    assert alt.deduct_per_error == 1 and alt.max_deduction is None


def test_step_minimum_never_pays_for_unrelated_work():
    raw = proposed()
    text = "列式至少给3分"
    raw["nonblank_floor"] = {"pts": 3}
    with pytest.raises(RubricAllocationError, match="floor"):
        freeze(text, raw)
    raw["nonblank_floor"] = {"pts": 0}
    rules = freeze(text, raw)
    assert _factorization_score(rules, student_quote="今天天气好", relevance="unrelated") == 0
    raw["nonblank_floor"] = {"pts": 3}
    with pytest.raises(RubricAllocationError, match="floor"):
        freeze("若列式正确，至少给3分", raw)


def test_explicit_subtotal_spends_only_its_children():
    raw = proposed()
    raw["parts"][0]["steps"][:1] = [dict(id="var", criterion="设未知数", total_pts=1),
                                     dict(id="eq", criterion="建立方程", total_pts=2)]
    text = "列式3分（其中设未知数1分、建立方程2分），计算结果7分"
    assert [s.total_pts for s in freeze(text, raw).parts[0].steps] == [1, 2, 7]
    with pytest.raises(RubricAllocationError):
        freeze(text.replace("其中设未知数1分", "其中设未知数2分"), raw)


def test_every_downstream_step_keeps_its_own_fraction():
    raw = proposed()
    steps = raw["parts"][0]["steps"] = [
        dict(id=key, total_pts=pts, criterion=key) for key, pts in (("a",2), ("b",4), ("c",4))]
    steps[0]["follow_through_keep"] = .5
    text = "后续各步骤沿用错值仍保留一半分"
    with pytest.raises(RubricAllocationError, match="follow-through"):
        freeze(text, raw)
    for step in steps[1:]:
        step["follow_through_keep"] = .5
    rules = freeze(text, raw)
    readings = [dict(id="a", outcome="missed", evidence="student:1", reason="算错"),
                *(dict(id=key, outcome="follow_through", evidence=f"student:{line}",
                       reason="后续推导正确", error_source="首步错值")
                  for key, line in (("b",2), ("c",3)))]
    assert settle_parts(rules, [vote(steps=readings)], "首步错值\n沿用该值\n代入计算", rules.stem)[0] == 4


def test_conditional_result_is_decided_independently_of_step_outcome():
    text = "列式3分、计算结果7分。结果必须写单位，未写单位时计算结果不得分"
    rules = freeze(text, proposed())
    assert rules.parts[0].steps[1].required_conditions == ("未写单位时计算结果不得分",)
    with pytest.raises(ValueError, match="judge every required"):
        settle_parts(rules, [vote()], "v=3", rules.stem)
    assert settle_parts(rules, [vote(condition_results={"val": [False]})], "v=3", rules.stem)[0] == 3
    assert settle_parts(rules, [vote(condition_results={"val": [True]}, relevance="complete")],
                        "v=3 m/s", rules.stem)[0] == 10
    assert freeze(text.replace("时计算结果", "时对应项"), proposed()).teacher_gaps


@pytest.mark.parametrize("lost,expected", [(1,9), (12,0)])
def test_mixed_part_deducts_only_explicit_penalties_after_earned_steps(lost, expected):
    raw = proposed()
    raw["parts"][0]["deductions"] = [dict(id="unit", criterion="漏单位", deduct_pts=lost)]
    text = f"列式3分、计算结果7分，漏单位扣{lost}分"
    rules = freeze(text, raw)
    event = dict(id="e", rule_id="unit", evidence="student:1", origin_quote="v=3", reason="漏单位")
    score, rows = settle_parts(rules, [vote(errors=[event])], "v=3", rules.stem)
    assert score == sum(row.score for row in rows) == expected
    with pytest.raises(RubricAllocationError, match="explicit teacher"):
        freeze("列式3分、计算结果7分", raw)


@pytest.mark.asyncio
async def test_new_setup_notes_freeze_separately_from_the_editable_question():
    row = make_row("列式3分、计算结果7分")
    _, original = generated_rubric(row, RubricDefinition(part_rubric=proposed()))
    row["rubric_draft"] = original.model_dump(mode="json")
    setup = {"teacher_notes": "其他正确解法最多5分、非空作答保底2分"}
    provider = Provider([json.dumps({"part_rubric": proposed()})])
    draft = saved_draft(row, grading_setup=setup) or await compile_parts_draft(row, provider, grading_setup=setup)
    rules = bind_draft(draft, row, grading_setup=setup).part_rubric
    assert _factorization_score(rules) == 5
    assert _factorization_score(rules, relevance="unrelated", student_quote="今天天气好") == 2
    assert _factorization_score(saved_draft(row).plan.part_rubric) == 10
    assert saved_draft(row, grading_setup={"teacher_notes": "第2题：其他正确解法最多1分"}) == original


@pytest.mark.parametrize("custom_alternative", [False, True])
def test_punctuation_only_stem_edit_preserves_funded_steps_and_teacher_override(custom_alternative):
    from backend.db import assignment_repository
    from backend.services import task_facade
    from backend.services.math_grading_context import problem_from_question
    from backend.tests.test_major_question_structure import _seed_task_with_two_part_question

    owner, task = "punctuation-owner", "punctuation-task"
    _seed_task_with_two_part_question(owner, task)
    record, = assignment_repository.list_questions(task, teacher_id=owner)
    presentation = record.source["presentation"]
    structure = presentation["question_structure"]
    # Reading only the absolute amounts from this text would total 7/10.
    text = "(a) 列式占25%、计算结果3分。\n(b) 计算结果4分，其余给列式。"
    parts = []
    for i, (subpart, points) in enumerate(zip(structure["subparts"], ((1,3), (2,4)))):
        part = proposed(points=points)["parts"][0]
        parts.append({**part, "id": f"p{i}", "label": subpart["label"], "stem": subpart["stem"],
                      "total_pts": sum(points)})
    row = {**make_row(text), "stem": record.stem, "reference_answer": record.reference_answer,
           "question_structure": structure}
    _, draft = generated_rubric(row, RubricDefinition(part_rubric={**proposed(), "parts": parts}))
    assignment_repository.update_question(task, teacher_id=owner, q_id=record.q_id, expected_version=record.version,
        criterion=text, source={"presentation": {**presentation, "rubric_draft": draft.model_dump(mode="json")}})
    args = dict(task_id=task, owner_id=owner, q_id=record.q_id)
    if custom_alternative:
        task_facade.update_problem(**args, patch={"alternative_rule_edits": {"p0": "另解最高一半分，每错扣0.5分"}})
    result = task_facade.update_problem(**args, patch={"stem": record.stem.replace("完成计算。", "完成计算？")})
    stored, = assignment_repository.list_questions(task, teacher_id=owner)
    reloaded = saved_draft(problem_from_question(stored)).plan.part_rubric
    assert reloaded.parts[0].stem.endswith("？")
    assert [[s.total_pts for s in part.steps] for part in reloaded.parts] == [[1,3],[2,4]]
    policy = reloaded.parts[0].alternative
    assert (policy.maximum.resolve(4), policy.deduct_per_error) == ((2, .5) if custom_alternative else (4, 2))
    assert result["problem"]["rubric_point_summary"]["is_valid"]


def test_reference_edit_compiles_and_displays_the_committed_new_version(monkeypatch):
    from backend.db import assignment_repository
    from backend.services import task_facade
    from backend.services.math_grading_context import problem_from_question
    from backend.tests.test_alternative_route import _edit_provider
    from backend.tests.test_frozen_part_rubric import _persist_method_question

    args = _persist_method_question()
    def read():
        stored, = assignment_repository.list_questions(args["task_id"], teacher_id=args["owner_id"])
        return saved_draft(problem_from_question(stored)).plan
    old = read()
    raw = old.part_rubric.model_dump(mode="json")
    raw["parts"][0]["steps"][0]["criterion"] = "按新标答配方求两根"
    provider = _edit_provider(monkeypatch, [json.dumps({"part_rubric": raw})])
    reference = "完成平方后有两个根，分别为 1 和 2。"
    result = task_facade.update_problem(**args, patch={"reference_answer": reference})["problem"]
    new = read()
    assert new.plan_id != old.plan_id and len(provider.calls) == 1
    assert new.adopted_material.reference_answer == result["reference_answer"] == reference
    assert new.part_rubric.rubric_version == result["rubric_version"] == old.part_rubric.rubric_version + 1
    assert new.part_rubric.parts[0].steps[0].criterion in result["effective_criterion"]


def test_inline_second_reference_solution_but_not_a_check():
    from backend.services.reference_strategy import validate_generated_reference
    first = "因式分解得两根为 1 和 2。"
    with pytest.raises(ValueError, match="one worked reference"):
        validate_generated_reference("求解方程。", {}, first + "也可以用求根公式求解，得到相同的两根。")
    validate_generated_reference("求解方程。", {}, first + "代回原方程验证两根。")
