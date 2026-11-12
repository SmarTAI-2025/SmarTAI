"""Unchanged teacher evidence and part identities retain their accepted policies."""
from copy import deepcopy

import pytest

from backend.services.question_structure import build_major_question_structure
from backend.services.rubric_drafts import RubricDefinition, compile_parts_draft, generated_rubric, saved_draft
from backend.tests.test_teacher_policy_audit import PolicyProvider, unknown_floor


@pytest.mark.asyncio
async def test_known_floor_note_keeps_existing_semantic_approval_without_model_call():
    row, raw = unknown_floor()
    row["criterion"] = "沿用前面的错误，求浓度这一步保留一半分"
    raw.pop("nonblank_floor")
    raw["parts"][0]["steps"][1]["follow_through_keep"] = .5
    provider = PolicyProvider(raw)
    prior = await compile_parts_draft(row, provider)
    row["rubric_draft"] = prior.model_dump(mode="json")
    setup = {"teacher_notes": "非空作答最低2分"}
    revised = await compile_parts_draft(row, provider, grading_setup=setup)
    assert len(provider.calls) == 2
    assert revised.semantic_policy_checks == prior.semantic_policy_checks
    assert revised.plan.part_rubric.nonblank_floor.resolve(10) == 2
    assert saved_draft({**row, "rubric_draft": revised.model_dump(mode="json")}, grading_setup=setup) is not None


def parts_question(first="求面积。", shared="完成两问。"):
    row = dict(q_id="q1", number="1", type="计算题", max_score=10,
               stem=f"{shared}\n(1) {first}\n(2) 求周长。", criterion="按完成程度给分。", reference_answer="")
    row["question_structure"] = build_major_question_structure(row, major_order=0).model_dump()
    raw = {"id": "q1", "label": "1", "stem": shared, "total_pts": 10, "parts": [
        {"id": "p1", "label": "(1)", "total_pts": 4, "steps": [
            {"id": "s1", "criterion": "求面积", "total_pts": 4}], "alternative": {"deduct_per_error": .5}},
        {"id": "p2", "label": "(2)", "total_pts": 6, "steps": [
            {"id": "s2", "criterion": "求周长", "total_pts": 6}], "alternative": {"deduct_per_error": .75}},
    ]}
    _, prior = generated_rubric(row, RubricDefinition(part_rubric=raw))
    return row, prior


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["reorder", "part_meaning", "shared_instruction", "reference_target"])
async def test_reorder_preserves_default_policy_only_when_source_meaning_is_unchanged(change):
    original_first = "利用第2问结果求面积。" if change == "reference_target" else "求面积。"
    row, prior = parts_question(original_first)
    first = "求面积并证明结论。" if change == "part_meaning" else original_first
    shared = "各问必须使用坐标法。" if change == "shared_instruction" else "完成两问。"
    revised_row = {**row, "stem": f"{shared}\n(1) 求周长。\n(2) {first}"}
    revised_row["question_structure"] = build_major_question_structure(
        revised_row, major_order=0, previous=row["question_structure"]).model_dump()
    raw = deepcopy(prior.plan.part_rubric.model_dump(mode="json"))
    raw["parts"].reverse()
    for index, part in enumerate(raw["parts"], 1):
        part["label"] = f"({index})"
        part["alternative"]["deduct_per_error"] = 3
    provider = PolicyProvider(raw)
    revised = await compile_parts_draft(revised_row, provider, previous=prior)
    assert len(provider.calls) == 1
    by_id = {part.id: part for part in revised.plan.part_rubric.parts}
    assert by_id["p1"].alternative.deduct_per_error == (.5 if change == "reorder" else 3)
    assert by_id["p2"].alternative.deduct_per_error == (3 if change == "shared_instruction" else .75)
