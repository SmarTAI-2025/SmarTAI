from __future__ import annotations

import io
import json
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from starlette.datastructures import Headers

from backend.api import task_preparation, tasks
from backend.db import assignment_repository, grading_repository, workflow_repository
from backend.db.models import (
    AssignmentQuestionRecord,
    AssignmentRecord,
    CourseRecord,
    UserRecord,
)
from backend.db.session import session_scope
from backend.domain.errors import (
    InvalidTransition,
    NotFound,
    ValidationError,
    VersionConflict,
)
from backend.services import task_facade
from backend.tools.structured_llm import PermanentLLMError, RateLimitError, TransientLLMError


def _seed_task(*, with_question: bool = False) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:10]
    owner_id = f"teacher_{suffix}"
    course_id = f"course_{suffix}"
    task_id = f"assignment_{suffix}"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id, username=owner_id, password_hash="hash",
            role="teacher", is_active=True,
        ))
        session.flush()
        session.add(CourseRecord(
            id=course_id, name="Course", code=f"C-{suffix}",
            teacher_id=owner_id,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=task_id, course_id=course_id, teacher_id=owner_id,
            name="Assignment", status="draft", version=1,
        ))
    workflow_repository.ensure_workflow(
        assignment_id=task_id, owner_id=owner_id
    )
    if with_question:
        assignment_repository.add_question(
            task_id, teacher_id=owner_id, q_id="q1", order_index=0,
            type="short", stem="Original", criterion="", max_score=10,
        )
    return owner_id, task_id


class _Registry:
    provider = SimpleNamespace(
        provider_id="test-provider",
        supports_vision=False,
    )

    def pick_default(self):
        return self.provider

    def pick_default_id(self):
        return "test-provider"

    def get(self, provider_id):
        return self.provider if provider_id == "test-provider" else None

    def uses_shared_pool(self):
        return True

    def list_configs(self):
        return [{"provider_id": "test-provider", "enabled": True}]


class _BackgroundTasks:
    def __init__(self) -> None:
        self.calls: list[tuple[object, tuple, dict]] = []

    def add_task(self, func, *args, **kwargs) -> None:
        self.calls.append((func, args, kwargs))


def _problem(stem: str) -> dict[str, dict]:
    return {
        "q1": {
            "q_id": "q1", "number": "1", "type": "short",
            "stem": stem, "criterion": "", "max_score": 10,
        }
    }


def _tombstone_task(task_id: str) -> None:
    with session_scope() as session:
        assignment = session.get(AssignmentRecord, task_id)
        assert assignment is not None
        assignment.deletion_requested_at = time.time()


def test_grading_terminal_state_releases_task_workflow_atomically():
    owner_id, task_id = _seed_task()
    run = grading_repository.create_run(
        task_id, teacher_id=owner_id, total_submissions=0
    )
    grading_repository.claim_lease(
        run.id, worker_id="worker", lease_seconds=60
    )
    workflow_repository.update_workflow(
        task_id,
        owner_id=owner_id,
        bump_revision=False,
        presentation_status="grading",
        grading_job_id=run.id,
        active_operation="grading",
        active_job_id=run.id,
    )

    grading_repository.mark_failed(
        run.id, worker_id="worker", error_message="provider_timeout"
    )

    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert workflow.presentation_status == "error"
    assert workflow.active_operation is None
    assert workflow.active_job_id is None
    assert workflow.grading_job_id == run.id
    assert workflow.last_failed_job_id == run.id
    assert workflow.error_code is None
    assert task_facade.get_task(
        task_id=task_id, owner_id=owner_id, full=False
    )["error"] == "provider_timeout"


def test_legacy_failed_grading_marker_is_repaired_on_read():
    owner_id, task_id = _seed_task()
    run = grading_repository.create_run(
        task_id, teacher_id=owner_id, total_submissions=0
    )
    grading_repository.claim_lease(
        run.id, worker_id="worker", lease_seconds=60
    )
    grading_repository.mark_failed(
        run.id, worker_id="worker", error_message="provider_timeout"
    )
    # Simulate the historical dirty row found in the user's local database.
    workflow_repository.update_workflow(
        task_id,
        owner_id=owner_id,
        bump_revision=False,
        presentation_status="grading",
        grading_job_id=run.id,
        active_operation="grading",
        active_job_id=run.id,
        last_failed_job_id=None,
        error_code=None,
    )

    task = task_facade.get_task(task_id=task_id, owner_id=owner_id, full=False)

    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert task["status"] == "error"
    assert task["error"] == "provider_timeout"
    assert workflow.active_job_id is None
    assert workflow.last_failed_job_id == run.id
    assert workflow.error_code == "provider_timeout"
    assert task_facade._ensure_no_other_active_operation(
        task_id=task_id,
        owner_id=owner_id,
        operation_type="question_preparation",
        input_hash="new-input",
    )[1] is None


def test_missing_active_grading_run_reports_persistence_failure():
    owner_id, task_id = _seed_task()
    workflow_repository.update_workflow(
        task_id,
        owner_id=owner_id,
        bump_revision=False,
        presentation_status="grading",
        grading_job_id="run-missing",
        active_operation="grading",
        active_job_id="run-missing",
    )

    task = task_facade.get_task(task_id=task_id, owner_id=owner_id, full=False)

    assert task["status"] == "error"
    assert task["error"] == "grading_persistence_failed"
    assert task["last_failed_job_id"] == "run-missing"


def test_task_rename_does_not_advance_workflow_revision():
    owner_id, task_id = _seed_task()

    updated = task_facade.update_task(
        task_id=task_id, owner_id=owner_id, name="Renamed"
    )

    assert updated["name"] == "Renamed"
    assert updated["workflow_revision"] == 0


def test_confirmed_upstream_restart_can_cancel_active_grading():
    owner_id, task_id = _seed_task()
    run = grading_repository.create_run(
        task_id, teacher_id=owner_id, total_submissions=0
    )
    workflow_repository.update_workflow(
        task_id,
        owner_id=owner_id,
        bump_revision=False,
        presentation_status="grading",
        grading_job_id=run.id,
        active_operation="grading",
        active_job_id=run.id,
    )

    workflow, active = task_facade._ensure_no_other_active_operation(
        task_id=task_id,
        owner_id=owner_id,
        operation_type="question_preparation",
        input_hash="replacement",
        allow_supersede=True,
    )

    assert active is None
    assert workflow.active_job_id is None
    assert workflow.grading_job_id is None
    assert grading_repository.get_run(run.id, actor_id=owner_id).status == "cancelled"


def test_replacing_questions_deactivates_students_and_invalidates_downstream():
    owner_id, task_id = _seed_task(with_question=True)
    student_id = f"student_{uuid.uuid4().hex[:10]}"
    with session_scope() as session:
        session.add(UserRecord(
            id=student_id,
            username=student_id,
            password_hash="hash",
            role="student",
            is_active=True,
        ))
    workflow_repository.upsert_student_presentation(
        assignment_id=task_id,
        student_id=student_id,
        display_student_id="S001",
        display_name="Student",
    )
    workflow_repository.update_workflow(
        task_id,
        owner_id=owner_id,
        bump_revision=False,
        presentation_status="graded",
        grading_job_id="obsolete-run",
        submission_file_name="old.zip",
        analysis_status="ready",
    )

    task_facade._replace_draft_questions(
        task_id,
        owner_id,
        _problem("Replacement"),
        "new.pdf",
        expected_workflow_revision=0,
        replace_confirmed=True,
    )

    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    presentation = workflow_repository.list_student_presentations(task_id)[student_id]
    assert workflow.presentation_status == "problems_ready"
    assert workflow.grading_job_id is None
    assert workflow.submission_file_name is None
    assert workflow.analysis_status == "not_generated"
    assert presentation.is_active is False


def test_replacing_submissions_preserves_questions_and_invalidates_grading():
    owner_id, task_id = _seed_task(with_question=True)
    workflow_repository.update_workflow(
        task_id,
        owner_id=owner_id,
        bump_revision=False,
        presentation_status="graded",
        grading_job_id="obsolete-run",
        analysis_status="ready",
    )

    imported = task_facade._commit_imported_submissions(
        task_id=task_id,
        owner_id=owner_id,
        course_id=assignment_repository.get_assignment(
            task_id, actor_id=owner_id
        ).course_id,
        students=[],
        replace_existing=True,
        expected_workflow_revision=0,
        submission_file_name="replacement.zip",
    )

    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert imported == 0
    assert len(assignment_repository.list_questions(
        task_id, teacher_id=owner_id
    )) == 1
    assert workflow.presentation_status == "submissions_ready"
    assert workflow.submission_file_name == "replacement.zip"
    assert workflow.grading_job_id is None
    assert workflow.analysis_status == "not_generated"


def test_editing_student_answer_atomically_invalidates_current_grading():
    owner_id, task_id = _seed_task(with_question=True)
    assignment = assignment_repository.get_assignment(task_id, actor_id=owner_id)
    task_facade._commit_imported_submissions(
        task_id=task_id,
        owner_id=owner_id,
        course_id=assignment.course_id,
        students=[{
            "stu_id": "S001",
            "stu_name": "Student",
            "source_filename": "old.txt",
            "stu_ans": [{"q_id": "q1", "content": "old answer"}],
        }],
        expected_workflow_revision=0,
        submission_file_name="old.txt",
    )
    workflow_repository.update_workflow(
        task_id,
        owner_id=owner_id,
        bump_revision=False,
        presentation_status="graded",
        grading_job_id="obsolete-run",
        analysis_status="ready",
    )

    response = task_facade.update_student_answer(
        task_id=task_id,
        owner_id=owner_id,
        display_student_id="S001",
        q_id="q1",
        patch={"content": "corrected answer", "review_status": "confirmed"},
        expected_revision=1,
    )

    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert response["answer"]["content"] == "corrected answer"
    assert response["workflow_revision"] == 2
    assert workflow.presentation_status == "submissions_ready"
    assert workflow.grading_job_id is None
    assert workflow.analysis_status == "not_generated"


def test_question_replace_requires_confirmation_and_cas_is_atomic():
    owner_id, task_id = _seed_task()
    assert task_facade._replace_draft_questions(
        task_id, owner_id, _problem("First"), "first.txt",
        expected_workflow_revision=0,
    ) == 1

    with pytest.raises(InvalidTransition) as unconfirmed:
        task_facade._replace_draft_questions(
            task_id, owner_id, _problem("Unconfirmed overwrite"), "second.txt",
            expected_workflow_revision=1,
        )
    assert unconfirmed.value.code == "replacement_confirmation_required"

    with pytest.raises(VersionConflict) as stale:
        task_facade._replace_draft_questions(
            task_id, owner_id, _problem("Stale overwrite"), "second.txt",
            expected_workflow_revision=0, replace_confirmed=True,
        )
    assert stale.value.code == "stale_revision"
    assert assignment_repository.list_questions(
        task_id, teacher_id=owner_id
    )[0].stem == "First"
    assert workflow_repository.get_workflow(
        task_id, owner_id=owner_id
    ).workflow_revision == 1

    assert task_facade._replace_draft_questions(
        task_id, owner_id, _problem("Confirmed overwrite"), "second.txt",
        expected_workflow_revision=1, replace_confirmed=True,
    ) == 2
    assert assignment_repository.list_questions(
        task_id, teacher_id=owner_id
    )[0].stem == "Confirmed overwrite"


def test_atomic_question_batch_rolls_back_before_any_partial_write():
    owner_id, task_id = _seed_task(with_question=True)
    job, _ = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type="material_import", input_hash=uuid.uuid4().hex,
        expires_at=time.time() + 60,
    )
    workflow_repository.update_operation(
        job.id, owner_id=owner_id, expected_attempt=job.attempt, status="ready"
    )

    with pytest.raises(ValidationError) as invalid:
        task_facade.apply_question_patches_atomic(
            task_id=task_id, owner_id=owner_id,
            expected_workflow_revision=0,
            patches=[
                {"q_id": "q1", "fields": {"criterion": "new"}},
                {"q_id": "missing", "fields": {"criterion": "bad"}},
            ],
            operation_id=job.id, expected_operation_attempt=job.attempt,
            required_operation_status="ready",
            final_operation_status="applied", operation_payload={},
        )
    assert invalid.value.code == "stale_revision"
    question = assignment_repository.list_questions(
        task_id, teacher_id=owner_id
    )[0]
    assert question.criterion == ""
    assert workflow_repository.get_workflow(
        task_id, owner_id=owner_id
    ).workflow_revision == 0
    assert workflow_repository.get_operation(
        job.id, owner_id=owner_id
    ).status == "ready"

    revised = task_facade.apply_question_patches_atomic(
        task_id=task_id, owner_id=owner_id, expected_workflow_revision=0,
        patches=[{"q_id": "q1", "fields": {"criterion": "new"}}],
        operation_id=job.id, expected_operation_attempt=job.attempt,
        required_operation_status="ready",
        final_operation_status="applied",
        operation_payload={"applied_candidate_ids": ["candidate-1"]},
    )
    assert revised == 1
    assert assignment_repository.list_questions(
        task_id, teacher_id=owner_id
    )[0].criterion == "new"
    applied = workflow_repository.get_operation(job.id, owner_id=owner_id)
    assert applied.status == "applied"
    assert applied.payload["applied_candidate_ids"] == ["candidate-1"]


def test_task_tombstone_fences_atomic_question_patch_publication():
    owner_id, task_id = _seed_task(with_question=True)
    job, _ = workflow_repository.create_operation(
        assignment_id=task_id,
        owner_id=owner_id,
        operation_type="material_import",
        input_hash=uuid.uuid4().hex,
        expires_at=time.time() + 60,
    )
    workflow_repository.update_operation(
        job.id,
        owner_id=owner_id,
        expected_attempt=job.attempt,
        status="ready",
    )
    _tombstone_task(task_id)

    with pytest.raises(NotFound):
        task_facade.apply_question_patches_atomic(
            task_id=task_id,
            owner_id=owner_id,
            expected_workflow_revision=0,
            patches=[{"q_id": "q1", "fields": {"criterion": "late"}}],
            operation_id=job.id,
            expected_operation_attempt=job.attempt,
            required_operation_status="ready",
            final_operation_status="applied",
            operation_payload={},
        )

    with session_scope() as session:
        question = session.scalar(
            select(AssignmentQuestionRecord).where(
                AssignmentQuestionRecord.assignment_id == task_id,
                AssignmentQuestionRecord.q_id == "q1",
            )
        )
        assert question is not None
        assert question.criterion == ""
    assert workflow_repository.get_workflow(
        task_id, owner_id=owner_id
    ).workflow_revision == 0
    assert workflow_repository.get_operation(
        job.id, owner_id=owner_id
    ).status == "ready"


def test_task_tombstone_fences_problem_extraction_publication():
    owner_id, task_id = _seed_task(with_question=True)
    job, _ = workflow_repository.create_operation(
        assignment_id=task_id,
        owner_id=owner_id,
        operation_type="problem_extraction",
        input_hash=uuid.uuid4().hex,
        expires_at=time.time() + 60,
    )
    workflow_repository.update_operation(
        job.id,
        owner_id=owner_id,
        expected_attempt=job.attempt,
        status="running",
    )
    _tombstone_task(task_id)

    with pytest.raises(NotFound):
        task_facade._replace_draft_questions(
            task_id,
            owner_id,
            _problem("late replacement"),
            "late.txt",
            expected_workflow_revision=0,
            replace_confirmed=True,
            operation_id=job.id,
            expected_operation_attempt=job.attempt,
        )

    with session_scope() as session:
        question = session.scalar(
            select(AssignmentQuestionRecord).where(
                AssignmentQuestionRecord.assignment_id == task_id,
                AssignmentQuestionRecord.q_id == "q1",
            )
        )
        assert question is not None
        assert question.stem == "Original"
    assert workflow_repository.get_operation(
        job.id, owner_id=owner_id
    ).status == "running"


def test_task_tombstone_fences_submission_recognition_publication():
    owner_id, task_id = _seed_task(with_question=True)
    with session_scope() as session:
        assignment = session.get(AssignmentRecord, task_id)
        assert assignment is not None
        course_id = assignment.course_id
    job, _ = workflow_repository.create_operation(
        assignment_id=task_id,
        owner_id=owner_id,
        operation_type="submission_recognition",
        input_hash=uuid.uuid4().hex,
        expires_at=time.time() + 60,
    )
    workflow_repository.update_operation(
        job.id,
        owner_id=owner_id,
        expected_attempt=job.attempt,
        status="running",
    )
    _tombstone_task(task_id)

    with pytest.raises(NotFound):
        task_facade._commit_imported_submissions(
            task_id=task_id,
            owner_id=owner_id,
            course_id=course_id,
            students=[],
            expected_workflow_revision=0,
            operation_id=job.id,
            expected_operation_attempt=job.attempt,
        )

    with session_scope() as session:
        assignment = session.get(AssignmentRecord, task_id)
        assert assignment is not None
        assert assignment.status == "draft"
    assert workflow_repository.get_operation(
        job.id, owner_id=owner_id
    ).status == "running"


def test_task_tombstone_fences_auxiliary_plan_publication():
    owner_id, task_id = _seed_task(with_question=True)
    job, _ = workflow_repository.create_operation(
        assignment_id=task_id,
        owner_id=owner_id,
        operation_type="material_import",
        input_hash=uuid.uuid4().hex,
        expires_at=time.time() + 60,
    )
    workflow_repository.update_operation(
        job.id,
        owner_id=owner_id,
        expected_attempt=job.attempt,
        status="running",
    )
    workflow_repository.update_workflow(
        task_id,
        owner_id=owner_id,
        bump_revision=False,
        active_operation="material_import",
        active_job_id=job.id,
    )
    _tombstone_task(task_id)

    with pytest.raises(NotFound):
        task_facade.complete_planning_operation_atomic(
            task_id=task_id,
            owner_id=owner_id,
            expected_workflow_revision=0,
            operation_id=job.id,
            expected_operation_attempt=job.attempt,
            payload={},
            progress={},
        )

    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert workflow.active_job_id == job.id
    assert workflow_repository.get_operation(
        job.id, owner_id=owner_id
    ).status == "running"


def test_atomic_question_batch_rejects_non_finite_operation_payload():
    owner_id, task_id = _seed_task(with_question=True)
    job, _ = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type="material_import", input_hash=uuid.uuid4().hex,
        expires_at=time.time() + 60,
    )
    workflow_repository.update_operation(
        job.id, owner_id=owner_id, expected_attempt=job.attempt, status="ready"
    )

    with pytest.raises(ValidationError) as invalid:
        task_facade.apply_question_patches_atomic(
            task_id=task_id, owner_id=owner_id,
            expected_workflow_revision=0,
            patches=[{"q_id": "q1", "fields": {"criterion": "new"}}],
            operation_id=job.id, expected_operation_attempt=job.attempt,
            required_operation_status="ready",
            final_operation_status="applied",
            operation_payload={"confidence": float("nan")},
        )

    assert invalid.value.code == "invalid_operation_payload"
    assert assignment_repository.list_questions(
        task_id, teacher_id=owner_id
    )[0].criterion == ""
    assert workflow_repository.get_workflow(
        task_id, owner_id=owner_id
    ).workflow_revision == 0
    assert workflow_repository.get_operation(
        job.id, owner_id=owner_id
    ).status == "ready"


def test_retry_attempt_rejects_every_stale_operation_update():
    owner_id, task_id = _seed_task()
    input_hash = uuid.uuid4().hex
    first, created = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type="problem_extraction", input_hash=input_hash,
        expires_at=time.time() + 60,
    )
    assert created is True
    assert first.attempt == 1
    workflow_repository.update_operation(
        first.id, owner_id=owner_id, expected_attempt=first.attempt,
        status="running", progress={"worker": "old"},
    )
    workflow_repository.update_operation(
        first.id, owner_id=owner_id, expected_attempt=first.attempt,
        status="error", error_code="workflow_failed", completed_at=time.time(),
    )

    retried, retry_created = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type="problem_extraction", input_hash=input_hash,
        expires_at=time.time() + 60,
    )
    assert retry_created is True
    assert retried.id == first.id
    assert retried.attempt == 2
    assert retried.status == "pending"
    assert retried.progress == {}

    for stale_changes in (
        {"status": "done", "completed_at": time.time()},
        {"status": "error", "error_code": "old_worker_failed"},
        {"progress": {"worker": "old", "completed_steps": 99}},
    ):
        with pytest.raises(VersionConflict) as stale:
            workflow_repository.update_operation(
                first.id, owner_id=owner_id, expected_attempt=first.attempt,
                **stale_changes,
            )
        assert stale.value.code == "stale_operation_attempt"

    current = workflow_repository.get_operation(first.id, owner_id=owner_id)
    assert current.attempt == retried.attempt
    assert current.status == "pending"
    assert current.progress == {}
    workflow_repository.update_operation(
        retried.id, owner_id=owner_id, expected_attempt=retried.attempt,
        status="running", progress={"worker": "new"},
    )
    completed = workflow_repository.update_operation(
        retried.id, owner_id=owner_id, expected_attempt=retried.attempt,
        status="done", progress={"worker": "new", "completed_steps": 1},
        completed_at=time.time(),
    )
    assert completed.attempt == 2
    assert completed.status == "done"
    assert completed.progress == {"worker": "new", "completed_steps": 1}


def test_expired_running_operation_retry_advances_attempt():
    owner_id, task_id = _seed_task()
    input_hash = uuid.uuid4().hex
    expired, created = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type="submission_recognition", input_hash=input_hash,
        expires_at=time.time() + 60,
    )
    assert created is True
    workflow_repository.update_operation(
        expired.id, owner_id=owner_id, expected_attempt=expired.attempt,
        status="running", expires_at=time.time() - 1,
    )
    workflow_repository.update_workflow(
        task_id, owner_id=owner_id,
        active_operation="submission_recognition",
        active_job_id=expired.id,
    )
    _workflow, active = task_facade._ensure_no_other_active_operation(
        task_id=task_id,
        owner_id=owner_id,
        operation_type="submission_recognition",
        input_hash=input_hash,
    )
    assert active is None

    retried, retry_created = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type="submission_recognition", input_hash=input_hash,
        expires_at=time.time() + 60,
    )
    assert retry_created is True
    assert retried.id == expired.id
    assert retried.attempt == expired.attempt + 1
    assert retried.status == "pending"
    with pytest.raises(VersionConflict) as stale:
        workflow_repository.update_operation(
            expired.id, owner_id=owner_id, expected_attempt=expired.attempt,
            status="error", error_code="old_worker_failed",
        )
    assert stale.value.code == "stale_operation_attempt"


def test_exact_facade_retry_consumes_only_its_prior_claim_revision():
    owner_id, task_id = _seed_task()
    request = {
        "task_id": task_id, "owner_id": owner_id,
        "filename": "questions.txt", "content": b"Question 1",
        "content_type": "text/plain", "registry": _Registry(),
        "expected_workflow_revision": 0,
    }
    first = task_facade.queue_task_problem_extraction(**request)
    first_operation = workflow_repository.get_operation(
        first["job_id"], owner_id=owner_id
    )
    assert first["workflow_revision"] == 1
    assert task_facade._fail_operation(
        task_id, owner_id, first_operation.id, first_operation.attempt,
        "problem_extraction_failed",
    ) is True

    retried = task_facade.queue_task_problem_extraction(**request)
    retry_operation = workflow_repository.get_operation(
        retried["job_id"], owner_id=owner_id
    )
    assert retried["status"] == "started"
    assert retried["job_id"] == first["job_id"]
    assert retried["workflow_revision"] == 2
    assert retry_operation.attempt == first_operation.attempt + 1
    assert retry_operation.payload["base_workflow_revision"] == 1
    assert task_facade._fail_operation(
        task_id, owner_id, retry_operation.id, retry_operation.attempt,
        "problem_extraction_failed",
    ) is True

    # One unrelated mutation beyond the failed attempt's own claim bump keeps
    # the original request stale; exact-retry handling is not a general bypass.
    workflow_repository.update_workflow(
        task_id, owner_id=owner_id, expected_revision=2,
    )
    with pytest.raises(VersionConflict) as stale_request:
        task_facade.queue_task_problem_extraction(**request)
    assert stale_request.value.code == "stale_revision"


def test_student_identity_conflict_does_not_consume_workflow_revision():
    owner_id, task_id = _seed_task()
    suffix = uuid.uuid4().hex[:10]
    first_student_id = f"student_a_{suffix}"
    second_student_id = f"student_b_{suffix}"
    with session_scope() as session:
        session.add_all([
            UserRecord(
                id=first_student_id, username=first_student_id,
                password_hash="hash", role="student", is_active=True,
            ),
            UserRecord(
                id=second_student_id, username=second_student_id,
                password_hash="hash", role="student", is_active=True,
            ),
        ])
    workflow_repository.upsert_student_presentation(
        assignment_id=task_id, student_id=first_student_id,
        display_student_id="S001", display_name="First",
    )
    workflow_repository.upsert_student_presentation(
        assignment_id=task_id, student_id=second_student_id,
        display_student_id="S002", display_name="Second",
    )

    with pytest.raises(ValidationError) as conflict:
        task_facade.update_student_identity(
            task_id=task_id, owner_id=owner_id,
            current_display_id="S001", new_display_id=" S002 ",
            new_display_name="Duplicate", expected_revision=0,
        )
    assert conflict.value.code == "student_identity_conflict"
    assert workflow_repository.get_workflow(
        task_id, owner_id=owner_id
    ).workflow_revision == 0

    claimed = workflow_repository.update_workflow(
        task_id, owner_id=owner_id, expected_revision=0
    )
    assert claimed.workflow_revision == 1


@pytest.mark.asyncio
async def test_extract_endpoint_queues_durable_work_and_returns_started():
    owner_id, task_id = _seed_task()
    background = _BackgroundTasks()
    upload = UploadFile(
        file=io.BytesIO(b"Question 1: example"), filename="questions.txt",
        headers=Headers({"content-type": "text/plain"}),
    )

    response = await tasks.extract_problems_endpoint(
        task_id=task_id, background_tasks=background, file=upload,
        source_token=None, confirmed_candidate_ids="[]",
        replace_confirmed=False, current=SimpleNamespace(id=owner_id),
        registry=_Registry(),
    )

    assert response["status"] == "started"
    assert background.calls == []
    operation = workflow_repository.get_operation(
        response["job_id"], owner_id=owner_id
    )
    assert operation.status == "pending"
    assert isinstance(operation.payload.get("source_id"), str)
    assert assignment_repository.list_questions(task_id, teacher_id=owner_id) == []


@pytest.mark.asyncio
async def test_submission_upload_is_bounded_before_any_operation_is_created(monkeypatch):
    background = _BackgroundTasks()
    upload = UploadFile(
        file=io.BytesIO(b"four"),
        filename="submissions.zip",
        headers=Headers({"content-type": "application/zip"}),
    )
    monkeypatch.setattr(tasks, "SUBMISSION_UPLOAD_MAX_BYTES", 3)

    with pytest.raises(HTTPException) as exc:
        await tasks.parse_submissions_endpoint(
            task_id="never-created",
            background_tasks=background,
            file=upload,
            identity_mode="filename",
            roster_file=None,
            recognition_provider_id=None,
            replace_confirmed=False,
            current=SimpleNamespace(id="owner"),
            registry=_Registry(),
        )

    assert exc.value.status_code == 413
    assert exc.value.detail == {"code": "submission_source_too_large"}
    assert background.calls == []


@pytest.mark.asyncio
async def test_question_preparation_timeout_persists_provider_timeout(monkeypatch):
    owner_id, task_id = _seed_task()
    job, _ = workflow_repository.create_operation(
        assignment_id=task_id,
        owner_id=owner_id,
        operation_type="question_preparation",
        input_hash=uuid.uuid4().hex,
        expires_at=time.time() + 60,
    )
    claimed_revision = task_facade.claim_workflow_operation_atomic(
        task_id=task_id,
        owner_id=owner_id,
        operation_id=job.id,
        expected_operation_attempt=job.attempt,
        expected_workflow_revision=0,
        workflow_changes={
            "presentation_status": "extracting_problems",
            "active_operation": "question_preparation",
            "active_job_id": job.id,
            "extract_job_id": job.id,
        },
    )

    async def _timeout(*args, **kwargs):
        del args, kwargs
        try:
            raise TimeoutError("provider request timed out")
        except TimeoutError as exc:
            raise TransientLLMError("provider request failed") from exc

    monkeypatch.setattr(task_preparation, "prepare_question_packages", _timeout)

    await task_preparation._run_question_preparation(
        task_id=task_id,
        owner_id=owner_id,
        job_id=job.id,
        job_attempt=job.attempt,
        sources=[],
        provider=SimpleNamespace(provider_id="gemini:test"),
        recognition_provider_id="gemini:test",
        claimed_workflow_revision=claimed_revision,
        replace_confirmed=False,
        score_policy=SimpleNamespace(),
    )

    failed = workflow_repository.get_operation(job.id, owner_id=owner_id)
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert failed.status == "error"
    assert failed.error_code == "provider_timeout"
    assert failed.progress["phase"] == "error"
    assert failed.progress["error_detail"] == "provider_timeout"
    assert workflow.presentation_status == "error"
    assert workflow.active_job_id is None
    assert workflow.error_code == "provider_timeout"
    task_preparation.remove_reporter(job.id)


def test_disabled_selected_recognition_provider_has_figma_error_code():
    owner_id, task_id = _seed_task(with_question=True)

    class DisabledRegistry(_Registry):
        def list_configs(self):
            return [{"provider_id": "disabled", "enabled": False}]

        def get(self, provider_id):
            return self.provider

    with pytest.raises(ValidationError) as disabled:
        task_facade.queue_task_submission_parsing(
            task_id=task_id, owner_id=owner_id, filename="answers.zip",
            content=b"archive", content_type="application/zip",
            registry=DisabledRegistry(), recognition_provider_id="disabled",
        )
    assert disabled.value.code == "recognition_provider_not_enabled"


def test_question_and_submission_queues_freeze_resolved_default_provider_id():
    problem_owner, problem_task = _seed_task()
    problem_queued = task_facade.queue_task_problem_extraction(
        task_id=problem_task,
        owner_id=problem_owner,
        filename="questions.txt",
        content=b"Question 1",
        content_type="text/plain",
        registry=_Registry(),
    )
    problem_operation = workflow_repository.get_operation(
        problem_queued["job_id"], owner_id=problem_owner,
    )
    problem_workflow = workflow_repository.get_workflow(
        problem_task, owner_id=problem_owner,
    )
    assert problem_queued["_recognition_provider_id"] == "test-provider"
    assert problem_operation.payload["recognition_provider_id"] == "test-provider"
    assert problem_workflow.question_recognition_provider_id == "test-provider"

    submission_owner, submission_task = _seed_task(with_question=True)
    submission_queued = task_facade.queue_task_submission_parsing(
        task_id=submission_task,
        owner_id=submission_owner,
        filename="answers.txt",
        content=b"Answer 1",
        content_type="text/plain",
        registry=_Registry(),
    )
    submission_operation = workflow_repository.get_operation(
        submission_queued["job_id"], owner_id=submission_owner,
    )
    submission_workflow = workflow_repository.get_workflow(
        submission_task, owner_id=submission_owner,
    )
    assert submission_queued["_recognition_provider_id"] == "test-provider"
    assert submission_operation.payload["recognition_provider_id"] == "test-provider"
    assert submission_workflow.submission_recognition_provider_id == "test-provider"


@pytest.mark.asyncio
async def test_submission_ocr_without_vision_provider_has_figma_error_code(monkeypatch):
    from backend.db import source_outcome_repository
    from backend.services import submission_source_pipeline

    owner_id, task_id = _seed_task(with_question=True)

    class NoVisionRegistry(_Registry):
        def pick_vision(self, preferred=None):
            del preferred
            return None

    async def _requires_vision(*args, **kwargs):
        del args, kwargs
        raise HTTPException(
            status_code=503,
            detail=(
                "answers.pdf requires OCR, but no vision-capable provider is "
                "configured. Add and enable a model that supports image input."
            ),
        )

    monkeypatch.setattr(
        submission_source_pipeline,
        "extract_text_from_upload",
        _requires_vision,
    )
    registry = NoVisionRegistry()
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id, owner_id=owner_id, filename="answers.pdf",
        content=b"%PDF-1.4\n", content_type="application/pdf", registry=registry,
    )

    await task_facade.run_task_submission_parsing(
        task_id=task_id,
        owner_id=owner_id,
        job_id=queued["job_id"],
        filename="answers.pdf",
        content=b"%PDF-1.4\n",
        content_type="application/pdf",
        registry=registry,
        job_attempt=queued["_job_attempt"],
        identity_mode="filename",
        roster_entries=None,
        recognition_provider_id=queued["_recognition_provider_id"],
        replace_confirmed=False,
        claimed_workflow_revision=queued["workflow_revision"],
    )

    failed = workflow_repository.get_operation(queued["job_id"], owner_id=owner_id)
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert failed.status == "error"
    assert failed.error_code == "provider_vision_not_supported"
    assert workflow.presentation_status == "error"
    assert workflow.error_code == "provider_vision_not_supported"
    result = source_outcome_repository.list_source_results(
        operation_id=failed.id,
        owner_id=owner_id,
        attempt=failed.attempt,
    )[0]
    assert result.outcome is not None
    assert result.outcome.stable_error_code == "provider_vision_not_supported"
    assert result.outcome.failure_phase == "ocr"


class _VisionlessRegistry(_Registry):
    """_Registry plus a no-op ``pick_vision`` so background runners reach their work."""

    def pick_vision(self, preferred=None):
        del preferred
        return None


@pytest.mark.asyncio
async def test_problem_extraction_timeout_persists_provider_timeout(monkeypatch):
    owner_id, task_id = _seed_task()
    registry = _VisionlessRegistry()

    async def _timeout(*args, **kwargs):
        del args, kwargs
        try:
            raise TimeoutError("provider request timed out")
        except TimeoutError as exc:
            raise TransientLLMError("provider request failed") from exc

    monkeypatch.setattr(task_facade, "extract_text_from_upload", _timeout)
    queued = task_facade.queue_task_problem_extraction(
        task_id=task_id, owner_id=owner_id, filename="questions.pdf",
        content=b"source", content_type="application/pdf", registry=registry,
    )
    await task_facade.run_task_problem_extraction(
        task_id=task_id, owner_id=owner_id, job_id=queued["job_id"],
        filename="questions.pdf", content=b"source", registry=registry,
        job_attempt=queued["_job_attempt"],
        claimed_workflow_revision=queued["workflow_revision"],
        replace_confirmed=False,
        recognition_provider_id=queued["_recognition_provider_id"],
    )

    failed = workflow_repository.get_operation(queued["job_id"], owner_id=owner_id)
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert failed.status == "error"
    assert failed.error_code == "provider_timeout"
    assert workflow.presentation_status == "error"
    assert workflow.error_code == "provider_timeout"


@pytest.mark.asyncio
async def test_submission_rate_limit_persists_provider_rate_limited(monkeypatch):
    from backend.db import source_outcome_repository
    from backend.services import submission_source_pipeline

    owner_id, task_id = _seed_task(with_question=True)

    async def _rate_limited(*args, **kwargs):
        del args, kwargs
        raise RateLimitError("429 Too Many Requests")

    monkeypatch.setattr(
        submission_source_pipeline,
        "extract_text_from_upload",
        _rate_limited,
    )
    registry = _VisionlessRegistry()
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id, owner_id=owner_id, filename="answers.txt",
        content=b"answer", content_type="text/plain", registry=registry,
    )
    await task_facade.run_task_submission_parsing(
        task_id=task_id, owner_id=owner_id, job_id=queued["job_id"],
        filename="answers.txt", content=b"answer", content_type="text/plain",
        registry=registry,
        job_attempt=queued["_job_attempt"], identity_mode="filename",
        roster_entries=None,
        recognition_provider_id=queued["_recognition_provider_id"],
        replace_confirmed=False,
        claimed_workflow_revision=queued["workflow_revision"],
    )

    failed = workflow_repository.get_operation(queued["job_id"], owner_id=owner_id)
    assert failed.status == "error"
    assert failed.error_code == "provider_rate_limited"
    result = source_outcome_repository.list_source_results(
        operation_id=failed.id,
        owner_id=owner_id,
        attempt=failed.attempt,
    )[0]
    assert result.outcome is not None
    assert result.outcome.stable_error_code == "provider_rate_limited"
    assert result.outcome.failure_phase == "recognition"


@pytest.mark.asyncio
async def test_submission_connection_error_persists_provider_unreachable(monkeypatch):
    from backend.db import source_outcome_repository
    from backend.services import submission_source_pipeline

    owner_id, task_id = _seed_task(with_question=True)

    async def _unreachable(*args, **kwargs):
        del args, kwargs
        raise ConnectionError("failed to connect to provider")

    monkeypatch.setattr(
        submission_source_pipeline,
        "extract_text_from_upload",
        _unreachable,
    )
    registry = _VisionlessRegistry()
    queued = task_facade.queue_task_submission_parsing(
        task_id=task_id, owner_id=owner_id, filename="answers.txt",
        content=b"answer", content_type="text/plain", registry=registry,
    )
    await task_facade.run_task_submission_parsing(
        task_id=task_id, owner_id=owner_id, job_id=queued["job_id"],
        filename="answers.txt", content=b"answer", content_type="text/plain",
        registry=registry,
        job_attempt=queued["_job_attempt"], identity_mode="filename",
        roster_entries=None,
        recognition_provider_id=queued["_recognition_provider_id"],
        replace_confirmed=False,
        claimed_workflow_revision=queued["workflow_revision"],
    )

    failed = workflow_repository.get_operation(queued["job_id"], owner_id=owner_id)
    assert failed.status == "error"
    assert failed.error_code == "provider_unreachable"
    result = source_outcome_repository.list_source_results(
        operation_id=failed.id,
        owner_id=owner_id,
        attempt=failed.attempt,
    )[0]
    assert result.outcome is not None
    assert result.outcome.stable_error_code == "provider_unreachable"
    assert result.outcome.failure_phase == "recognition"


@pytest.mark.asyncio
async def test_problem_extraction_auth_error_persists_provider_auth_failed(monkeypatch):
    owner_id, task_id = _seed_task()
    registry = _VisionlessRegistry()

    async def _auth_failed(*args, **kwargs):
        del args, kwargs
        raise PermanentLLMError("401 invalid api key")

    monkeypatch.setattr(task_facade, "extract_text_from_upload", _auth_failed)
    queued = task_facade.queue_task_problem_extraction(
        task_id=task_id, owner_id=owner_id, filename="questions.pdf",
        content=b"source", content_type="application/pdf", registry=registry,
    )
    await task_facade.run_task_problem_extraction(
        task_id=task_id, owner_id=owner_id, job_id=queued["job_id"],
        filename="questions.pdf", content=b"source", registry=registry,
        job_attempt=queued["_job_attempt"],
        claimed_workflow_revision=queued["workflow_revision"],
        replace_confirmed=False,
        recognition_provider_id=queued["_recognition_provider_id"],
    )

    failed = workflow_repository.get_operation(queued["job_id"], owner_id=owner_id)
    assert failed.status == "error"
    assert failed.error_code == "provider_auth_failed"


@pytest.mark.asyncio
async def test_unknown_ai_completion_target_uses_figma_error_code():
    owner_id, task_id = _seed_task(with_question=True)
    response = await task_preparation.confirm_ai_completion(
        task_id=task_id,
        request=task_preparation.ConfirmAICompletionRequest(
            target_ids=["q1:unknown"], expected_workflow_revision=0,
        ),
        background_tasks=_BackgroundTasks(),
        current=SimpleNamespace(id=owner_id), registry=_Registry(),
    )
    body = json.loads(response.body)
    assert response.status_code == 422
    assert body["error"]["code"] == "unknown_ai_completion_target"


@pytest.mark.asyncio
async def test_ai_completion_retry_replays_running_job_after_revision_claim():
    owner_id, task_id = _seed_task(with_question=True)
    request = task_preparation.ConfirmAICompletionRequest(
        target_ids=["q1:criterion"], expected_workflow_revision=0,
        test_case_count=7,
    )
    first_background = _BackgroundTasks()
    first = await task_preparation.confirm_ai_completion(
        task_id=task_id, request=request,
        background_tasks=first_background,
        current=SimpleNamespace(id=owner_id), registry=_Registry(),
    )
    retry_background = _BackgroundTasks()
    retry = await task_preparation.confirm_ai_completion(
        task_id=task_id, request=request,
        background_tasks=retry_background,
        current=SimpleNamespace(id=owner_id), registry=_Registry(),
    )

    assert first["status"] == "started"
    assert retry["status"] == "already_running"
    assert retry["job_id"] == first["job_id"]
    assert first_background.calls == []
    assert retry_background.calls == []


@pytest.mark.asyncio
async def test_ai_completion_error_retry_reclaims_with_current_internal_revision():
    owner_id, task_id = _seed_task(with_question=True)
    request = task_preparation.ConfirmAICompletionRequest(
        target_ids=["q1:criterion"], expected_workflow_revision=0,
        test_case_count=7,
    )
    first_background = _BackgroundTasks()
    first = await task_preparation.confirm_ai_completion(
        task_id=task_id, request=request,
        background_tasks=first_background,
        current=SimpleNamespace(id=owner_id), registry=_Registry(),
    )
    first_operation = workflow_repository.get_operation(
        first["job_id"], owner_id=owner_id
    )
    assert task_facade._fail_operation(
        task_id, owner_id, first_operation.id, first_operation.attempt,
        "ai_completion_failed",
    ) is True

    retry_background = _BackgroundTasks()
    retried = await task_preparation.confirm_ai_completion(
        task_id=task_id, request=request,
        background_tasks=retry_background,
        current=SimpleNamespace(id=owner_id), registry=_Registry(),
    )
    retry_operation = workflow_repository.get_operation(
        retried["job_id"], owner_id=owner_id
    )
    assert retried["status"] == "started"
    assert retried["job_id"] == first["job_id"]
    assert retried["workflow_revision"] == 2
    assert retry_operation.attempt == first_operation.attempt + 1
    assert retry_operation.payload["base_workflow_revision"] == 1
    assert retry_background.calls == []


@pytest.mark.parametrize(
    ("operation_type", "failure_code"),
    [
        ("material_import", "material_import_failed"),
        ("ai_completion", "ai_completion_failed"),
    ],
)
def test_auxiliary_failure_keeps_questions_usable_and_retry_success_clears_error(
    operation_type: str, failure_code: str,
):
    owner_id, task_id = _seed_task(with_question=True)
    input_hash = uuid.uuid4().hex
    job, created = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type=operation_type, input_hash=input_hash,
        expires_at=time.time() + 60,
    )
    assert created is True
    claimed_revision = task_facade.claim_workflow_operation_atomic(
        task_id=task_id, owner_id=owner_id, operation_id=job.id,
        expected_operation_attempt=job.attempt,
        expected_workflow_revision=0,
        workflow_changes={
            "active_operation": operation_type, "active_job_id": job.id,
            "presentation_status": "error", "error_code": "old_error",
        },
    )
    claimed = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert claimed_revision == 1
    assert claimed.presentation_status == "problems_ready"
    assert claimed.error_code is None

    task_facade._fail_operation(
        task_id, owner_id, job.id, job.attempt, failure_code
    )
    failed = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    failed_task = task_facade.get_task(
        task_id=task_id, owner_id=owner_id, full=False
    )
    assert failed.presentation_status == "problems_ready"
    assert failed.active_job_id is None
    assert failed.error_code == failure_code
    assert failed_task["status"] == "problems_ready"
    assert failed_task["needs_attention"] is True

    retried, retry_created = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type=operation_type, input_hash=input_hash,
        expires_at=time.time() + 60,
    )
    assert retry_created is True
    retry_revision = task_facade.claim_workflow_operation_atomic(
        task_id=task_id, owner_id=owner_id, operation_id=retried.id,
        expected_operation_attempt=retried.attempt,
        expected_workflow_revision=1,
        workflow_changes={
            "active_operation": operation_type, "active_job_id": retried.id,
            "presentation_status": "error", "error_code": failure_code,
        },
    )
    retry_workflow = workflow_repository.get_workflow(
        task_id, owner_id=owner_id
    )
    assert retry_revision == 2
    assert retry_workflow.presentation_status == "problems_ready"
    assert retry_workflow.error_code is None
    assert task_facade.get_task(
        task_id=task_id, owner_id=owner_id, full=False
    )["needs_attention"] is False
    assert task_facade._fail_operation(
        task_id, owner_id, job.id, job.attempt, failure_code
    ) is False
    after_stale_failure = workflow_repository.get_workflow(
        task_id, owner_id=owner_id
    )
    assert after_stale_failure.active_job_id == retried.id
    assert after_stale_failure.error_code is None

    if operation_type == "material_import":
        with pytest.raises(VersionConflict) as stale_completion:
            task_facade.complete_planning_operation_atomic(
                task_id=task_id, owner_id=owner_id,
                expected_workflow_revision=retry_revision,
                operation_id=job.id,
                expected_operation_attempt=job.attempt,
                payload={"worker": "old"}, progress={"worker": "old"},
            )
        assert stale_completion.value.code == "stale_operation_attempt"
        task_facade.complete_planning_operation_atomic(
            task_id=task_id, owner_id=owner_id,
            expected_workflow_revision=retry_revision,
            operation_id=retried.id,
            expected_operation_attempt=retried.attempt,
            payload={}, progress={},
        )
        final_revision = task_facade.apply_question_patches_atomic(
            task_id=task_id, owner_id=owner_id,
            expected_workflow_revision=retry_revision, patches=[],
            operation_id=retried.id,
            expected_operation_attempt=retried.attempt,
            required_operation_status="ready",
            final_operation_status="applied", operation_payload={},
        )
    else:
        with pytest.raises(VersionConflict) as stale_completion:
            task_facade.apply_question_patches_atomic(
                task_id=task_id, owner_id=owner_id,
                expected_workflow_revision=retry_revision, patches=[],
                operation_id=job.id,
                expected_operation_attempt=job.attempt,
                required_operation_status="running",
                final_operation_status="done",
                operation_payload={"worker": "old"},
                operation_progress={"worker": "old"},
                require_missing=True,
            )
        assert stale_completion.value.code == "stale_operation_attempt"
        final_revision = task_facade.apply_question_patches_atomic(
            task_id=task_id, owner_id=owner_id,
            expected_workflow_revision=retry_revision, patches=[],
            operation_id=retried.id,
            expected_operation_attempt=retried.attempt,
            required_operation_status="running",
            final_operation_status="done", operation_payload={},
            require_missing=True,
        )
    succeeded = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert final_revision == 3
    assert succeeded.presentation_status == "problems_ready"
    assert succeeded.error_code is None


def test_material_apply_failure_after_plan_ready_keeps_attention_without_blocking_task():
    owner_id, task_id = _seed_task(with_question=True)
    job, _ = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type="material_import", input_hash=uuid.uuid4().hex,
        expires_at=time.time() + 60,
    )
    revision = task_facade.claim_workflow_operation_atomic(
        task_id=task_id, owner_id=owner_id, operation_id=job.id,
        expected_operation_attempt=job.attempt,
        expected_workflow_revision=0,
        workflow_changes={
            "active_operation": "material_import", "active_job_id": job.id,
        },
    )
    task_facade.complete_planning_operation_atomic(
        task_id=task_id, owner_id=owner_id,
        expected_workflow_revision=revision,
        operation_id=job.id, expected_operation_attempt=job.attempt,
        payload={}, progress={},
    )
    assert workflow_repository.get_workflow(
        task_id, owner_id=owner_id
    ).active_job_id is None

    task_facade._fail_operation(
        task_id, owner_id, job.id, job.attempt, "stale_revision"
    )
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    task = task_facade.get_task(task_id=task_id, owner_id=owner_id, full=False)
    assert workflow.presentation_status == "problems_ready"
    assert workflow.error_code == "stale_revision"
    assert task["status"] == "problems_ready"
    assert task["needs_attention"] is True


def test_primary_problem_failure_still_sets_blocking_error_status():
    owner_id, task_id = _seed_task(with_question=True)
    job, _ = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type="problem_extraction", input_hash=uuid.uuid4().hex,
        expires_at=time.time() + 60,
    )
    task_facade.claim_workflow_operation_atomic(
        task_id=task_id, owner_id=owner_id, operation_id=job.id,
        expected_operation_attempt=job.attempt,
        expected_workflow_revision=0,
        workflow_changes={
            "active_operation": "problem_extraction", "active_job_id": job.id,
            "presentation_status": "extracting_problems",
        },
    )

    task_facade._fail_operation(
        task_id, owner_id, job.id, job.attempt, "problem_extraction_failed"
    )
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert workflow.presentation_status == "error"
    assert workflow.active_job_id is None
    assert workflow.error_code == "problem_extraction_failed"
