"""Edit contracts: stored budgets and identities survive display-only changes."""
import json

import pytest

from backend.db import assignment_repository
from backend.services import task_facade
from backend.services.math_grading_context import problem_from_question
from backend.services.question_structure import build_major_question_structure
from backend.services.rubric_drafts import RubricDefinition, generated_rubric, saved_draft
from backend.tests.test_alternative_route import _edit_provider
from backend.tests.test_major_question_structure import _seed_task_with_two_part_question


def seeded():
    owner, task = "identity-owner", "identity-task"
    _seed_task_with_two_part_question(owner, task)
    old, = assignment_repository.list_questions(task, teacher_id=owner)
    stem = "(1) 列式并计算。\n(2) 写出对称轴。\n(3) 说明结论。"
    text = "第一问按1、2、1分分步；第二问2分；第三问4分。"
    structure = build_major_question_structure(dict(stem=stem, number="1"), major_order=0).model_dump()
    parts = [dict(id=f"p{i}", label=part["label"], total_pts=sum(points),
                  steps=[dict(id=f"s{j}", total_pts=score, criterion=f"完成第{j+1}项")
                         for j, score in enumerate(points)], alternative={"deduct_per_error": .5})
             for i, (part, points) in enumerate(zip(structure["subparts"], [[1, 2, 1], [2], [4]]))]
    row = dict(q_id="q1", number="1", type="计算题", stem=stem, criterion=text,
               reference_answer="", max_score=10, question_structure=structure)
    _, draft = generated_rubric(row, RubricDefinition(part_rubric=dict(
        id="q1", label="1", stem=stem, total_pts=10, parts=parts)))
    assignment_repository.update_question(task, teacher_id=owner, q_id="q1", expected_version=old.version,
        stem=stem, criterion=text, number="1", reference_answer="", source={"presentation": {
            **old.source["presentation"], "question_structure": structure,
            "rubric_schema_version": 2, "rubric_draft": draft.model_dump(mode="json")}})
    args = dict(owner_id=owner, task_id=task, q_id="q1")
    task_facade.update_problem(**args, patch={"alternative_rule_edits": {
        "p1": "另解最高一半分，每错扣0.5分"}})
    return args


def record(args):
    row, = assignment_repository.list_questions(args["task_id"], teacher_id=args["owner_id"])
    return row


@pytest.mark.parametrize("patch", [
    {"stem": "(1) 列式并计算。\n(2) 写出对称轴!\n(3) 说明结论。"},
    {"stem": "(1) 列式并计算。\n(2) 写出对称轴！\n(3) 说明结论。"},
    {"criterion": "第一问按1、2、1分分步；第二问2分；第三问4分。。"},
])
def test_display_edit_preserves_scoring_without_model(monkeypatch, patch):
    args = seeded()
    old = saved_draft(problem_from_question(record(args))).plan.part_rubric
    def forbidden(*args, **kwargs):
        pytest.fail("A display-only edit must not call a model")
    monkeypatch.setattr(task_facade, "_compile_review_edit", forbidden)
    response = task_facade.update_problem(**args, patch=patch)
    new = saved_draft(problem_from_question(record(args))).plan.part_rubric
    assert [[s.total_pts for s in p.steps] for p in new.parts] == [[1, 2, 1], [2], [4]]
    assert [p.alternative for p in new.parts] == [p.alternative for p in old.parts]
    assert response["problem"]["rubric_point_summary"]["total_points"] == "10"
    assert new.parts[1].alternative.maximum.resolve(2) == 1


def test_semantic_edit_reaches_compiler_before_any_text_sum(monkeypatch):
    args = seeded()
    old = record(args)
    rules = saved_draft(problem_from_question(old)).plan.part_rubric.model_dump(mode="json")
    provider = _edit_provider(monkeypatch, [json.dumps({"part_rubric": rules})])
    text = old.criterion.replace("分分步", "分分步，需说明每步依据")
    task_facade.update_problem(**args, patch={"criterion": text})
    assert len(provider.calls) == 1
    assert record(args).criterion == text
    assert saved_draft(problem_from_question(record(args))) is not None


def test_subpart_identity_survives_reorder_insert_delete_and_renumber():
    def build(stem, previous=None):
        return build_major_question_structure(dict(stem=stem, number="1"), major_order=0,
                                              previous=previous)
    old = build("(1) 求面积。\n(2) 求周长。\n(3) 证明唯一性。")
    reordered = build("(1) 证明唯一性。\n(2) 求面积。\n(3) 求周长。", old)
    assert [p.subpart_id for p in reordered.subparts] == ["sp3", "sp1", "sp2"]
    inserted = build("(1) 求面积。\n(2) 求角度。\n(3) 求周长。", reordered)
    assert [p.subpart_id for p in inserted.subparts] == ["sp1", "sp4", "sp2"]
    deleted = build("(1) 求面积。\n(2) 求周长。", inserted)
    again = build("(1) 求面积。\n(2) 求周长。\n(3) 求体积。", deleted)
    assert [p.subpart_id for p in again.subparts] == ["sp1", "sp2", "sp5"]


def test_wording_change_keeps_identity_but_recompiles_rules(monkeypatch):
    args = seeded()
    old = record(args)
    rules = saved_draft(problem_from_question(old)).plan.part_rubric.model_dump(mode="json")
    provider = _edit_provider(monkeypatch, [json.dumps({"part_rubric": rules})])
    task_facade.update_problem(**args, patch={"stem": old.stem.replace("写出对称轴", "求对称轴方程并解释")})
    assert len(provider.calls) == 1
    row = record(args)
    assert row.source["presentation"]["alternative_rules"]["p1"]["text"] == "另解最高一半分，每错扣0.5分"
    new = saved_draft(problem_from_question(row)).plan.part_rubric
    assert new.parts[1].id == "p1" and new.parts[1].alternative.maximum.resolve(2) == 1


def test_reorder_follows_source_identity_and_keeps_teacher_alternative(monkeypatch):
    args = seeded()
    old = record(args)
    raw = saved_draft(problem_from_question(old)).plan.part_rubric.model_dump(mode="json")
    raw["parts"] = [raw["parts"][i] for i in (2, 0, 1)]
    for i, part in enumerate(raw["parts"], 1):
        part["label"] = f"({i})"
    provider = _edit_provider(monkeypatch, [json.dumps({"part_rubric": raw})])
    task_facade.update_problem(**args, patch={
        "stem": "(1) 说明结论。\n(2) 列式并计算。\n(3) 写出对称轴。",
        "criterion": "第一问4分；第二问按1、2、1分分步；第三问2分。"})
    assert len(provider.calls) == 1
    row = record(args)
    draft = saved_draft(problem_from_question(row))
    assert [(part.id, part.subpart_id) for part in draft.plan.part_rubric.parts] == [
        ("p2", "sp3"), ("p0", "sp1"), ("p1", "sp2")]
    assert draft.plan.part_rubric.parts[2].alternative.maximum.resolve(2) == 1
    override = row.source["presentation"]["alternative_rules"]["p1"]
    assert override["label"] == "(3)" and override["subpart_id"] == "sp2"


def test_failed_semantic_audit_keeps_source_structure_and_version_atomic(monkeypatch):
    from types import SimpleNamespace
    from backend.domain.errors import ValidationError
    from backend.tests.test_teacher_policy_audit import PolicyProvider
    args = seeded()
    old = record(args)
    raw = saved_draft(problem_from_question(old)).plan.part_rubric.model_dump(mode="json")
    provider = PolicyProvider(raw, verdict="inconsistent")
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda owner: object())
    monkeypatch.setattr(task_facade, "resolve_stage_provider_route", lambda **kwargs: SimpleNamespace(provider=provider))
    with pytest.raises(ValidationError, match="could not be verified"):
        task_facade.update_problem(**args, patch={"criterion": old.criterion + "非空作答最低分按满分的五分之一计算"})
    new = record(args)
    assert len(provider.calls) == 2
    assert (new.version, new.stem, new.criterion, new.source) == (old.version, old.stem, old.criterion, old.source)


def test_adding_binding_ids_does_not_invalidate_safe_preexisting_frozen_rules():
    from backend.services.math_rubric_plan import _content_hash, _question_input, math_rubric_fingerprint
    from backend.tests.test_rubric_policy_preservation import parts_question
    row, draft = parts_question()
    # Reconstruct the pre-change projection and stored shape independently.
    old_context = _question_input(row)
    for part in old_context["question_structure"]["subparts"]:
        part.pop("subpart_id")
    legacy = draft.model_dump(mode="json")
    legacy["content_fingerprint"] = _content_hash({key: value for key, value in old_context.items()
        if key not in {"q_id", "number", "teacher_subpart_points"}})
    plan = legacy["plan"]
    plan["input_fingerprint"] = _content_hash(old_context)
    for part in plan["part_rubric"]["parts"]:
        part.pop("subpart_id")
    plan["plan_id"] = _content_hash({key: value for key, value in plan.items() if key != "plan_id"})
    assert math_rubric_fingerprint(row) == plan["input_fingerprint"]
    accepted = saved_draft({**row, "rubric_draft": legacy})
    assert accepted is not None and accepted.plan.plan_id == plan["plan_id"]
