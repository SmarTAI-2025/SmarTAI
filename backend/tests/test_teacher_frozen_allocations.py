import json

import pytest

from backend.services.rubric_drafts import RubricAllocationError, RubricDefinition, generated_rubric, saved_draft
from backend.skills.part_scoring import PartDecision, settle_parts


def make_row(text):
    return dict(q_id="q1", number="1", type="计算题", stem="求未知数。", reference_answer="先列式，再计算。",
                max_score=10, criterion=text)


def proposed(names=("列式", "计算结果"), points=(3, 7), quotes=("", "")):
    return {"id": "q1", "stem": "求未知数。", "label": "1", "total_pts": 10, "parts": [{
        "id": "p", "total_pts": 10, "alternative": {"deduct_per_error": 2},
        "steps": [{"id": key, "criterion": name, "total_pts": pts, "source_quote": quote}
                  for key, name, pts, quote in zip(("eq","val"),names,points,quotes)]
    }]}


@pytest.mark.parametrize("text,names,points", [
    ("列式3分、计算结果7分", ("列式", "计算结果"), (3, 7)),
    ("列式三分和计算结果七分", ("列式", "计算结果"), (3, 7)),
    ("列式占3分而计算结果占7分。", ("列式", "计算结果"), (3, 7)),
    ("列式最多3分，计算结果最多7分", ("列式", "计算结果"), (3, 7)),
    ("列式占总分30%，计算结果占总分70%", ("列式", "计算结果"), (3, 7)),
    ("建立模型2.5分验证边界7.5分", ("建立模型", "验证边界"), (2.5, 7.5)),
    ("列式3分（允许等价式）；计算结果7分（须给单位）", ("列式", "计算结果"), (3, 7)),
    ("假设与验证分别占三和七分", ("假设", "验证"), (3, 7)),
    ("Award 3 points for the equation and 7 points for the result",
     ("the equation", "the result"), (3, 7)),
    ("Write the equation: 2.5 points; numerical result: 7.5 points",
     ("Write the equation","numerical result"), (2.5,7.5)),
    ("提出假设五分；验证假设五分", ("提出假设","验证假设"), (5,5)),
])
def test_teacher_awards_follow_the_named_condition_through_settlement(text,names,points):
    row = make_row(text)
    for wrong in [(9,1), tuple(reversed(points))]:
        if wrong != points:
            with pytest.raises(RubricAllocationError, match="requires"):
                generated_rubric(row, RubricDefinition(part_rubric=proposed(names,wrong)))
    _, draft = generated_rubric(row, RubricDefinition(part_rubric=proposed(names,points)))
    vote = PartDecision(part_id="p", target_quote="part:p", student_quote="student:1", progress="完成第一项",
        method="reference", relevance="partial", steps=[
        dict(id="eq", outcome="earned", evidence="student:1", reason="已完成第一项"),
        dict(id="val", outcome="missed", evidence="student:1", reason="遗漏第二项")])
    assert settle_parts(draft.plan.part_rubric,[vote],"x+1=2",row["stem"])[0] == points[0]
    assert all(s.source_quote for s in draft.plan.part_rubric.parts[0].steps)
    assert saved_draft({**row, "rubric_draft": draft.model_dump(mode="json")}) is not None


def test_quotes_cannot_swap_roles_or_spend_the_same_teacher_award_twice():
    row=make_row("列式3分、计算结果7分")
    with pytest.raises(RubricAllocationError):
        generated_rubric(row,RubricDefinition(part_rubric=proposed(points=(7,3),quotes=("计算结果7分","列式3分"))))
    raw=proposed(names=("列出等量关系","求得答案"),quotes=("列式3分","计算结果7分"))
    _,draft=generated_rubric(row,RubricDefinition(part_rubric=raw))
    assert [s.criterion for s in draft.plan.part_rubric.parts[0].steps]==["列式","计算结果"]
    raw=proposed(names=("建立关系","补足配分"),quotes=("列式3分、计算结果7分",""))
    with pytest.raises(RubricAllocationError):
        generated_rubric(row,RubricDefinition(part_rubric=raw))


@pytest.mark.asyncio
async def test_preexisting_wrong_allocations_are_repaired_before_a_new_grade():
    from backend.services.math_grading_context import MATH_GRADING_ENGINE_VERSION, plans_from_snapshots
    from backend.services.math_rubric_plan import _content_hash
    from backend.services.rubric_drafts import compile_parts_draft
    from backend.tests.test_question_material_format_repair import Provider
    row=make_row("列式3分、计算结果7分")
    _,draft=generated_rubric(row,RubricDefinition(part_rubric=proposed()))
    historic=draft.model_dump(mode="json")
    body=historic["plan"]
    for step,pts in zip(body["part_rubric"]["parts"][0]["steps"],(9,1)):
        step["total_pts"]=float(pts)
    body["plan_id"]=_content_hash({k:v for k,v in body.items() if k!="plan_id"})
    manifest={"math_grading_engine_version":MATH_GRADING_ENGINE_VERSION,"math_rubric_plans":{"q1":body}}
    row["rubric_draft"]=historic
    assert saved_draft(row) is None and not plans_from_snapshots(manifest,[],for_grading=True)
    assert plans_from_snapshots(manifest,[])["q1"].part_rubric.parts[0].steps[0].total_pts==9
    provider=Provider([json.dumps({"part_rubric":proposed(points=(9,1))}),json.dumps({"part_rubric":proposed()})])
    fixed=await compile_parts_draft(row,provider)
    assert len(provider.calls)==2
    assert "eq" in provider.calls[1][-1].content and "3" in provider.calls[1][-1].content
    assert fixed.plan.part_rubric.parts[0].steps[0].total_pts==3 and historic["plan"]==body


def test_teacher_words_and_full_scoring_projection_are_both_returned():
    from backend.services import task_facade
    from backend.db import assignment_repository
    from backend.tests.test_frozen_part_rubric import _persist_method_question
    args=_persist_method_question()
    record,=assignment_repository.list_questions(args["task_id"],teacher_id=args["owner_id"])
    row=make_row("过程正确即可")
    row.update(q_id=record.q_id,stem=record.stem,reference_answer=record.reference_answer)
    raw=proposed(("建立方程","求出结果"),(4,6))
    _,draft=generated_rubric(row,RubricDefinition(part_rubric=raw))
    record.criterion=row["criterion"]
    response=task_facade._serialize_problem(record,math_plan=draft.plan)
    assert response["criterion"]=="过程正确即可"
    assert all(s in response["effective_criterion"] for s in ("建立方程","4分","求出结果","6分","保底","折算"))
    assert response["alternative_rules"][0]["text"]


@pytest.mark.asyncio
async def test_generation_repairs_two_parallel_methods_for_one_target():
    from backend.agents.ingest_agent import generate_missing_question_materials
    from backend.tests.test_question_material_format_repair import Provider
    target={"q_id":"q1","target":"reference_answer","target_id":"q1:reference_answer"}
    def reply(content):
        return json.dumps({"candidates":[{**target,"text_value":content,"confidence":1.0}]})
    single="配方得根为 1 和 2。"
    provider=Provider([reply("方法一：配方得根。\n方法二：因式分解得根。"),reply(single)])
    result=await generate_missing_question_materials({"q1":{"stem":"求方程的根。","type":"计算题","max_score":10}},[target],6,provider)
    assert len(provider.calls)==2 and result[0].text_value==single


def test_reference_methods_are_scoped_and_expressly_requested_methods_survive():
    from backend.services.reference_strategy import validate_generated_reference
    structure={"shared_stem":"完成两问。","subparts":[{"label":"(1)","stem":"求根。"},{"label":"(2)","stem":"用两种方法证明结论。"}]}
    validate_generated_reference("",structure,"(1) 方法一：配方。\n(2) 方法一：归纳。\n方法二：反证。")
    with pytest.raises(ValueError):
        validate_generated_reference("",structure,"(1) 方法一：配方。\n方法二：分解。\n(2) 证明略。")
    validate_generated_reference("Prove both directions.",None,"Necessity: apply the definition.\nSufficiency: reverse the implication.")


def test_equal_named_awards_remain_separate_across_parts():
    from backend.services.part_rubric import PartRubric, bind_parts
    text="（1）列式2分，计算结果3分。（2）列式2分，计算结果3分。"
    labels=["(1)","(2)"]
    parts=[]
    for idx,label in enumerate(labels):
        part=proposed(points=(2,3))["parts"][0]
        part.update(id=f"p{idx}",label=label,stem="求解",total_pts=5)
        parts.append(part)
    row=make_row(text)
    row["question_structure"]={"subparts":[{"label":label,"stem":"求解"} for label in labels]}
    raw=proposed()
    raw["parts"]=parts
    rules=bind_parts(PartRubric.model_validate(raw),row)
    assert [[s.total_pts for s in p.steps] for p in rules.parts]==[[2,3],[2,3]]
    parts[1]["steps"][0]["total_pts"], parts[1]["steps"][1]["total_pts"]=3,2
    with pytest.raises(ValueError,match="p1/eq"):
        bind_parts(PartRubric.model_validate(raw),row)


def test_one_teacher_award_may_fund_several_distinct_substeps():
    row = make_row("列式3分、计算结果7分")
    raw = proposed()
    items = [
        dict(id="var", criterion="设未知数", total_pts=1, source_quote="列式3分"),
        dict(id="eq", criterion="写出等量关系", total_pts=2, source_quote="列式3分"),
        raw["parts"][0]["steps"][1],
    ]
    raw["parts"][0]["steps"] = items
    _, draft = generated_rubric(row, RubricDefinition(part_rubric=raw))
    rules = draft.plan.part_rubric
    assert [(s.criterion, s.total_pts) for s in rules.parts[0].steps[:2]] == [
        ("列式 · 设未知数", 1), ("列式 · 写出等量关系", 2)]
    assert saved_draft({**row,"rubric_draft":draft.model_dump(mode="json")}) is not None
    items[1]["total_pts"], items[2]["total_pts"] = 4, 5
    with pytest.raises(RubricAllocationError, match="allocated 3"):
        generated_rubric(row, RubricDefinition(part_rubric=raw))


@pytest.mark.asyncio
async def test_editing_ordinary_allocations_preserves_the_unedited_alternative_policy():
    from backend.services.rubric_drafts import compile_parts_draft
    from backend.tests.test_question_material_format_repair import Provider

    old = make_row("列式3分、计算结果7分")
    _, prior = generated_rubric(old, RubricDefinition(part_rubric=proposed()))
    revised = {**old, "criterion": "列式占4分而计算结果占6分"}
    proposal = proposed(points=(4, 6))
    proposal["parts"][0]["alternative"] = {"maximum": {"points": 6}, "deduct_per_error": 1}
    reply = json.dumps({"part_rubric": proposal})
    updated = await compile_parts_draft(revised, Provider([reply]), previous=prior)
    part, = updated.plan.part_rubric.parts
    assert [s.total_pts for s in part.steps] == [4, 6]
    assert part.alternative == prior.plan.part_rubric.parts[0].alternative

    revised["criterion"] += "。其他解法最多给6分，每错扣1分。"
    edited = await compile_parts_draft(revised, Provider([reply]), previous=prior)
    alt = edited.plan.part_rubric.parts[0].alternative
    assert alt.maximum.resolve(10) == 6 and alt.deduct_per_error == 1


def test_frozen_reference_is_bound_after_sibling_generation_and_teacher_segments_need_sources():
    from backend.services.rubric_drafts import retarget_draft
    from backend.services.math_grading_context import get_math_plan, math_plans_context, scoring_plan
    from backend.domain.errors import ValidationError
    from backend.models import ProblemInfo

    row = make_row("列式3分、计算结果7分")
    raw = proposed()
    raw["answer_segments"] = [{"id": "a1", "origin": "teacher", "text": row["reference_answer"]}]
    raw["parts"][0]["steps"][0]["answer_refs"] = ["a1"]
    _, draft = generated_rubric(row, RubricDefinition(part_rubric=raw))
    assert draft.plan.adopted_material.stem == row["stem"]
    assert draft.plan.adopted_material.reference_answer == row["reference_answer"]
    problem = ProblemInfo(**row)
    with math_plans_context({"q1": draft.plan}):
        assert get_math_plan(problem)
        changed = problem.model_copy(update={"reference_answer": "改变后的标答"})
        assert get_math_plan(changed) is None
        with pytest.raises(ValidationError, match="frozen question"):
            scoring_plan(changed, None)
    with pytest.raises(RubricAllocationError, match="a1"):
        retarget_draft(draft, {**row, "reference_answer": "改用配方。"})
    raw["answer_segments"][0]["text"] = "并不存在的教师原话"
    with pytest.raises(RubricAllocationError, match="a1"):
        generated_rubric(row, RubricDefinition(part_rubric=raw))
    raw["answer_segments"][0]["text"] = "x=1"
    with pytest.raises(RubricAllocationError, match="a1"):
        generated_rubric({**row, "reference_answer": "x=10"}, RubricDefinition(part_rubric=raw))


def test_one_result_uses_adopted_answer_without_changing_other_parts_or_teacher_rules():
    from backend.agents.incremental_question_preparation import _rows

    row = _rows([dict(number="7", type="计算题", max_score=10,
        stem="完成下列各问。\n(1) 只写最终结果。\n(2) 证明恒等式。",
        reference_answer="(1) 结果为 4。\n(2) 展开可得。")])["q1"]
    raw = proposed()
    raw["parts"] = [dict(id=f"p{i}", label=f"({i})", total_pts=5, alternative={"deduct_per_error": 1},
         steps=[dict(id="s", total_pts=5, criterion=desc)])
         for i, desc in [(1,"结果为 7。"), (2, "展开两边并建立等式。")]]
    _, draft = generated_rubric(row, RubricDefinition(part_rubric=raw))
    a, b = draft.plan.part_rubric.parts
    assert "标答" in a.steps[0].criterion and "7" not in a.steps[0].criterion
    assert b.steps[0].criterion == raw["parts"][1]["steps"][0]["criterion"]
    row["criterion"] = "第一问接受写成 4 或 4.0。"
    raw["parts"][0]["steps"][0]["criterion"] = "接受写成 4 或 4.0。"
    _, provided = generated_rubric(row, RubricDefinition(part_rubric=raw))
    assert provided.plan.part_rubric.parts[0].steps[0].criterion == "接受写成 4 或 4.0。"
    row = _rows([dict(number="8", type="计算题", max_score=10,
                     stem="试确定标记语句的执行次数，并给出计算过程。", reference_answer="逐次计数。")])["q1"]
    raw = proposed()
    raw["parts"][0]["steps"] = [{"id": "s", "criterion": "列出循环边界并计算。", "total_pts": 10}]
    _, working = generated_rubric(row, RubricDefinition(part_rubric=raw))
    assert working.plan.part_rubric.parts[0].steps[0].criterion == "列出循环边界并计算。"


@pytest.mark.parametrize("teacher,partial_allowed,relevance,flag", [
    (False, True, "partial", True), (True, True, "partial", False),
    (False, False, "partial", True), (False, True, "incorrect", False),
])
def test_zero_with_unfunded_progress_requires_review_only_for_ai_step_rules(teacher, partial_allowed, relevance, flag):
    from backend.agents.multi_expert import _apply_math_review_advisory
    from backend.models import Correction, ExpertResult, ProblemInfo
    from backend.services.math_grading_context import math_plans_context
    from backend.services.rubric_drafts import bind_draft
    from backend.skills.math_grading import MathAssessment, TRACE_VERSION

    row = make_row("列式3分、计算结果7分" if teacher else "")
    criterion, draft = generated_rubric(row, RubricDefinition(part_rubric=proposed()))
    row["criterion"] = criterion
    if not teacher:
        row["ai_completion_provenance"] = {"criterion": {"source_kind": "ai_generated"}}
    plan = bind_draft(draft, row, grading_setup={"allow_partial_credit": partial_allowed})
    problem = ProblemInfo(**row)
    problem._math_rubric_source = {"presentation": {"ai_completion_provenance": row.get("ai_completion_provenance", {})}}
    decision = PartDecision(part_id="p", target_quote="part:p", student_quote="student:1", progress="已得到一个中间不等式。",
        method="reference", relevance=relevance, steps=[dict(id=s, outcome="missed", evidence="student:1", reason="未完成本项")
                                                       for s in ("eq", "val")])
    a = MathAssessment(route_id="parts", route_reason="根据原文", part_decisions=[decision], confidence=1, comment="判断完成。")
    raw = json.dumps({"version":TRACE_VERSION, "plan_id":plan.plan_id,"assessment":a.model_dump(mode="json")})
    votes = [ExpertResult(provider=k, score=0, max_score=10, confidence=1, comment="判断完成。", raw_output=raw) for k in ("a","b")]
    correction = Correction(q_id="q1", type="计算题", score=0, max_score=10, confidence=1,
                            comment="判断完成。", steps=[], expert_results=votes)
    with math_plans_context({"q1": plan}):
        result = _apply_math_review_advisory(correction, problem=problem)
    assert result.requires_human_review is flag and result.score == 0
    assert ("math_scoring_uncertainty" in result.review_reasons) is flag


@pytest.mark.asyncio
async def test_edit_compiler_uses_the_same_source_envelope_and_repair_reason_as_generation():
    from backend.services.rubric_drafts import compile_parts_draft
    from backend.tests.test_question_material_format_repair import Provider

    row = make_row("列式占3分而计算结果占7分。其他解法最多给一半，每错扣1分。")
    raw = proposed()
    part = raw.pop("parts")[0]
    raw.update(steps=part["steps"], alternative={"deduct_per_error": 1})
    both = {**raw, "deductions": [{"id":"e", "criterion":"错误", "deduct_pts":1}]}
    provider = Provider([json.dumps({"part_rubric":both}), json.dumps({"part_rubric":raw})])
    draft = await compile_parts_draft(row, provider)
    frozen, = draft.plan.part_rubric.parts
    assert [s.total_pts for s in frozen.steps] == [3, 7]
    assert frozen.alternative.maximum.resolve(10) == 5 and frozen.alternative.deduct_per_error == 1
    initial = json.loads(provider.calls[0][-1].content)
    assert "另" not in initial["teacher_ordinary_rules"] and len(initial["teacher_alternative_rules"]) == 1
    assert len(provider.calls)==2 and "parts" in provider.calls[1][-1].content


def test_question_metadata_has_one_owner_and_only_alternative_wording_cannot_source_main_steps():
    from backend.services.rubric_drafts import source_bound_definition
    from backend.tools.structured_llm import StructuredOutputInvalidError

    row = make_row("其他正确解法最多给一半，每错扣1分。")
    raw = proposed()
    raw["parts"][0]["steps"][0]["source_quote"] = "模型补充的列式步骤"
    wrapped = source_bound_definition({"part_rubric": raw, "teacher_gaps": []}, row)
    assert "teacher_gaps" not in wrapped and wrapped["part_rubric"]["teacher_gaps"] == []
    _, draft = generated_rubric(row, RubricDefinition.model_validate(wrapped))
    part = draft.plan.part_rubric.parts[0]
    assert part.origin_overrides["criterion"] == "ai" and not part.steps[0].source_quote
    assert part.alternative.maximum.resolve(10) == 5 and part.alternative.deduct_per_error == 1
    raw["total_pts"] = 10
    with pytest.raises(StructuredOutputInvalidError, match="Conflicting question fields"):
        source_bound_definition({"part_rubric": raw, "total_pts": 9}, row)


def test_question_floor_is_bound_separately_from_generated_main_conditions():
    from backend.services.rubric_drafts import source_bound_definition
    row = make_row("非空作答保底4分。其他解法每错扣2分。")
    row["stem"] = "必须用配方法求根。"
    raw = proposed()
    raw["nonblank_floor"] = {"pts": 0}
    raw["parts"][0]["steps"][0]["source_quote"] = "来自标答的配方步骤"
    proposal = source_bound_definition({"part_rubric": raw}, row)
    _, draft = generated_rubric(row, RubricDefinition.model_validate(proposal))
    rules = draft.plan.part_rubric
    assert rules.nonblank_floor.resolve(10) == 4
    assert not rules.parts[0].steps[0].source_quote
    assert rules.parts[0].alternative.maximum.resolve(10) == rules.parts[0].alternative.minimum.resolve(10) == 4


@pytest.mark.asyncio
async def test_invalid_json_repair_can_use_the_last_shared_attempt_with_teacher_input():
    from backend.services.rubric_drafts import compile_parts_draft
    from backend.tests.test_question_material_format_repair import Provider
    row = make_row("列式3分、计算结果7分")
    provider = Provider(['{"part_rubric": ,,,}', '{"part_rubric": ,,,}', json.dumps({"part_rubric": proposed()})])
    draft = await compile_parts_draft(row, provider)
    assert len(provider.calls) == 3 and draft.plan.part_rubric.parts[0].steps[1].total_pts == 7
    assert "validation_feedback" in provider.calls[-1][-1].content


@pytest.mark.parametrize("zero", [0, 0.0, "0"])
def test_zero_alternative_bound_has_the_same_value_in_either_unit(zero):
    from backend.services.part_rubric import Amount
    from pydantic import ValidationError
    assert Amount.model_validate(zero).resolve(7) == 0
    with pytest.raises(ValidationError):
        Amount.model_validate(0.5)
