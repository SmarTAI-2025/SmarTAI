from types import SimpleNamespace

import pytest

from backend.api import task_preparation
from backend.db import assignment_repository, workflow_repository
from backend.db.session import session_scope
from backend.domain.errors import ValidationError
from backend.services import task_facade
from backend.tests.test_question_preparation_recovery import (
    _RecoveryRegistry, _claim, _install_incremental_preparer, _queue_question_preparation, _seed_task,
)


async def failed_preparation(monkeypatch, failed_question="q2", failure_questions=None):
    owner, task = _seed_task()
    _, response, _ = await _queue_question_preparation(owner, task, source_text="1. First.\n2. Second.")
    calls = []
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: _RecoveryRegistry())
    _install_incremental_preparer(monkeypatch, calls,
        question_count=2, known_failure_question=failed_question, failure_questions=failure_questions, record_base_provider_calls=True)
    await task_preparation.run_durable_question_preparation(_claim(owner, response["job_id"], "manual-test"))
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    return owner, task, response["job_id"], workflow.workflow_revision, calls


async def manual(owner, task, job, revision, question_count=None):
    result = await task_preparation.manually_complete_question_preparation(task, job,
        task_preparation.ManualQuestionPreparationRequest(expected_workflow_revision=revision, question_count=question_count),
        current=SimpleNamespace(id=owner))
    if isinstance(result, dict) and result["status"] == "ready_for_review":
        for _ in range(2):
            for data in (task_facade.get_task(task_id=task, owner_id=owner),
                         await task_facade.async_task_state(task_id=task, owner_id=owner),
                         task_facade.list_tasks(owner_id=owner)[task]):
                assert data["status"] == "problems_ready"
                assert data["error"] is None and data["last_failed_job_id"] is None
                assert (data.get("progress") or {}).get("phase") != "error"
    return result


@pytest.mark.parametrize("policy,maximum,source", [
    ({"mode": "uniform", "uniform_max_score": 7}, 7, "uniform"),
    ({"mode": "per_question", "per_question_text": "第1题：8分"}, 8, "per_question_text"),
    ({"mode": "default_10"}, 10, "default_10"),
])
def test_empty_manual_rows_keep_frozen_teacher_score_policy(policy, maximum, source):
    from backend.services.question_preparation_manual import blank_manual_question_packages
    operation = SimpleNamespace(payload={"score_policy": policy}, error_code="operation_cancelled")
    saved = blank_manual_question_packages(operation, 1)["manual-1"]
    assert saved["max_score"] == maximum and saved["max_score_source"] == source
    assert saved["type"] == "" and saved["stem"] == "" and saved["criterion"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped_after", [None, "q1", "q2"])
async def test_paused_question_handoff_keeps_completed_questions_and_sources(monkeypatch, stopped_after):
    import asyncio
    from backend.services.task_execution import stop_task_run
    owner, task = _seed_task()
    source, queued, _ = await _queue_question_preparation(owner, task, source_text="1. First.\n2. Second.")
    calls = []
    if stopped_after:
        monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: _RecoveryRegistry())
        _install_incremental_preparer(monkeypatch, calls,
            question_count=2, cancel_after_question=stopped_after, record_base_provider_calls=True)
        with pytest.raises(asyncio.CancelledError):
            await task_preparation.run_durable_question_preparation(_claim(owner, queued["job_id"], "stopped-manual"))
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=queued["job_id"], expected_revision=workflow.workflow_revision)
    paused = task_facade.get_task(task_id=task, owner_id=owner)
    before_calls = list(calls)
    result = await manual(owner, task, queued["job_id"], paused["workflow_revision"])
    assert isinstance(result, dict), getattr(result, "body", result)
    assert calls == before_calls
    if not stopped_after:
        assert result["status"] == "ready_for_review" and result["first_question_id"] is None
        assert task_facade.get_task(task_id=task, owner_id=owner)["status"] == "problems_ready"
        assert "questions_required" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
        from pydantic import ValidationError as RequestValidationError
        for payload in ({"manual_questions": [{}]}, {"completed_source_tokens": [source["source_token"]]},
                        {"question_count": 0}, {"question_count": 501}, {"question_count": 1.5}):
            with pytest.raises(RequestValidationError):
                task_preparation.ManualQuestionPreparationRequest(expected_workflow_revision=result["workflow_revision"], **payload)
        assert not any("manual-review/{source_token}" in route.path for route in task_preparation.router.routes)
        request = task_preparation.ManualQuestionPreparationRequest(expected_workflow_revision=result["workflow_revision"], question_count=2)
        stranger, _ = _seed_task()
        refused = await task_preparation.manually_complete_question_preparation(task, queued["job_id"], request, current=SimpleNamespace(id=stranger))
        assert refused.status_code == 404
        stale = await task_preparation.manually_complete_question_preparation(task, queued["job_id"],
            request.model_copy(update={"expected_workflow_revision": paused["workflow_revision"]}), current=SimpleNamespace(id=owner))
        assert stale.status_code == 409
        result = await task_preparation.manually_complete_question_preparation(task, queued["job_id"], request, current=SimpleNamespace(id=owner))
        assert result["first_question_id"] == "manual-1"
        questions = assignment_repository.list_questions(task, teacher_id=owner)
        assert len(questions) == 2 and all(not q.stem and not q.criterion for q in questions)
        assert all(q.type == "" for q in questions)
        assert "question_manual_completion_required" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
        # A new count cannot replace rows already being edited, even with a current revision.
        duplicate = await task_preparation.manually_complete_question_preparation(task, queued["job_id"],
            request.model_copy(update={"expected_workflow_revision": result["workflow_revision"]}), current=SimpleNamespace(id=owner))
        assert duplicate.status_code == 409
        with pytest.raises(ValidationError):
            task_facade.update_problem(task_id=task, owner_id=owner, q_id="manual-1", patch={"review_status": "confirmed"})
        for q in questions:
            task_facade.update_problem(task_id=task, owner_id=owner, q_id=q.q_id, patch={"number": f"A-{q.number}"})
            task_facade.update_problem(task_id=task, owner_id=owner, q_id=q.q_id, patch={"type": "concept", "stem": "Teacher-authored question"})
            task_facade.update_problem(task_id=task, owner_id=owner, q_id=q.q_id, patch={"criterion": "Award 10 points for the solution.", "max_score": 10})
            task_facade.update_problem(task_id=task, owner_id=owner, q_id=q.q_id, patch={"review_status": "confirmed"})
        projected = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]["manual-1"]
        assert projected["number"] == "A-1" and projected["question_structure"]["major_number"] == "A-1"
        assert "question_manual_completion_required" not in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
        # Keep the compatibility read of previously saved teacher-entered scores.
        from backend.db.models import AssignmentQuestionRecord
        with session_scope() as session:
            stored = session.get(AssignmentQuestionRecord, questions[0].id)
            stored.source = {**stored.source, "presentation": {
                **stored.source["presentation"], "max_score_source": "teacher_input"}}
        projected = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]["manual-1"]
        assert projected["max_score_source"] == "teacher_edited" and projected["stem"] == "Teacher-authored question"

    else:
        questions = assignment_repository.list_questions(task, teacher_id=owner)
        assert questions[0].reference_answer
        assert bool(questions[1].reference_answer) == (stopped_after == "q2")
    original = workflow_repository.get_operation(queued["job_id"], owner_id=owner)
    assert original.status == "error" and original.error_code == "operation_cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("fill_reference", [False, True])
async def test_manual_completion_preserves_success_and_requires_filling_then_confirmation(monkeypatch, fill_reference):
    owner, task, job, revision, calls = await failed_preparation(monkeypatch)
    before_calls = list(calls)
    refused = await manual(owner, task, job, revision, question_count=2)
    assert refused.status_code == 409
    assert not assignment_repository.list_questions(task, teacher_id=owner)
    result = await manual(owner, task, job, revision)
    assert isinstance(result, dict), getattr(result, "body", result)
    assert result["first_question_id"] == "q2"
    assert calls == before_calls


    q1, q2 = assignment_repository.list_questions(task, teacher_id=owner)
    assert q1.reference_answer and q1.criterion
    assert not q2.reference_answer and q2.criterion == ""
    assert workflow_repository.get_workflow(task, owner_id=owner).presentation_status == "problems_ready"
    failed = workflow_repository.get_operation(job, owner_id=owner)
    assert failed.status == "error" and failed.error_code
    assert failed.checkpoint["manual_completion_question_ids"] == ["q2"]
    assert "question_manual_completion_required" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
    with pytest.raises(ValidationError, match="Fix the failed recognition"):
        task_facade.update_problem(task_id=task, owner_id=owner, q_id="q2", patch={"review_status": "confirmed"})
    task_facade.update_problem(task_id=task, owner_id=owner, q_id="q2",
        patch={"criterion": "Award 10 points for the solution.", **({"reference_answer": "Teacher solution"} if fill_reference else {})})
    assert "question_manual_completion_required" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
    task_facade.update_problem(task_id=task, owner_id=owner, q_id="q2", patch={"review_status": "confirmed"})
    assert "question_manual_completion_required" not in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
    # Later edits require a fresh confirmation, and removing a marker cannot bypass it.
    task_facade.update_problem(task_id=task, owner_id=owner, q_id="q2", patch={"criterion": " ", "preparation_issues": []})
    assert "question_manual_completion_required" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
    with pytest.raises(ValidationError):
        task_facade.update_problem(task_id=task, owner_id=owner, q_id="q2", patch={"review_status": "confirmed"})
    duplicate = await manual(owner, task, job, revision)
    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_manual_handoff_rechecks_latest_operation_after_reading_artifacts(monkeypatch):
    from backend.services import question_preparation_manual
    owner, task, job, revision, _ = await failed_preparation(monkeypatch)
    original = question_preparation_manual.recover_manual_question_packages

    def raced(operation):
        result = original(operation)
        workflow_repository.update_workflow(task, owner_id=owner, last_failed_job_id=None)
        return result

    monkeypatch.setattr(question_preparation_manual, "recover_manual_question_packages", raced)
    response = await manual(owner, task, job, revision)
    assert response.status_code == 409
    assert not assignment_repository.list_questions(task, teacher_id=owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["stale", "other_owner", "missing_base", "newer_operation"])
@pytest.mark.parametrize("question_count", [None, 2])
async def test_manual_completion_rejects_stale_foreign_or_unverified_results(monkeypatch, change, question_count):
    owner, task, job, revision, calls = await failed_preparation(monkeypatch)
    before_calls = list(calls)
    actor = owner
    if change == "stale":
        revision -= 1
    elif change == "other_owner":
        actor, _ = _seed_task()
    elif change == "missing_base":
        operation = workflow_repository.get_operation(job, owner_id=owner)
        with session_scope() as session:
            record = session.get(workflow_repository.WorkflowOperationRecord, job)
            record.checkpoint = {**operation.checkpoint, "questions_extracted_artifact_id": "unverified-artifact"}
    else:
        workflow_repository.update_workflow(task, owner_id=owner, last_failed_job_id=None)
    response = await manual(actor, task, job, revision, question_count=question_count)
    assert response.status_code in {404, 409, 422}, response.body
    assert not assignment_repository.list_questions(task, teacher_id=owner)
    assert calls == before_calls


@pytest.mark.asyncio
async def test_all_question_units_failed_can_enter_manual_review(monkeypatch):
    owner, task, job, revision, calls = await failed_preparation(monkeypatch, failure_questions=["q1", "q2"])
    before_calls = list(calls)
    result = await manual(owner, task, job, revision)
    assert result["status"] == "ready_for_review" and calls == before_calls
    questions = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
    assert len(questions) == 2 and all(not q["criterion"] for q in questions.values())
    assert "question_manual_completion_required" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "empty", "paused"])
@pytest.mark.parametrize("enter_review_first", [False, True])
async def test_empty_worker_projection_allows_manual_count_and_completion(monkeypatch, outcome, enter_review_first):
    import asyncio
    from unittest.mock import AsyncMock
    from backend.agents import incremental_question_preparation as incremental
    from backend.services.task_execution import stop_task_run

    owner, task = _seed_task()
    _, queued, _ = await _queue_question_preparation(owner, task, source_text="Unrecognized source text.")
    job = queued["job_id"]
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _: _RecoveryRegistry())
    extract = AsyncMock(return_value={}, side_effect=(
        ValidationError("Invalid response", code="provider_response_invalid") if outcome == "failed"
        else asyncio.CancelledError() if outcome == "paused" else None))
    monkeypatch.setattr(incremental, "_extract_problems_call", extract)
    generate = AsyncMock(side_effect=AssertionError("No question is available for generation"))
    monkeypatch.setattr(incremental, "generate_missing_question_materials", generate)
    operation = _claim(owner, job, "empty-manual")
    if outcome == "paused":
        with pytest.raises(asyncio.CancelledError):
            await task_preparation.run_durable_question_preparation(operation)
        workflow = workflow_repository.get_workflow(task, owner_id=owner)
        stop_task_run(task_id=task, owner_id=owner, job_id=job, expected_revision=workflow.workflow_revision)
    else:
        await task_preparation.run_durable_question_preparation(operation)
    saved = workflow_repository.get_operation(job, owner_id=owner)
    assert saved.status == "error" and saved.checkpoint["incremental_view_from_units"] is True
    assert not saved.checkpoint.get("question_ids")
    assert extract.await_count > 0
    extract_count = extract.await_count
    extract.side_effect = AssertionError("Manual processing must never invoke AI")
    revision = workflow_repository.get_workflow(task, owner_id=owner).workflow_revision
    if enter_review_first:
        result = await manual(owner, task, job, revision)
        assert result["status"] == "ready_for_review", getattr(result, "body", result)
        revision = result["workflow_revision"]
        reopened = task_facade.get_task(task_id=task, owner_id=owner)
        assert reopened["manual_question_count_required"] and not reopened["problem_data"]
        repeated = await manual(owner, task, job, revision)
        assert repeated["workflow_revision"] == revision

    request = task_preparation.ManualQuestionPreparationRequest(expected_workflow_revision=revision, question_count=2)
    result = await task_preparation.manually_complete_question_preparation(task, job, request, current=SimpleNamespace(id=owner))
    assert isinstance(result, dict), getattr(result, "body", result)
    assert result["first_question_id"] == "manual-1"
    reopened = task_facade.get_task(task_id=task, owner_id=owner)
    assert not reopened["manual_question_count_required"]
    assert list(reopened["problem_data"]) == ["manual-1", "manual-2"]
    assert all(not row["stem"] and not row["criterion"] and not row["reference_answer"]
               for row in reopened["problem_data"].values())
    assert "question_manual_completion_required" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
    for q_id in reopened["problem_data"]:
        task_facade.update_problem(task_id=task, owner_id=owner, q_id=q_id,
            patch={"stem": "Teacher-authored question", "type": "other", "criterion": "Award 10 points for the solution.",
                   "reference_answer": "Teacher-authored answer"})
        task_facade.update_problem(task_id=task, owner_id=owner, q_id=q_id, patch={"review_status": "confirmed"})
    edited = task_facade.get_task(task_id=task, owner_id=owner)
    assert "question_manual_completion_required" not in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
    for revision in (request.expected_workflow_revision, edited["workflow_revision"]):
        duplicate = await task_preparation.manually_complete_question_preparation(task, job,
            request.model_copy(update={"expected_workflow_revision": revision}), current=SimpleNamespace(id=owner))
        assert duplicate.status_code == 409
    assert task_facade.get_task(task_id=task, owner_id=owner)["problem_data"] == edited["problem_data"]
    assert extract.await_count == extract_count
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_count_cannot_replace_frozen_targets_before_worker_starts(monkeypatch):
    import json
    from backend.services.task_execution import stop_task_run

    preflight = task_preparation.preflight_problem_source
    async def targeted(**kwargs):
        kwargs["recognition_options"] = json.dumps({"targets": ["3", "7"]})
        return await preflight(**kwargs)
    monkeypatch.setattr(task_preparation, "preflight_problem_source", targeted)
    owner, task = _seed_task()
    _, queued, _ = await _queue_question_preparation(owner, task)
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    stop_task_run(task_id=task, owner_id=owner, job_id=queued["job_id"], expected_revision=workflow.workflow_revision)
    revision = workflow_repository.get_workflow(task, owner_id=owner).workflow_revision
    refused = await manual(owner, task, queued["job_id"], revision, question_count=2)
    assert refused.status_code == 409
    result = await manual(owner, task, queued["job_id"], revision)
    assert result["status"] == "ready_for_review"
    rows = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
    assert [row["number"] for row in rows.values()] == ["3", "7"]


@pytest.mark.asyncio
async def test_regular_recognized_question_number_uses_the_same_editor(monkeypatch):
    owner, task, job, revision, _ = await failed_preparation(monkeypatch)
    await manual(owner, task, job, revision)
    original = task_facade.get_task(task_id=task, owner_id=owner)
    changed = task_facade.update_problem(task_id=task, owner_id=owner, q_id="q1",
        expected_revision=original["workflow_revision"], patch={"number": "1.1.5", "type": "other"})
    question = changed["problem"]
    assert question["number"] == "1.1.5" and question["q_id"] == "q1"
    assert question["type"] == "其他"
    assert question["stem"] == original["problem_data"]["q1"]["stem"]
    assert question["reference_answer"] == original["problem_data"]["q1"]["reference_answer"]
    assert question["criterion"] == original["problem_data"]["q1"]["criterion"]
    assert question["question_structure"]["major_number"] == "1.1.5"
    for value in (" ", "x" * 65):
        with pytest.raises(ValidationError):
            task_facade.update_problem(task_id=task, owner_id=owner, q_id="q1", patch={"number": value})
        with pytest.raises(ValidationError):
            task_facade.update_problem(task_id=task, owner_id=owner, q_id="q1", patch={"type": value})
