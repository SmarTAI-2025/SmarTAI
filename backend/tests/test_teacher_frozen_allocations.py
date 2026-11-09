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
