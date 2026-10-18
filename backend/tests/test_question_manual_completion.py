from types import SimpleNamespace

import pytest

from backend.api import task_preparation
from backend.db import assignment_repository, workflow_repository
from backend.db.session import session_scope
from backend.domain.errors import ValidationError
from backend.services import task_facade
from backend.tests.test_question_preparation_recovery import (
    _RecoveryRegistry, _claim, _fake_preparer, _queue_question_preparation, _seed_task,
)


async def failed_preparation(monkeypatch, failed_question="q2"):
    owner, task = _seed_task()
    _, response, _ = await _queue_question_preparation(owner, task, source_text="1. First.\n2. Second.")
    calls = []
    monkeypatch.setattr(task_facade, "_registry_for_owner", lambda _owner: _RecoveryRegistry())
    monkeypatch.setattr(task_preparation, "prepare_question_packages", _fake_preparer(calls,
        question_count=2, known_failure_question=failed_question, record_base_provider_calls=True))
    await task_preparation.run_durable_question_preparation(_claim(owner, response["job_id"], "manual-test"))
    workflow = workflow_repository.get_workflow(task, owner_id=owner)
    return owner, task, response["job_id"], workflow.workflow_revision, calls


async def manual(owner, task, job, revision):
    result = await task_preparation.manually_complete_question_preparation(task, job,
        task_preparation.ManualQuestionPreparationRequest(expected_workflow_revision=revision),
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
        monkeypatch.setattr(task_preparation, "prepare_question_packages", _fake_preparer(calls,
            question_count=2, cancel_after_question=stopped_after, record_base_provider_calls=True))
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
        assert result["status"] == "sources_pending" and result["first_question_id"] is None
        assert task_facade.get_task(task_id=task, owner_id=owner)["status"] == "problems_paused"
        assert "questions_required" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
        original_source = task_preparation.manual_question_source_content(task, queued["job_id"], source["source_token"], current=SimpleNamespace(id=owner))
        assert original_source.body.decode() == "1. First.\n2. Second."
        stranger, _ = _seed_task()
        assert task_preparation.manual_question_source_content(task, queued["job_id"], source["source_token"], current=SimpleNamespace(id=stranger)).status_code == 404
        question = task_preparation.ManualQuestionDraft(q_id="teacher-q", stem="Teacher-authored question",
            reference_answer="Teacher solution", criterion="Award 10 points for the solution.")
        arguments = dict(manual_questions=[question], expected_workflow_revision=paused["workflow_revision"])
        refused = await task_preparation.manually_complete_question_preparation(task, queued["job_id"],
            task_preparation.ManualQuestionPreparationRequest(**arguments), current=SimpleNamespace(id=owner))
        assert refused.status_code == 422 and not assignment_repository.list_questions(task, teacher_id=owner)
        for invalid in ([question, question], [question.model_copy(update={"stem": "(a) Explain A.\n(b) Explain B.", "criterion": "(a) 4 分\n(b) 4 分"})]):
            response = await task_preparation.manually_complete_question_preparation(task, queued["job_id"],
                task_preparation.ManualQuestionPreparationRequest(expected_workflow_revision=paused["workflow_revision"],
                    manual_questions=invalid, completed_source_tokens=[source["source_token"]]), current=SimpleNamespace(id=owner))
            assert response.status_code == 422
        result = await task_preparation.manually_complete_question_preparation(task, queued["job_id"],
            task_preparation.ManualQuestionPreparationRequest(**arguments, completed_source_tokens=[source["source_token"]]),
            current=SimpleNamespace(id=owner))
        assert result["first_question_id"] == "teacher-q"
        assert "question_manual_completion_required" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
        assert assignment_repository.list_questions(task, teacher_id=owner)[0].stem == question.stem
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
async def test_manual_completion_rejects_stale_foreign_or_unverified_results(monkeypatch, change):
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
            record.checkpoint = {**operation.checkpoint, "aligned_base_artifact_id": "unverified-artifact"}
    else:
        workflow_repository.update_workflow(task, owner_id=owner, last_failed_job_id=None)
    response = await manual(actor, task, job, revision)
    assert response.status_code in {404, 409, 422}, response.body
    assert not assignment_repository.list_questions(task, teacher_id=owner)
    assert calls == before_calls


@pytest.mark.asyncio
async def test_all_question_units_failed_can_enter_manual_review(monkeypatch):
    owner, task, job, revision, calls = await failed_preparation(monkeypatch, failed_question="q1")
    before_calls = list(calls)
    result = await manual(owner, task, job, revision)
    assert result["status"] == "ready_for_review" and calls == before_calls
    questions = task_facade.get_task(task_id=task, owner_id=owner)["problem_data"]
    assert len(questions) == 2 and all(not q["criterion"] for q in questions.values())
    assert "question_manual_completion_required" in task_facade.grading_readiness(task_id=task, owner_id=owner)["blocking_issues"]
