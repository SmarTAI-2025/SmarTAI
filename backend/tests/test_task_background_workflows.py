from __future__ import annotations

import io
import json
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from backend.api import task_preparation, tasks
from backend.db import assignment_repository, workflow_repository
from backend.db.models import AssignmentRecord, CourseRecord, UserRecord
from backend.db.session import session_scope
from backend.domain.errors import InvalidTransition, ValidationError, VersionConflict
from backend.services import task_facade


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
    provider = SimpleNamespace(provider_id="test-provider")

    def pick_default(self):
        return self.provider

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
async def test_extract_endpoint_queues_background_work_and_returns_started():
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
    assert len(background.calls) == 1
    assert background.calls[0][0] is task_facade.run_task_problem_extraction
    operation = workflow_repository.get_operation(
        response["job_id"], owner_id=owner_id
    )
    assert operation.status == "running"
    assert assignment_repository.list_questions(task_id, teacher_id=owner_id) == []

    worker, args, kwargs = background.calls[0]
    await worker(*args, **kwargs)
    failed = workflow_repository.get_operation(
        response["job_id"], owner_id=owner_id
    )
    assert failed.status == "error"
    assert failed.error_code == "problem_extraction_failed"
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert workflow.active_job_id is None
    assert workflow.error_code == "problem_extraction_failed"


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
    assert len(first_background.calls) == 1
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
    assert len(retry_background.calls) == 1


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
