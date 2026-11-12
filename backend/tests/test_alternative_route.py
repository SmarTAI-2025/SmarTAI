"""Use one frozen alternative ledger regardless of the ordinary scoring method."""
import json

import pytest

from backend.services.part_rubric import alternative_text
from backend.services.rubric_drafts import RubricDefinition, generated_rubric, saved_draft
from backend.skills.part_scoring import PartDecision, settle_parts


STEM = "解方程 x²−3x+2=0。"
ANSWER = "因式分解为 (x−1)(x−2)=0。\n第二个根误写为负二。"


def material(*, ordinary="steps", teacher="", alternative=None, stem=STEM):
    main = ({"steps": [{"id": "complete_square", "criterion": "配平方", "total_pts": 10}]}
            if ordinary == "steps" else
            {"deductions": [{"id": "ordinary_error", "criterion": "普通错误", "deduct_pts": 4}]})
    part = {"id": "p", "total_pts": 10, **main}
    if alternative is not None:
        part["alternative"] = alternative
    row = dict(q_id="q1", number="1", type="计算题", stem=stem, max_score=10,
               reference_answer="用配方法解得 1 和 2。", criterion=teacher)
    definition = RubricDefinition(part_rubric=dict(id="q1", label="1", stem=stem, total_pts=10, parts=[part]))
    criterion, draft = generated_rubric(row, definition)
    if not teacher:
        row["ai_completion_provenance"] = {"criterion": {}}
    row.update(criterion=criterion, rubric_draft=draft.model_dump(mode="json"))
    return row, saved_draft(row)


def alternate(relevance="partial", **kwargs):
    return PartDecision.model_validate(dict(
        part_id="p", method="alternative", reference_method="配方", student_method="因式分解",
        target_quote="part:p", student_quote="student:1", progress="核对因式与两根",
        relevance=relevance, **kwargs))


def error(rule_id="alternative", line=2):
    return dict(id=f"e{line}", rule_id=rule_id, evidence=f"student:{line}",
                origin_quote=f"student:{line}", reason="独立符号错误")


@pytest.mark.parametrize("ordinary", ["steps", "deductions"])
@pytest.mark.parametrize("teacher,expected", [("", 8), ("其他解法最高给一半分。", 3),
                                                ("其他解法每错扣1分。", 9)])
def test_ai_rate_survives_binding_and_teacher_overrides_only_owned_fields(ordinary, teacher, expected):
    _, draft = material(ordinary=ordinary, teacher=teacher, alternative={"deduct_per_error": 2})
    rules = draft.plan.part_rubric
    assert rules.parts[0].alternative.deduct_per_error == (1 if "扣1分" in teacher else 2)
    score, rows = settle_parts(rules, [alternate(errors=[error()])], ANSWER, STEM)
    assert score == expected
    assert [(row.part_id, row.item_id, row.score) for row in rows] == [("p", None, expected)]
    assert "独立错误扣" in alternative_text(rules.parts[0])
    with pytest.raises(ValueError):
        settle_parts(rules, [alternate(errors=[error("ordinary_error")])], ANSWER, STEM)
    with pytest.raises(ValueError):
        settle_parts(rules, [alternate(steps=[dict(id="complete_square", outcome="missed",
                                                   evidence="student:2", reason="不是配方法")])], ANSWER, STEM)


def test_new_variable_policy_requires_rate_before_grading_and_fixed_cap_needs_none():
    with pytest.raises(ValueError, match=r"p.*deduct_per_error"):
        material()
    _, fixed = material(teacher="其他解法不得分。")
    assert settle_parts(fixed.plan.part_rubric, [alternate()], ANSWER, STEM)[0] == 0


def test_teacher_minimum_and_caps_settle_independently_of_ai_ordinary_steps():
    _, draft = material(teacher="其他解法最高5分，最低1分，每错扣3分，最多扣2分。",
                        alternative={"deduct_per_error": 2})
    rules = draft.plan.part_rubric
    assert settle_parts(rules, [alternate("complete")], ANSWER, STEM)[0] == 5
    assert settle_parts(rules, [alternate(errors=[error()])], ANSWER, STEM)[0] == 3
    assert settle_parts(rules, [alternate("unrelated")], ANSWER, STEM)[0] == 0
    with pytest.raises(ValueError):
        settle_parts(rules, [alternate(errors=[error(), {**error(), "id": "repeat"}])], ANSWER, STEM)


@pytest.mark.asyncio
async def test_old_preparation_fills_only_missing_rates_and_preserves_old_run():
    from backend.services.rubric_drafts import bind_draft, compile_parts_draft
    from backend.services.math_rubric_plan import _content_hash
    from backend.services.math_grading_context import MATH_GRADING_ENGINE_VERSION, plans_from_snapshots
    from backend.tests.test_question_material_format_repair import Provider

    row, draft = material(teacher="其他方法最高给一半分。", alternative={"deduct_per_error": 2})
    stored = row["rubric_draft"]["plan"]
    stored["part_rubric"]["parts"][0]["alternative"]["deduct_per_error"] = None
    stored["plan_id"] = _content_hash({k: v for k, v in stored.items() if k != "plan_id"})
    snapshot = json.dumps(stored, sort_keys=True)
    manifest = dict(math_grading_engine_version=MATH_GRADING_ENGINE_VERSION, math_rubric_plans={"q1": stored})
    assert not plans_from_snapshots(manifest, [], for_grading=True)
    assert plans_from_snapshots(manifest, [])["q1"].plan_id == stored["plan_id"]
    assert saved_draft(row) is None
    with pytest.raises(ValueError, match="before grading"):
        bind_draft(saved_draft(row, allow_incomplete_alternative=True), row)

    provider = Provider([json.dumps({"deduct_per_error": {"unknown": 4}}),
                         json.dumps({"deduct_per_error": {"p": 2}})])
    upgraded = await compile_parts_draft(row, provider)
    assert len(provider.calls) == 2 and upgraded.plan.plan_id != stored["plan_id"]
    assert upgraded.plan.part_rubric.model_dump(exclude={"rubric_version"}) == draft.plan.part_rubric.model_dump(exclude={"rubric_version"})
    assert upgraded.plan.part_rubric.rubric_version == draft.plan.part_rubric.rubric_version + 1
    assert saved_draft({**row, "rubric_draft": upgraded.model_dump(mode="json")}) == upgraded
    assert json.dumps(stored, sort_keys=True) == snapshot


@pytest.mark.asyncio
async def test_review_switches_route_and_settles_against_same_frozen_policy():
    from backend.models import ProblemInfo, StudentAnswerInfo
    from backend.services.math_grading_context import math_plans_context
    from backend.services.rubric_drafts import bind_draft
    from backend.skills import math_grading as grading
    from backend.tests.test_math_evidence_reliability import raw_provider
    from backend.tests.test_math_grading import expert_for

    row, draft = material(teacher="参考解配方得10分。", alternative={"deduct_per_error": 2})
    problem = ProblemInfo(**row)
    plan = bind_draft(draft, problem)
    answer = StudentAnswerInfo(q_id=problem.q_id, number="1", type=problem.type, content=ANSWER)
    valid = alternate(errors=[error()])
    misclassified = valid.model_copy(update={"method":"reference","relevance":"incorrect","errors":()})
    def vote(d):
        assessment = grading.FrozenPartAssessment(part_decisions=[d], comment="核对依据").assessment()
        return expert_for(assessment, plan, answer.content)
    experts = [vote(misclassified), vote(valid)]
    assert [v.score for v in experts] == [0, 8]
    provider, _ = raw_provider(json.dumps({"revisions":[{
        "expert_index": 0, "reason":"该生使用因式分解，只错一个符号。", "comment":"重算另解。",
        "decisions":[valid.model_dump()]}]}))
    with math_plans_context({problem.q_id:plan}), grading.math_review_scope():
        corrected = await grading.review_math_experts(problem, answer, experts, provider)
    assert [v.score for v in corrected] == [8, 8]
    assert all(grading.read_math_trace(v)["plan_id"] == plan.plan_id for v in corrected)
    assert corrected[0].steps[0].item_id is None


def test_unknown_method_is_not_an_implicit_points_route():
    _, draft = material(alternative={"deduct_per_error": 2})
    d = alternate("complete").model_copy(update={"method":"undetermined"})
    with pytest.raises(ValueError, match="undetermined"):
        settle_parts(draft.plan.part_rubric, [d], ANSWER, STEM)


@pytest.mark.asyncio
async def test_compilation_can_repair_a_missing_rate_without_changing_teacher_cap():
    from backend.services.rubric_drafts import compile_parts_draft
    from backend.tests.test_question_material_format_repair import Provider
    row, draft = material(teacher="其他方法最高给一半分。", alternative={"deduct_per_error": 2})
    row.pop("rubric_draft")
    valid = {"part_rubric": draft.plan.part_rubric.model_dump(mode="json")}
    invalid = json.loads(json.dumps(valid))
    invalid["part_rubric"]["parts"][0]["alternative"]["deduct_per_error"] = None
    provider = Provider([json.dumps(invalid), json.dumps(valid)])
    new = await compile_parts_draft(row, provider)
    assert new.plan.part_rubric == draft.plan.part_rubric
    assert len(provider.calls) == 2
    assert "p: supply a positive alternative.deduct_per_error" in provider.calls[1][-1].content


@pytest.mark.parametrize("teacher_rate,expected,warning", [
    ("其他解法每错扣2分。", 6, True),
    ("其他正确解法给满分，每错扣2分。", 8, False),
])
def test_required_method_floor_and_teacher_exception(teacher_rate, expected, warning):
    from backend.services.part_rubric import bind_parts, PartRubric
    stem = "必须用配方法求方程的根。"
    row = dict(stem=stem, number="1", max_score=10, criterion="本题非空最低6分。"+teacher_rate)
    raw = PartRubric(id="q", label="1", stem=stem, total_pts=10, nonblank_floor={"pts":6},
                     parts=[dict(id="p", total_pts=10, alternative={"deduct_per_error":3},
                                 steps=[dict(id="s",criterion="配方",total_pts=10)])])
    rule = bind_parts(raw, row)
    alt = rule.parts[0].alternative
    assert alt.inferred_method_cap == warning and alt.deduct_per_error == 2
    assert settle_parts(rule,[alternate(errors=[error()])],ANSWER,stem)[0] == expected
    assert alt.maximum.resolve(10) == (6 if warning else 10)
    if warning:
        assert alt.minimum.resolve(10) == 6 and "最高得6分" in alternative_text(rule.parts[0])


def test_an_unrestricted_ai_alternative_can_freeze_a_partial_cap_and_floor():
    _, draft = material(alternative={"maximum":{"fraction":"1/2"}, "minimum":{"points":1},
                                    "deduct_per_error":2, "max_deduction":3, "source_quote":"untrusted"})
    alt = draft.plan.part_rubric.parts[0].alternative
    assert alt.source_quote == "" and alt.maximum.resolve(10) == 5 and alt.minimum.points == 1
    assert settle_parts(draft.plan.part_rubric, [alternate(errors=[error()])], ANSWER, STEM)[0] == 3


@pytest.mark.parametrize("shared, cap2", [(True, 3.6), (False, 5)])
def test_required_method_uses_its_part_share_and_settles_one_question_floor(shared, cap2):
    from backend.services.part_rubric import PartRubric, bind_parts

    common = "必须用配方法。" if shared else "完成两问。"
    stems = ["必须用配方法求根。", "求第二个方程的根。"]
    parts = [dict(id=f"p{i}", label=f"({i})", stem=stem, total_pts=points,
                  steps=[dict(id="result", criterion="求根", total_pts=points)],
                  alternative={"maximum":{"points":maximum}, "deduct_per_error":2})
             for i,(stem,points,maximum) in enumerate(zip(stems,(4,6),(4,5)),1)]
    source = dict(number="1", stem=common+"\n"+"\n".join(stems), max_score=10,
                  criterion="整题有书写最低6分。", question_structure={"shared_stem":common,
                  "subparts":[{"label":p["label"],"stem":p["stem"]} for p in parts]})
    raw = PartRubric(id="q", label="1", stem=common, total_pts=10, nonblank_floor={"pts":6}, parts=parts)
    rules = bind_parts(raw, source)
    assert [float(p.alternative.maximum.resolve(p.total_pts)) for p in rules.parts] == [2.4, cap2]
    def judgments(relevance):
        return [PartDecision(part_id=p.id, target_quote="part:"+p.id,
            student_quote="student:1" if relevance != "unanswered" else "", progress="两问均已核对",
            method="alternative" if relevance == "complete" else "undetermined",
            reference_method="配方", student_method="分解", relevance=relevance) for p in rules.parts]
    assert settle_parts(rules, judgments("unanswered"), "", common)[0] == 0
    score, rows = settle_parts(rules, judgments("unrelated"), ANSWER, common)
    assert score == 6 and [r.score for r in rows if r.part_id is None] == [6]
    assert settle_parts(rules, judgments("complete"), ANSWER, common)[0] == pytest.approx(2.4+cap2)
    if not shared:
        for invalid in [3,7]:
            with pytest.raises(ValueError):
                altered = raw.model_dump(mode="json")
                altered["parts"][1]["alternative"]["maximum"] = {"points":invalid}
                bind_parts(PartRubric.model_validate(altered), source)


def _edit_provider(monkeypatch, responses):
    from types import SimpleNamespace
    from backend.services import task_facade
    from backend.tests.test_question_material_format_repair import Provider

    provider = Provider(responses)
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda owner: object())
    monkeypatch.setattr(task_facade, "resolve_stage_provider_route", lambda **kwargs: SimpleNamespace(provider=provider))
    return provider


def test_teacher_semantic_edit_commits_the_new_rule_and_reuses_it_for_grading(monkeypatch):
    from backend.db import assignment_repository
    from fastapi.testclient import TestClient
    from backend.auth import create_token
    from backend.main import create_app
    from backend.services.math_grading_context import problem_from_question
    from backend.tests.test_frozen_part_rubric import _persist_method_question, _factorization_score

    args = _persist_method_question()
    def saved():
        record, = assignment_repository.list_questions(args["task_id"], teacher_id=args["owner_id"])
        return record, saved_draft(problem_from_question(record))
    original, draft = saved()
    text = "按独立计算错误扣分，每错扣2分。其他正确解法给满分，每错扣1分。"
    raw = draft.plan.part_rubric.model_dump(mode="json")
    part = raw["parts"][0]
    del part["steps"]
    part["deductions"] = [{"id":"calc", "criterion":"独立计算错误", "deduct_pts":2, "source_quote":"每错扣2分"}]
    part["alternative"] = {"deduct_per_error":9}
    broken = json.loads(json.dumps(raw))
    broken["parts"][0]["deductions"][0]["deduct_pts"] = 3
    provider = _edit_provider(monkeypatch, [json.dumps({"part_rubric":item}) for item in (broken,raw)])
    with TestClient(create_app(), headers={"Authorization": "Bearer "+create_token(args["owner_id"],"teacher")}) as client:
        response = client.put(f"/tasks/{args['task_id']}/problems/{args['q_id']}", json={"criterion":text})
    assert response.status_code == 200, response.text
    returned = response.json()["problem"]
    assert returned["criterion"] == text and "独立计算错误" in returned["effective_criterion"]
    record, updated = saved()
    assert len(provider.calls) == 2
    assert record.version == original.version+1 and updated.plan.plan_id != draft.plan.plan_id
    rules = updated.plan.part_rubric
    assert rules.rubric_version == draft.plan.part_rubric.rubric_version+1
    assert record.criterion == rules.teacher_rubric == text and rules.parts[0].deductions[0].deduct_pts == 2
    assert rules.parts[0].alternative.deduct_per_error == 1
    common = dict(student_quote="算错常数项", relevance="partial")
    def judge(method, rule):
        events = [dict(id="x",rule_id=rule,evidence="算错常数项",origin_quote="算错常数项",reason="独立错误")]
        return _factorization_score(rules,method=method,errors=events,**common)
    assert (judge("reference","calc"), judge("alternative","alternative")) == (8,9)


def test_failed_or_stale_model_interpretation_cannot_overwrite_teacher_edits(monkeypatch):
    from backend.db import assignment_repository
    from backend.domain.errors import ValidationError, VersionConflict
    from backend.services import task_facade
    from backend.services.math_grading_context import problem_from_question
    from backend.tests.test_frozen_part_rubric import _persist_method_question

    args = _persist_method_question()
    def record():
        return assignment_repository.list_questions(args["task_id"], teacher_id=args["owner_id"])[0]
    old = record()
    text = "要说明解的检验过程，否则该步不得分。"
    provider = _edit_provider(monkeypatch, ['{}']*8)
    with pytest.raises(ValidationError):
        task_facade.update_problem(**args, patch={"criterion":text})
    assert record().source == old.source and record().version == old.version and record().criterion == old.criterion
    rules = saved_draft(problem_from_question(old)).plan.part_rubric.model_dump(mode="json")
    provider.responses = iter([json.dumps({"part_rubric":rules})])
    invoke = provider.ainvoke
    async def concurrent_save(messages):
        task_facade.update_problem(**args, patch={"alternative_rule_edits":{"p1":"其他正确解法给满分，每错扣1分"}})
        return await invoke(messages)
    monkeypatch.setattr(provider, "ainvoke", concurrent_save)
    with pytest.raises(VersionConflict):
        task_facade.update_problem(**args, patch={"criterion":text})
    latest = record()
    assert latest.criterion == old.criterion and latest.version == old.version+1
    assert saved_draft(problem_from_question(latest)).plan.part_rubric.parts[0].alternative.maximum.resolve(10) == 10


def test_semantic_alternative_edit_adopts_only_the_edited_policy(monkeypatch):
    from backend.db import assignment_repository
    from backend.services import task_facade
    from backend.services.math_grading_context import problem_from_question
    from backend.tests.test_frozen_part_rubric import _persist_method_question, _factorization_score

    args = _persist_method_question()
    def load():
        row, = assignment_repository.list_questions(args["task_id"], teacher_id=args["owner_id"])
        return saved_draft(problem_from_question(row)).plan.part_rubric
    original = load()
    text = "换路子也能拿足这道题的全部配分"
    raw = original.model_dump(mode="json")
    raw["parts"][0]["steps"][0]["criterion"] = "模型擅改的评分项"
    raw["parts"][0]["alternative"] = {"maximum":{"points":10},"deduct_per_error":1,
                                       "field_sources":{"maximum":text}}
    provider = _edit_provider(monkeypatch,[json.dumps({"part_rubric":raw})])
    task_facade.update_problem(**args,patch={"alternative_rule_edits":{"p1":text}})
    rules = load()
    assert len(provider.calls)==1 and rules.parts[0].steps==original.parts[0].steps
    assert _factorization_score(rules) == 10
    assert rules.parts[0].alternative.source_quote == text
