import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.auth import create_token
from backend.db import assignment_repository, workflow_repository
from backend.main import create_app
from backend.services import question_stem_save, task_facade
from backend.services.math_grading_context import problem_from_question
from backend.services.rubric_drafts import saved_draft
from backend.services.workflow_worker import WorkflowWorker
from backend.tests.test_question_material_format_repair import Provider
from backend.tests.test_task_background_workflows import _seed_task


OLD_STEM = "Calculate 6 × 7."
NEW_STEM = "Calculate 6 × 8."
ANSWER = "6 × 8 = 48."
RULE = "Give full credit for the result 48."


def response(answer=ANSWER, rule=RULE, test_points=0):
    steps = [{"id": "result", "criterion": rule, "total_pts": 10 - test_points}]
    if test_points:
        steps.append({"id": "tests", "criterion": "Pass the functional tests.",
                      "total_pts": test_points, "test_suite": True})
    return json.dumps({"candidates": [
        {"target_id": "q1:reference_answer", "q_id": "q1", "target": "reference_answer",
         "text_value": answer},
        {"target_id": "q1:criterion", "q_id": "q1", "target": "criterion",
         "rubric_definition": {"part_rubric": {"total_pts": 10, "parts": [
             {"id": "p1", "label": None, "total_pts": 10,
              "steps": steps,
              "alternative": {"deduct_per_error": 1}},
         ]}}},
    ]})


@pytest.fixture
def edit():
    owner, task = _seed_task()
    assignment_repository.add_question(
        task, teacher_id=owner, q_id="q1", order_index=0, number="1",
        type="计算题", stem=OLD_STEM, criterion="Give full credit for 42.",
        max_score=10, reference_answer="6 × 7 = 42.",
        source={"presentation": {"ai_completion_provenance": {
            "reference_answer": {"source_kind": "ai_generated"},
            "criterion": {"source_kind": "ai_generated"},
        }}},
    )
    workflow = workflow_repository.update_workflow(task, owner_id=owner, presentation_status="problems_ready")
    client = TestClient(create_app(), headers={"Authorization": "Bearer " + create_token(owner, "teacher")})
    return owner, task, workflow.workflow_revision, client


def current(edit):
    owner, task, _, _ = edit
    return assignment_repository.list_questions(task, teacher_id=owner)[0]


def submit(edit, **extra):
    _, task, revision, client = edit
    result = client.post(f"/tasks/{task}/problems/q1/stem-save", json={
        "stem": NEW_STEM, "expected_workflow_revision": revision, **extra,
    })
    assert result.status_code == 202, result.text
    return result.json()


def status(edit):
    _, task, _, client = edit
    result = client.get(f"/tasks/{task}/question-stem-saves")
    assert result.status_code == 200, result.text
    return result.json()[0]


def worker(monkeypatch, provider):
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda owner: None)
    monkeypatch.setattr(question_stem_save, "resolve_stage_provider_route", lambda **_: SimpleNamespace(provider=provider))
    return WorkflowWorker(handlers={question_stem_save.QUESTION_STEM_SAVE_OPERATION: question_stem_save.run})


async def run_once(worker):
    assert await worker.poll_once() == 1
    await asyncio.wait_for(asyncio.gather(*worker._in_flight), 5)


@pytest.mark.asyncio
async def test_saves_all_fields_once_after_duplicate_and_reloaded_status(edit, monkeypatch):
    began = asyncio.Event()
    release = asyncio.Event()

    class SlowProvider(Provider):
        async def ainvoke(self, messages):
            began.set()
            await release.wait()
            return await super().ainvoke(messages)

    provider = SlowProvider([response()])
    running = worker(monkeypatch, provider)
    original = current(edit)
    receipt = submit(edit)
    assert receipt["status"] == "pending"
    assert submit(edit)["id"] == receipt["id"]
    assert await running.poll_once() == 1
    await asyncio.wait_for(began.wait(), 5)
    assert status(edit)["status"] == "running"
    assert current(edit) == original
    assert submit(edit)["id"] == receipt["id"]
    release.set()
    await asyncio.wait_for(asyncio.gather(*running._in_flight), 5)
    saved = status(edit)
    assert saved["status"] == "done", saved
    assert "stem" not in saved
    assert submit(edit) == saved
    assert await running.poll_once() == 0
    assert len(provider.calls) == 1
    after = current(edit)
    draft = saved_draft(problem_from_question(after))
    assert after.version == original.version + 1
    assert after.stem == NEW_STEM
    assert after.reference_answer == ANSWER
    assert RULE in after.criterion and "42" not in after.criterion
    assert draft is not None
    assert draft.plan.original_criterion == after.criterion
    assert draft.plan.effective_criterion == after.criterion
    assert draft.plan.adopted_material.reference_answer == ANSWER
    assert draft.plan.part_rubric.parts[0].steps[0].criterion == RULE
    owner, task, _, _ = edit
    visible = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]["q1"]
    assert visible["criterion"] == after.criterion and visible["reference_answer"] == ANSWER
    assert "48" in visible["effective_criterion"]


@pytest.mark.asyncio
async def test_failure_leaves_all_fields_and_explicit_retry_is_idempotent(edit, monkeypatch):
    original = current(edit)
    receipt = submit(edit)
    await run_once(worker(monkeypatch, None))
    failed = status(edit)
    assert failed["status"] == "error" and failed["stem"] == NEW_STEM
    assert failed["error_code"] == "no_provider_configured"
    assert current(edit) == original
    assert submit(edit) == failed
    retry = submit(edit, retry_of=receipt["id"])
    assert retry["id"] != receipt["id"]
    assert submit(edit, retry_of=receipt["id"]) == retry
    await run_once(worker(monkeypatch, Provider([response()])))
    assert status(edit)["status"] == "done"


@pytest.mark.asyncio
async def test_concurrent_teacher_edit_prevents_late_model_overwrite(edit, monkeypatch):
    owner, task, revision, _ = edit
    original = current(edit)
    submit(edit)

    class ConcurrentProvider(Provider):
        async def ainvoke(self, messages):
            task_facade.update_problem(task_id=task, owner_id=owner, q_id="q1",
                                       patch={"number": "2"}, expected_revision=revision)
            return await super().ainvoke(messages)

    await run_once(worker(monkeypatch, ConcurrentProvider([response()])))
    failed = status(edit)
    assert failed["status"] == "error" and failed["error_code"] == "stale_revision"
    after = current(edit)
    assert after.number == "2"
    assert (after.stem, after.reference_answer, after.criterion) == (
        original.stem, original.reference_answer, original.criterion,
    )


@pytest.mark.asyncio
async def test_log_base_correction_rebuilds_both_materials(edit, monkeypatch):
    owner, task, _, _ = edit
    stem = r"按增长率由小到大排列：$n^{2/3}$, $n$, $n/\log_n 2$, $n\log_2 n$, $n^{3/2}$。"
    assignment_repository.update_question(task, teacher_id=owner, q_id="q1",
        expected_version=current(edit).version, stem=stem.replace(r"\log_n 2", r"\log_2 n"))
    answer = r"$n^{2/3}<n<n/\log_n 2=n\log_2 n<n^{3/2}$。"
    rule = r"将 $n/\log_n 2$ 和 $n\log_2 n$ 并列在 $n$ 与 $n^{3/2}$ 之间。"
    provider = Provider([response(answer, rule)])
    submit(edit, stem=stem)
    await run_once(worker(monkeypatch, provider))
    assert status(edit)["status"] == "done"
    assert "source_growth_order" in provider.calls[0][1].content
    updated = current(edit)
    assert updated.reference_answer == answer and rule in updated.criterion
    assert r"n/\log_2 n" not in updated.criterion + updated.reference_answer


@pytest.mark.asyncio
async def test_regeneration_keeps_teacher_alternative_deduction(edit, monkeypatch):
    owner, task, _, client = edit
    submit(edit)
    await run_once(worker(monkeypatch, Provider([response()])))
    rule = task_facade.update_problem(task_id=task, owner_id=owner, q_id="q1", patch={
        "alternative_rule_edits": {"p1": "其他正确解法给满分，每个独立错误扣 2 分。"},
    })
    edit = owner, task, rule["workflow_revision"], client
    submit(edit, stem="Calculate 6 × 9.")
    await run_once(worker(monkeypatch, Provider([response("6 × 9 = 54.", "Give full credit for 54.")])))
    assert status(edit)["status"] == "done", status(edit)
    draft = saved_draft(problem_from_question(current(edit)))
    assert draft is not None
    assert draft.plan.part_rubric.parts[0].alternative.deduct_per_error == 2
    assert "54" in draft.plan.original_criterion


@pytest.mark.asyncio
@pytest.mark.parametrize("test_points", [0, 6])
async def test_programming_regeneration_preserves_teacher_test_budget(edit, monkeypatch, test_points):
    owner, task, _, client = edit
    assignment_repository.update_question(task, teacher_id=owner, q_id="q1",
        expected_version=current(edit).version, type="编程题", stem="Implement square(x) for integer x.")
    saved = task_facade.update_problem(task_id=task, owner_id=owner, q_id="q1", patch={
        "criterion": f"Returns the correct value: {10 - test_points} points.",
        "programming_scoring": {"test_points": test_points, "input_rules": "Input x is an integer."},
    })
    edit = owner, task, saved["workflow_revision"], client
    stem = "Implement cube(x) for integer x."
    answer = "Return x * x * x."
    rule = "Returns the correct value."
    provider = Provider([response(answer, rule, test_points)])
    submit(edit, stem=stem)
    await run_once(worker(monkeypatch, provider))
    assert status(edit)["status"] == "done", status(edit)
    after = current(edit)
    draft = saved_draft(problem_from_question(after))
    assert draft is not None and draft.programming_scoring.test_points == test_points
    assert draft.programming_scoring.input_rules == "Input x is an integer."
    assert answer == after.reference_answer and rule.rstrip(".") in after.criterion
    assert "functional tests" not in after.criterion


def test_another_teacher_cannot_start_or_read_the_saved_draft(edit):
    _, task, revision, _ = edit
    stranger, _ = _seed_task()
    client = TestClient(create_app(), headers={"Authorization": "Bearer " + create_token(stranger, "teacher")})
    submit(edit)
    assert client.get(f"/tasks/{task}/question-stem-saves").status_code == 404
    assert client.post(f"/tasks/{task}/problems/q1/stem-save", json={
        "stem": NEW_STEM, "expected_workflow_revision": revision,
    }).status_code == 404
