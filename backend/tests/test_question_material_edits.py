"""Teacher source protection and edit direction at the public save endpoints."""
import json

import pytest

from backend.db import assignment_repository
from backend.services.math_grading_context import problem_from_question
from backend.services.question_material_edits import answer_sections, missing_answer_parts, semantic_text_edits
from backend.services.rubric_drafts import saved_draft
from backend.tests.test_question_stem_save import (
    Provider, current, edit, response, run_once, status, submit, worker,
)


def criterion_reply(rule):
    values = json.loads(response(rule=rule))
    return json.dumps({"candidates": [row for row in values["candidates"] if row["target"] == "criterion"]})


@pytest.mark.asyncio
async def test_display_edit_does_not_resolve_a_provider_or_replace_materials(edit, monkeypatch):
    owner, task, _, _ = edit
    assignment_repository.update_question(task, teacher_id=owner, q_id="q1", expected_version=current(edit).version,
                                          stem="计算六乘七的结果")
    before = current(edit)
    submit(edit, stem=before.stem + "。")
    await run_once(worker(monkeypatch, None))
    assert status(edit)["status"] == "done", status(edit)
    after = current(edit)
    assert after.stem == before.stem + "。"
    assert (after.reference_answer, after.criterion) == (before.reference_answer, before.criterion)


@pytest.mark.asyncio
async def test_teacher_answer_is_never_a_regeneration_target(edit, monkeypatch):
    owner, task, _, _ = edit
    teacher = "Six groups of seven contain 42 items."
    assignment_repository.update_question(task, teacher_id=owner, q_id="q1", expected_version=current(edit).version,
        reference_answer=teacher, source={"presentation": {
            "material_provenance": {"reference_answer": {"authority": "uploaded_reference"}},
            "ai_completion_provenance": {"criterion": {"source_kind": "ai_generated"}}}})
    provider = Provider(['{"answer":false,"rubric":true,"execution":false}',
                         criterion_reply("Give credit for multiplication and 42.")])
    submit(edit, stem="Calculate 6 × 7 and explain the multiplication.")
    await run_once(worker(monkeypatch, provider))
    assert status(edit)["status"] == "done", status(edit)
    question = current(edit)
    assert question.reference_answer == teacher and len(provider.calls) == 2
    targets = json.loads(provider.calls[-1][1].content.split("\n", 1)[1])["requested_targets"]
    assert [item["target"] for item in targets] == ["criterion"]
    assert "reference_answer" not in question.source["presentation"]["ai_completion_provenance"]
    assert saved_draft(problem_from_question(question)).plan.adopted_material.reference_answer == teacher


@pytest.mark.asyncio
async def test_manual_answer_updates_only_generated_rubric_without_an_existing_draft(edit, monkeypatch):
    _, task, version, client = edit
    before = current(edit)
    teacher = "Count six groups of seven. There are 42 objects."
    payload = {"field": "reference_answer", "value": teacher, "expected_workflow_revision": version}
    url = f"/tasks/{task}/problems/q1/material-save"
    receipt = client.post(url, json=payload)
    assert receipt.status_code == 202
    assert client.post(url, json=payload).json()["id"] == receipt.json()["id"]
    assert status(edit)["reference_answer"] == teacher
    provider = Provider([criterion_reply("Award credit for six groups and a final count of 42.")])
    await run_once(worker(monkeypatch, provider))
    assert status(edit)["status"] == "done", status(edit)
    question = current(edit)
    assert question.stem == before.stem and question.reference_answer == teacher
    assert "six groups" in question.criterion and len(provider.calls) == 1
    assert "reference_answer" not in question.source["presentation"]["ai_completion_provenance"]


def test_missing_parts_preserve_existing_teacher_solution():
    structure = {"subparts": [{"label": "(a)"}, {"label": "(b)"}]}
    teacher = "(a) Original method and result."
    assert missing_answer_parts(teacher, structure) == ("(b)",)
    sections = answer_sections("(a) A different solution.\n(b) New result.", structure)
    assert sections["(b)"] == "(b) New result."


@pytest.mark.parametrize("old,new", [("n", "n!"), ("3.5", "35"), ("x < 2", "x ≤ 2"),
    ('Print "a." exactly.', 'Print "a" exactly.'), ("`n!`", "`n`")])
def test_semantic_tokens_survive_display_comparison(old, new):
    assert semantic_text_edits({"stem": old}, {"stem": new}) == {"stem"}


def test_punctuation_around_a_preserved_program_does_not_change_meaning():
    code = "\n```python\nprint('x!')\n```"
    assert not semantic_text_edits({"stem": "运行下面程序" + code}, {"stem": "运行下面程序。" + code})
    assert semantic_text_edits({"stem": code}, {"stem": code.replace("x!", "x")})


@pytest.mark.asyncio
async def test_triple_edit_is_atomic_and_full_task_uses_the_new_reference_with_no_initial_cases(edit, monkeypatch):
    from backend.models import TestCase
    from backend.services import grading_adapter, task_facade
    from backend.services.programming_materials import material_problem, resolve_generated_outputs
    from backend.services.programming_runs import _reference
    from backend.tests.test_programming_question_edit import DOUBLE, TRIPLE, provider_for

    owner, task, revision, client = edit
    stem = "Implement scale(x). The input x is an integer. Return x * 2."
    answer = "Return x * 3."
    assignment_repository.update_question(task, teacher_id=owner, q_id="q1", expected_version=current(edit).version,
        type="编程题", stem=stem, reference_answer="Return x * 2.", source={"presentation": {
            "solution_code": DOUBLE, "material_provenance": {"solution_code": {"source_kind": "uploaded_code"}},
            "ai_completion_provenance": {"reference_answer": {"source_kind": "ai_generated"}}}})
    saved = client.put(f"/tasks/{task}/problems/q1", json={"expected_workflow_revision": revision,
        "criterion": "Returns the correct value: 10 points.", "programming_scoring": {"test_points": 0, "input_rules": ""}})
    assert saved.status_code == 200, saved.text
    edit = owner, task, saved.json()["workflow_revision"], client
    policy = current(edit).criterion
    replies = [response(answer, "Returns the correct value.", 0), *provider_for({"solution_code": TRIPLE}).responses]
    provider = Provider(replies)
    submit(edit, stem=stem.replace("* 2", "* 3"))
    await run_once(worker(monkeypatch, provider))
    assert status(edit)["status"] == "done", status(edit)
    question = current(edit)
    assert question.reference_answer == answer and question.criterion == policy
    assert not question.test_cases and len(provider.calls) == 2
    full = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]["q1"]
    assert full["solution_code"] == DOUBLE and full["programming_reference_binding"]["generated_code"] == TRIPLE
    assert _reference(material_problem(full)) == TRIPLE
    grading = grading_adapter.build_problem_store([question])["q1"]
    descriptor = grading["_math_rubric_source"]["presentation"]["programming_reference_binding"]
    assert descriptor == full["programming_reference_binding"]
    probes = [TestCase(io_mode="function", function_args=[i]) for i in range(10)]
    assert (await resolve_generated_outputs(full, probes))[4].expected_return == "12"


def test_changed_prose_in_whole_form_updates_echoed_ai_materials(edit, monkeypatch):
    _, task, version, client = edit
    before = current(edit)
    provider = Provider([response()])
    worker(monkeypatch, provider)
    saved = client.put(f"/tasks/{task}/problems/q1", json={
        "stem": "Calculate 6 × 8.", "reference_answer": before.reference_answer, "criterion": before.criterion,
        "expected_workflow_revision": version})
    assert saved.status_code == 200, saved.text
    assert "48" in current(edit).criterion and current(edit).reference_answer == "6 × 8 = 48."
    assert len(provider.calls) == 1


def test_ordinary_given_program_and_input_are_rebound_to_edited_text():
    from backend.domain.code_output import bind_code_output, rebind_edited_code_output
    from backend.tests.test_code_output_grading import source_question
    stem, candidate = source_question("blank")
    before = bind_code_output(candidate, stem=stem, document=stem)
    after = rebind_edited_code_output(before.model_dump(), stem.replace("* 2", "* 3").replace("Input: 3", "Input: 4"))
    assert "* 3" in after.source.code and after.source.stdin == "4"
    assert rebind_edited_code_output(before.model_dump(), stem + "\nInput: 5").source is None


@pytest.mark.asyncio
async def test_teacher_result_receives_only_the_requested_missing_derivation(edit, monkeypatch):
    owner, task, _, _ = edit
    assignment_repository.update_question(task, teacher_id=owner, q_id="q1", expected_version=current(edit).version,
        reference_answer="42", source={"presentation": {"ai_completion_provenance": {"criterion": {}}}})
    new_stem = "Calculate 6 × 7. Show your work."
    explanation = "Adding six groups of seven gives forty-two."
    provider = Provider(['{"answer":true,"rubric":true,"execution":false}',
                         response(explanation, "Award credit for adding six groups of seven.")])
    submit(edit, stem=new_stem)
    await run_once(worker(monkeypatch, provider))
    assert status(edit)["status"] == "done", status(edit)
    after = current(edit)
    assert after.reference_answer == "42\n\n" + explanation
    assert after.source["presentation"]["teacher_material_supplements"]["reference_answer"]["original"] == "42"
    assert "reference_answer" not in after.source["presentation"]["ai_completion_provenance"]
    assert "only the missing derivation" in provider.calls[-1][1].content

