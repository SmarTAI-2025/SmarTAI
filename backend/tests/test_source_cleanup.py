from __future__ import annotations

import asyncio
import hashlib
import io
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from backend.config import settings
from backend.db import (
    assignment_repository,
    file_repository,
    grading_repository,
    source_storage_repository,
    submission_repository,
    workflow_repository,
)
from backend.db.models import (
    AssignmentKnowledgeDocumentRecord,
    AssignmentQuestionRecord,
    AssignmentRecord,
    CourseEnrollmentRecord,
    GradeResultRecord,
    GradingRunRecord,
    KnowledgeDocumentRecord,
    SourceStorageReservationRecord,
    StoredFileRecord,
    SubmissionAnswerRecord,
    SubmissionRecord,
    SubmissionRevisionRecord,
    TeacherReviewRecord,
    UserRecord,
)
from backend.db.session import session_scope
from backend.domain.errors import (
    InvalidTransition,
    LeaseLost,
    NotFound,
    SourceStorageReservationConflict,
)
from backend.domain import education
from backend.domain.source_storage import (
    SOURCE_CLEANUP_OPERATION,
    SOURCE_REPLACEMENT_CLEANUP_OPERATION,
    SOURCE_RESERVATION_CLEANUP_OPERATION,
    TASK_DELETE_OPERATION,
)
from backend.services import grading_runs, source_cleanup, task_deletion, task_facade
from backend.services.workflow_worker import WorkflowWorker
from backend.progress.tracker import get_or_create_reporter, get_reporter
from backend.state import remove_user
from backend.storage.base import StorageBackend, StorageObjectNotFound
from backend.storage.local import LocalStorage
from backend.storage.object import S3Storage
from backend.tests.test_task_finalization_contract import _prepared_task


async def _drain(worker: WorkflowWorker) -> None:
    for _ in range(500):
        if worker.in_flight_count == 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("source cleanup worker did not drain")


def _cleanup_operation(*, owner_id: str, assignment_id: str):
    with session_scope() as session:
        return session.scalar(select(workflow_repository.WorkflowOperationRecord).where(
            workflow_repository.WorkflowOperationRecord.owner_id == owner_id,
            workflow_repository.WorkflowOperationRecord.assignment_id == assignment_id,
            workflow_repository.WorkflowOperationRecord.operation_type
            == SOURCE_CLEANUP_OPERATION,
        ))


def _worker() -> WorkflowWorker:
    return WorkflowWorker(
        handlers={SOURCE_CLEANUP_OPERATION: source_cleanup.run_source_cleanup},
        worker_id="source-cleanup-test-worker",
        lease_seconds=60,
        heartbeat_seconds=60,
        poll_seconds=60,
        claim_batch_size=10,
        max_in_flight=2,
        shutdown_seconds=1,
    )


def _reservation_worker() -> WorkflowWorker:
    return WorkflowWorker(
        handlers={
            SOURCE_RESERVATION_CLEANUP_OPERATION: (
                source_cleanup.run_source_reservation_cleanup
            ),
        },
        worker_id="source-reservation-test-worker",
        lease_seconds=60,
        heartbeat_seconds=60,
        poll_seconds=60,
        claim_batch_size=10,
        max_in_flight=1,
        shutdown_seconds=1,
    )


def _replacement_worker() -> WorkflowWorker:
    return WorkflowWorker(
        handlers={
            SOURCE_REPLACEMENT_CLEANUP_OPERATION: (
                source_cleanup.run_source_replacement_cleanup
            ),
        },
        worker_id="source-replacement-test-worker",
        lease_seconds=60,
        heartbeat_seconds=60,
        poll_seconds=60,
        claim_batch_size=10,
        max_in_flight=1,
        shutdown_seconds=1,
    )


def _task_delete_worker() -> WorkflowWorker:
    return WorkflowWorker(
        handlers={TASK_DELETE_OPERATION: task_deletion.run_task_deletion},
        worker_id="task-delete-test-worker",
        lease_seconds=60,
        heartbeat_seconds=60,
        poll_seconds=60,
        claim_batch_size=10,
        max_in_flight=1,
        shutdown_seconds=1,
    )


async def _finish_task_delete(worker: WorkflowWorker, task_id: str) -> None:
    """Advance delayed verification passes without wall-clock sleeps."""
    for _ in range(12):
        with session_scope() as session:
            assignment = session.get(AssignmentRecord, task_id)
            if assignment is None:
                return
            session.execute(
                update(workflow_repository.WorkflowOperationRecord)
                .where(
                    workflow_repository.WorkflowOperationRecord.assignment_id
                    == task_id,
                    workflow_repository.WorkflowOperationRecord.operation_type
                    == TASK_DELETE_OPERATION,
                    workflow_repository.WorkflowOperationRecord.status == "pending",
                )
                .values(expires_at=0.0)
            )
        await worker.poll_once()
        await _drain(worker)
    raise AssertionError("task deletion did not finish")


@pytest.mark.asyncio
async def test_grading_completion_enqueues_cleanup_before_confirmation_or_export(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "graded-sources")
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    questions = assignment_repository.get_questions_by_assignment(
        assignment_id=seeded["task_id"]
    )
    submissions = submission_repository.list_submissions(
        assignment_id=seeded["task_id"], actor_id=seeded["owner_id"]
    )
    assert len(submissions) == 1
    submission = submissions[0]
    assert submission.current_revision_id is not None
    frozen_revision_id = submission.current_revision_id
    original = file_repository.save_file(
        storage=storage,
        owner_id=seeded["student_id"],
        kind="submission",
        original_name="late-student.png",
        content=b"\x89PNG\r\n\x1a\nstudent-original",
        content_type="image/png",
        submission_revision_id=frozen_revision_id,
    )

    workflow = workflow_repository.get_workflow(
        seeded["task_id"], owner_id=seeded["owner_id"]
    )
    frozen_source_file_ids = source_storage_repository.grading_source_file_ids(
        assignment_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        problem_operation_id=workflow.extract_job_id,
        submission_operation_id=workflow.parse_job_id,
        frozen_revision_ids=(frozen_revision_id,),
    )
    assert frozen_source_file_ids == (original.id,)
    run = grading_runs.start_run(
        teacher_id=seeded["owner_id"],
        assignment_id=seeded["task_id"],
        grading_setup={},
        setup_fingerprint="completion-source-scope",
        input_manifest={
            "questions": [
                question.model_dump(mode="json") for question in questions
            ],
            "submission_revision_ids": [frozen_revision_id],
            "source_file_ids": list(frozen_source_file_ids),
            "knowledge_document_ids": [],
            "provider_configuration_fingerprint": "test",
            "student_presentations": [],
            "answer_review_statuses": {},
        },
        workflow_expected_revision=workflow.workflow_revision,
    )
    late_revision = submission_repository.add_revision(
        submission_id=submission.id,
        student_id=seeded["student_id"],
        source=education.SubmissionRevisionSource.ONLINE.value,
        file_name="newer-student.png",
        answers=[{
            "question_id": question.id,
            "q_id": question.q_id,
            "type": question.type,
            "content": "newer answer",
        } for question in questions],
    )
    late_content = b"\x89PNG\r\n\x1a\nnewer-student-original"
    late_file_id = "completion-inflight-new-revision"
    late_storage_key = (
        f"submissions/{late_revision.id}/inflight/{late_file_id}.png"
    )
    late_replacement_group = (
        source_storage_repository.replacement_claim_group_id(
            assignment_id=seeded["task_id"],
            family="submission",
            replacement_file_ids=(original.id,),
        )
    )
    late_reservation = source_storage_repository.reserve_source_upload(
        file_id=late_file_id,
        file_owner_id=seeded["student_id"],
        kind="submission",
        original_name="newer-student.png",
        storage_backend=storage.name,
        storage_key=late_storage_key,
        content_type="image/png",
        requested_bytes=len(late_content),
        sha256=hashlib.sha256(late_content).hexdigest(),
        assignment_id=None,
        submission_revision_id=late_revision.id,
        replacement_file_ids=(original.id,),
        replacement_group_id=late_replacement_group,
    )
    storage.save(late_storage_key, late_content)
    source_storage_repository.mark_reservation_object_written(
        late_reservation.id
    )
    epoch_before_completion = workflow_repository.get_workflow(
        seeded["task_id"], owner_id=seeded["owner_id"]
    ).source_lifecycle_epoch
    grading_repository.claim_lease(
        run_id=run.id,
        worker_id="visual-analysis-completion-worker",
        lease_seconds=60,
    )
    completed = grading_repository.mark_completed(
        run.id,
        worker_id="visual-analysis-completion-worker",
        completed=0,
        failed=1,
    )
    assert completed.status == "partial_failed"
    assert completed.released_at is None
    assert workflow_repository.get_workflow(
        seeded["task_id"], owner_id=seeded["owner_id"]
    ).source_lifecycle_epoch == epoch_before_completion
    late = source_storage_repository.publish_source_reservation(
        reservation_id=late_reservation.id,
        assignment_id=None,
        submission_revision_id=late_revision.id,
        knowledge_document_id=None,
    )
    assert workflow_repository.list_artifact_manifests(
        seeded["task_id"], owner_id=seeded["owner_id"]
    ) == []

    pending = file_repository.get_file(
        file_id=original.id, owner_id=seeded["student_id"]
    )
    assert pending is not None
    assert pending.availability_status == "cleanup_pending"
    still_available = file_repository.get_file(
        file_id=late.id, owner_id=seeded["student_id"]
    )
    assert still_available is not None
    assert still_available.availability_status == "available"
    cleanup = _cleanup_operation(
        owner_id=seeded["owner_id"], assignment_id=seeded["task_id"]
    )
    assert cleanup is not None
    assert cleanup.payload["grading_run_id"] == run.id
    assert cleanup.payload["finalized_at"] == completed.completed_at
    assert source_storage_repository.cleanup_generation_is_authorized(
        assignment_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        grading_run_id=cleanup.payload["grading_run_id"],
        final_result_version=cleanup.payload["final_result_version"],
        finalized_at=cleanup.payload["finalized_at"],
    )

    worker = _worker()
    assert await worker.poll_once() == 1
    await _drain(worker)
    deleted = file_repository.get_file(
        file_id=original.id, owner_id=seeded["student_id"]
    )
    assert deleted is not None
    assert deleted.availability_status == "unavailable"
    assert deleted.availability_reason == "task_finalized"
    assert not storage.exists(original.storage_key)
    assert storage.exists(late.storage_key)
    assert source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    ).used_bytes == late.size_bytes


@pytest.mark.asyncio
async def test_exact_formal_confirmation_does_not_fence_inflight_new_source(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "formal-confirmation-inflight")
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    old = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="graded-problem.pdf",
        content=b"graded-problem-original",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    questions = assignment_repository.get_questions_by_assignment(
        assignment_id=seeded["task_id"]
    )
    frozen_revisions = grading_repository.list_frozen_submissions(
        seeded["run_id"]
    )
    # Retrofitting the immutable manifest models a current (non-legacy) run;
    # _prepared_task intentionally creates its run through the compatibility
    # helper so it has no setup row by default.
    workflow_repository.save_run_setup(
        grading_run_id=seeded["run_id"],
        assignment_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        setup={},
        fingerprint="formal-exact-source-manifest",
        input_manifest={
            "questions": [
                question.model_dump(mode="json") for question in questions
            ],
            "submission_revision_ids": [
                revision.id for revision in frozen_revisions
            ],
            "source_file_ids": [old.id],
        },
    )

    new_content = b"new-problem-reserved-before-confirmation"
    new_file_id = "formal-inflight-new-source"
    new_storage_key = (
        f"assignments/{seeded['task_id']}/inflight/{new_file_id}.pdf"
    )
    replacement_group = source_storage_repository.replacement_claim_group_id(
        assignment_id=seeded["task_id"],
        family="problem",
        replacement_file_ids=(old.id,),
    )
    reservation = source_storage_repository.reserve_source_upload(
        file_id=new_file_id,
        file_owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="new-problem.pdf",
        storage_backend=storage.name,
        storage_key=new_storage_key,
        content_type="application/pdf",
        requested_bytes=len(new_content),
        sha256=hashlib.sha256(new_content).hexdigest(),
        assignment_id=seeded["task_id"],
        submission_revision_id=None,
        replacement_file_ids=(old.id,),
        replacement_group_id=replacement_group,
    )
    storage.save(new_storage_key, new_content)
    source_storage_repository.mark_reservation_object_written(reservation.id)
    epoch_before_confirmation = workflow_repository.get_workflow(
        seeded["task_id"], owner_id=seeded["owner_id"]
    ).source_lifecycle_epoch

    response = task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    assert response["status"] == "ok"
    assert workflow_repository.get_workflow(
        seeded["task_id"], owner_id=seeded["owner_id"]
    ).source_lifecycle_epoch == epoch_before_confirmation

    new = source_storage_repository.publish_source_reservation(
        reservation_id=reservation.id,
        assignment_id=seeded["task_id"],
        submission_revision_id=None,
        knowledge_document_id=None,
    )
    assert new.availability_status == "available"
    pending = file_repository.get_file(
        file_id=old.id, owner_id=seeded["owner_id"]
    )
    assert pending is not None
    assert pending.availability_status == "cleanup_pending"

    worker = _worker()
    assert await worker.poll_once() == 1
    await _drain(worker)
    assert not storage.exists(old.storage_key)
    assert storage.exists(new.storage_key)
    assert file_repository.get_file(
        file_id=new.id, owner_id=seeded["owner_id"]
    ).availability_status == "available"


@pytest.mark.asyncio
async def test_formal_result_enqueues_cleanup_without_waiting_for_report_export(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "finalized-sources")
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    problem = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="problem.pdf",
        content=b"problem-original",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    submission = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="submission_source",
        original_name="student.pdf",
        content=b"student-original",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    derived = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="ocr_artifact",
        original_name="recognized.json",
        content=b'{"questions": []}',
        content_type="application/json",
        assignment_id=seeded["task_id"],
    )
    knowledge = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="personal_knowledge",
        original_name="textbook.pdf",
        content=b"long-lived-knowledge",
        content_type="application/pdf",
    )

    response = task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )

    assert response["status"] == "ok"
    assert response["source_cleanup"]["status"] == "pending"
    assert response["source_cleanup"]["pending_count"] == 2
    assert response["source_cleanup"]["total_count"] == 2
    # Optional CSV/Markdown/TeX/ZIP generation has not run and is not a gate.
    assert workflow_repository.list_artifact_manifests(
        seeded["task_id"], owner_id=seeded["owner_id"]
    ) == []
    assert file_repository.get_file(
        file_id=problem.id, owner_id=seeded["owner_id"]
    ).availability_status == "cleanup_pending"
    assert file_repository.get_file(
        file_id=submission.id, owner_id=seeded["owner_id"]
    ).availability_status == "cleanup_pending"
    assert storage.exists(problem.storage_key)
    assert storage.exists(submission.storage_key)
    queued = _cleanup_operation(
        owner_id=seeded["owner_id"], assignment_id=seeded["task_id"]
    )
    assert queued is not None
    assert {entry["file_id"] for entry in queued.payload["files"]} == {
        problem.id,
        submission.id,
    }
    assert source_storage_repository.cleanup_generation_is_authorized(
        assignment_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        grading_run_id=queued.payload["grading_run_id"],
        final_result_version=queued.payload["final_result_version"],
        finalized_at=queued.payload["finalized_at"],
    )

    worker = _worker()
    assert await worker.poll_once() == 1
    await _drain(worker)

    for original in (problem, submission):
        tombstone = file_repository.get_file(
            file_id=original.id, owner_id=seeded["owner_id"]
        )
        assert tombstone is not None
        assert tombstone.availability_status == "unavailable"
        assert tombstone.availability_reason == "task_finalized"
        assert not storage.exists(original.storage_key)
    assert source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    ).used_bytes == 0
    assert source_storage_repository.cleanup_summary(
        assignment_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )["status"] == "completed"

    # Derived artifacts, knowledge files, structured grading rows, and the
    # formal result remain usable after physical originals are gone.
    for retained in (derived, knowledge):
        current = file_repository.get_file(
            file_id=retained.id, owner_id=seeded["owner_id"]
        )
        assert current is not None
        assert current.availability_status == "available"
        assert storage.exists(current.storage_key)
    assert len(grading_repository.list_results_for_run(seeded["run_id"])) == 2
    snapshot = task_facade.result_snapshot(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        result_version=1,
    )
    assert snapshot["payload"]["results"]


class _MemoryS3Client:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, *, Bucket, Key, Body):
        assert Bucket == "private-test-bucket"
        self.objects[Key] = bytes(Body)
        return {}

    def get_object(self, *, Bucket, Key):
        from botocore.exceptions import ClientError

        assert Bucket == "private-test-bucket"
        if Key not in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey", "Message": "missing"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "GetObject",
            )
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, *, Bucket, Key, VersionId=None):
        assert Bucket == "private-test-bucket"
        assert VersionId == "null"
        self.objects.pop(Key, None)
        return {}

    def list_object_versions(
        self, *, Bucket, Prefix, KeyMarker=None, VersionIdMarker=None
    ):
        assert Bucket == "private-test-bucket"
        assert KeyMarker is None
        assert VersionIdMarker is None
        return {
            "IsTruncated": False,
            "Versions": [
                {"Key": key, "VersionId": "null"}
                for key in sorted(self.objects)
                if key.startswith(Prefix)
            ],
        }

    def head_object(self, *, Bucket, Key):
        from botocore.exceptions import ClientError

        assert Bucket == "private-test-bucket"
        if Key not in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey", "Message": "missing"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            )
        return {}


@pytest.mark.asyncio
async def test_s3_compatible_backend_matches_finalization_cleanup_contract(
    monkeypatch,
):
    seeded = _prepared_task()
    client = _MemoryS3Client()
    storage = S3Storage.__new__(S3Storage)
    storage.bucket = "private-test-bucket"
    storage.client = client
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    original = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="submission_source",
        original_name="student.pdf",
        content=b"s3-compatible-original",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    assert storage.exists(original.storage_key)

    task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    worker = _worker()
    assert await worker.poll_once() == 1
    await _drain(worker)

    tombstone = file_repository.get_file(
        file_id=original.id, owner_id=seeded["owner_id"]
    )
    assert tombstone is not None
    assert tombstone.availability_status == "unavailable"
    assert tombstone.availability_reason == "task_finalized"
    assert not storage.exists(original.storage_key)
    assert source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    ).used_bytes == 0


@pytest.mark.asyncio
async def test_task_delete_purges_task_progress_reporters_after_tombstone():
    seeded = _prepared_task()
    reporter = get_or_create_reporter(
        seeded["run_id"], total_students=1, total_questions=1
    )
    async with reporter.step(
        "student-private-id", "q-private-id", skill="grading"
    ) as unit:
        await reporter.substep(unit, "private-progress")
    assert get_reporter(seeded["run_id"]) is reporter

    response = task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )

    assert response["status"] == "deletion_pending"
    assert get_reporter(seeded["run_id"]) is None


@pytest.mark.asyncio
async def test_task_delete_hides_then_removes_an_available_original(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "guarded-task-delete")
    original = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="problem.pdf",
        content=b"must-remain-tracked",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )

    monkeypatch.setattr(task_deletion, "get_storage", lambda: storage)
    response = task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )
    assert response["status"] == "deletion_pending"
    assert storage.exists(original.storage_key)
    assert file_repository.get_file(
        file_id=original.id, owner_id=seeded["owner_id"]
    ) is not None
    assert source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    ).used_bytes == len(b"must-remain-tracked")
    with pytest.raises(NotFound):
        task_facade.get_task(
            task_id=seeded["task_id"], owner_id=seeded["owner_id"], full=False
        )
    await _finish_task_delete(_task_delete_worker(), seeded["task_id"])
    assert not storage.exists(original.storage_key)
    assert source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    ).used_bytes == 0


@pytest.mark.asyncio
async def test_unfinished_draft_task_delete_removes_structured_data_and_original(
    tmp_path, monkeypatch,
):
    """Deletion is not gated on a completed run or generated report."""
    owner_id = "unfinished-draft-delete-owner"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id,
            username=owner_id,
            password_hash="hash",
            role="teacher",
            is_active=True,
        ))
    task = task_facade.create_task(
        owner_id=owner_id,
        name="Unfinished draft",
        semester_id=None,
        course_id=None,
        idempotency_key="unfinished-draft-delete",
    )
    task_id = task["task_id"]
    question_id = "unfinished-draft-question"
    with session_scope() as session:
        session.add(AssignmentQuestionRecord(
            id=question_id,
            assignment_id=task_id,
            q_id="Q1",
            order_index=0,
            number="1",
            type="calculation",
            stem="Unfinished structured question",
            criterion="Not yet graded",
            max_score=10,
            version=1,
        ))

    storage = LocalStorage(tmp_path / "unfinished-draft-delete")
    original = file_repository.save_file(
        storage=storage,
        owner_id=owner_id,
        kind="problem_source",
        original_name="draft-problems.pdf",
        content=b"unfinished draft original",
        content_type="application/pdf",
        assignment_id=task_id,
    )
    with session_scope() as session:
        assert session.scalar(select(GradeResultRecord.id).limit(1)) is None

    monkeypatch.setattr(task_deletion, "get_storage", lambda: storage)
    response = task_facade.delete_task(task_id=task_id, owner_id=owner_id)
    assert response["status"] == "deletion_pending"
    with pytest.raises(NotFound):
        task_facade.get_task(task_id=task_id, owner_id=owner_id, full=False)
    assert storage.exists(original.storage_key)

    await _finish_task_delete(_task_delete_worker(), task_id)
    assert not storage.exists(original.storage_key)
    with session_scope() as session:
        assert session.get(AssignmentRecord, task_id) is None
        assert session.get(AssignmentQuestionRecord, question_id) is None
        assert session.get(StoredFileRecord, original.id) is None


def test_task_delete_accepts_zero_byte_revision_source_and_waits_for_reservation(
    tmp_path,
):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "revision-and-reservation-gate")
    with session_scope() as session:
        revision_id = session.scalar(
            select(SubmissionRevisionRecord.id)
            .join(
                SubmissionRecord,
                SubmissionRecord.id == SubmissionRevisionRecord.submission_id,
            )
            .where(SubmissionRecord.assignment_id == seeded["task_id"])
        )
    assert revision_id
    revision_source = file_repository.save_file(
        storage=storage,
        owner_id=seeded["student_id"],
        kind="submission",
        original_name="empty.pdf",
        content=b"",
        content_type="application/pdf",
        submission_revision_id=revision_id,
    )
    reservation = source_storage_repository.reserve_source_upload(
        file_id="delete-gate-reservation",
        file_owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="pending.pdf",
        storage_backend=storage.name,
        storage_key=(
            f"assignments/{seeded['task_id']}/delete-gate/pending.pdf"
        ),
        content_type="application/pdf",
        requested_bytes=1,
        sha256=hashlib.sha256(b"x").hexdigest(),
        assignment_id=seeded["task_id"],
        submission_revision_id=None,
    )

    response = task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )
    assert response["status"] == "deletion_pending"
    assert storage.exists(revision_source.storage_key)
    assert file_repository.get_file(
        file_id=revision_source.id, owner_id=seeded["student_id"]
    ) is not None
    with session_scope() as session:
        assert session.get(SourceStorageReservationRecord, reservation.id) is not None
    usage = source_storage_repository.source_quota_usage(seeded["owner_id"])
    assert usage.used_bytes == 1
    assert usage.available_source_bytes == 0
    assert usage.reserved_bytes == 1
    with pytest.raises(NotFound):
        task_facade.get_task(
            task_id=seeded["task_id"], owner_id=seeded["owner_id"], full=False
        )


@pytest.mark.asyncio
async def test_task_delete_removes_available_derived_artifact(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "derived-delete-gate")
    artifact = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="ocr_artifact",
        original_name="recognized.json",
        content=b"{}",
        content_type="application/json",
        assignment_id=seeded["task_id"],
    )

    monkeypatch.setattr(task_deletion, "get_storage", lambda: storage)
    task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )
    await _finish_task_delete(_task_delete_worker(), seeded["task_id"])
    assert not storage.exists(artifact.storage_key)
    assert file_repository.get_file(
        file_id=artifact.id, owner_id=seeded["owner_id"]
    ) is None


@pytest.mark.asyncio
async def test_task_delete_removes_only_knowledge_link_and_retains_library_object(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "retained-knowledge")
    content = b"long-lived-knowledge"
    from backend.db.knowledge_repository import (
        replace_document_chunks,
        set_task_documents,
    )
    from backend.services.knowledge_storage import persist_knowledge_upload

    upload = persist_knowledge_upload(
        storage=storage,
        owner_id=seeded["owner_id"],
        original_name="reference.pdf",
        content=content,
        content_type="application/pdf",
        retention_policy="retained",
    )
    document_id = upload.document_id
    replace_document_chunks(document_id, ["long-lived knowledge"])
    set_task_documents(
        assignment_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        document_ids=[document_id],
    )
    knowledge = file_repository.get_file(
        file_id=upload.file_id, owner_id=seeded["owner_id"]
    )
    assert knowledge is not None

    monkeypatch.setattr(task_deletion, "get_storage", lambda: storage)
    task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )
    await _finish_task_delete(_task_delete_worker(), seeded["task_id"])

    with session_scope() as session:
        assert session.get(KnowledgeDocumentRecord, document_id) is not None
        assert session.scalar(select(AssignmentKnowledgeDocumentRecord).where(
            AssignmentKnowledgeDocumentRecord.assignment_id == seeded["task_id"]
        )) is None
    retained = file_repository.get_file(
        file_id=knowledge.id, owner_id=seeded["owner_id"]
    )
    assert retained is not None
    assert retained.availability_status == "available"
    assert retained.source_quota_owner_id is None
    assert storage.exists(knowledge.storage_key)


@pytest.mark.asyncio
async def test_task_delete_enqueues_last_reference_task_only_knowledge_cleanup(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "task-only-knowledge")
    content = b"temporary-task-knowledge"
    from backend.db.knowledge_repository import (
        replace_document_chunks,
        set_task_documents,
    )
    from backend.db.knowledge_storage_repository import get_document_storage
    from backend.services.knowledge_storage import (
        KnowledgeStorageWorker,
        persist_knowledge_upload,
    )

    upload = persist_knowledge_upload(
        storage=storage,
        owner_id=seeded["owner_id"],
        original_name="temporary.txt",
        content=content,
        content_type="text/plain",
        retention_policy="task_only",
        origin_assignment_id=seeded["task_id"],
    )
    replace_document_chunks(upload.document_id, ["temporary task knowledge"])
    set_task_documents(
        assignment_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        document_ids=[upload.document_id],
    )
    assert storage.exists(upload.entry.storage_key)

    monkeypatch.setattr(task_deletion, "get_storage", lambda: storage)
    task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )
    await _finish_task_delete(_task_delete_worker(), seeded["task_id"])

    pending = get_document_storage(upload.document_id, seeded["owner_id"])
    assert pending is not None
    assert pending.state == "cleanup_pending"
    assert pending.cleanup_reason == "task_deleted"
    assert storage.exists(upload.entry.storage_key)

    assert await KnowledgeStorageWorker(storage=storage).run_once() == 1
    assert get_document_storage(upload.document_id, seeded["owner_id"]) is None
    assert not storage.exists(upload.entry.storage_key)


def test_legacy_user_removal_deactivates_without_cascading_files(tmp_path):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "soft-user-removal")
    raw = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="problem.pdf",
        content=b"retained-raw-tracking",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    knowledge = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="personal_knowledge",
        original_name="notes.txt",
        content=b"retained-knowledge-tracking",
        content_type="text/plain",
    )

    assert remove_user(seeded["owner_id"]) is True

    with session_scope() as session:
        user = session.get(UserRecord, seeded["owner_id"])
        assert user is not None
        assert user.is_active is False
        assert user.auth_invalid_before > 0
    for stored in (raw, knowledge):
        assert file_repository.get_file(
            file_id=stored.id, owner_id=seeded["owner_id"]
        ) is not None
        assert storage.exists(stored.storage_key)


@pytest.mark.asyncio
async def test_task_delete_succeeds_after_original_is_tombstoned(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "safe-task-delete")
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    monkeypatch.setattr(task_deletion, "get_storage", lambda: storage)
    original = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="problem.pdf",
        content=b"deleted-before-task-cascade",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    worker = _worker()
    assert await worker.poll_once() == 1
    await _drain(worker)
    assert not storage.exists(original.storage_key)

    task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )
    await _finish_task_delete(_task_delete_worker(), seeded["task_id"])

    with pytest.raises(NotFound):
        task_facade.get_task(
            task_id=seeded["task_id"], owner_id=seeded["owner_id"], full=False
        )
    assert file_repository.get_file(
        file_id=original.id, owner_id=seeded["owner_id"]
    ) is None
    assert source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    ).used_bytes == 0


def test_task_delete_accepts_while_running_cleanup_drains(tmp_path):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "running-cleanup-delete-gate")
    original = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="problem.pdf",
        content=b"cleanup-not-terminal",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    queued = _cleanup_operation(
        owner_id=seeded["owner_id"], assignment_id=seeded["task_id"]
    )
    running = workflow_repository.claim_operation(
        queued.id,
        owner_id=seeded["owner_id"],
        worker_id="delete-gate-cleanup-worker",
        lease_seconds=60,
    )
    entry = running.payload["files"][0]
    claim = source_storage_repository.claim_finalized_source_delete(
        operation_id=running.id,
        owner_id=running.owner_id,
        assignment_id=running.assignment_id,
        operation_attempt=running.attempt,
        worker_id="delete-gate-cleanup-worker",
        lease_token=running.lease_token,
        grading_run_id=running.payload["grading_run_id"],
        final_result_version=running.payload["final_result_version"],
        finalized_at=running.payload["finalized_at"],
        entry=entry,
    )
    assert claim.status == "ready"
    storage.delete(claim.storage_key)
    finished = source_storage_repository.finish_finalized_source_delete(
        operation_id=running.id,
        owner_id=running.owner_id,
        assignment_id=running.assignment_id,
        operation_attempt=running.attempt,
        worker_id="delete-gate-cleanup-worker",
        lease_token=running.lease_token,
        grading_run_id=running.payload["grading_run_id"],
        final_result_version=running.payload["final_result_version"],
        finalized_at=running.payload["finalized_at"],
        entry=entry,
        claim_token=claim.claim_token,
        deleted=True,
    )
    assert finished.status == "deleted"
    assert file_repository.get_file(
        file_id=original.id, owner_id=seeded["owner_id"]
    ).availability_status == "unavailable"

    response = task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )
    assert response["status"] == "deletion_pending"
    with pytest.raises(NotFound):
        task_facade.get_task(
            task_id=seeded["task_id"], owner_id=seeded["owner_id"], full=False
        )
    with session_scope() as session:
        assignment = session.get(AssignmentRecord, seeded["task_id"])
        assert assignment is not None
        assert assignment.deletion_requested_at is not None


@pytest.mark.asyncio
async def test_task_delete_cancels_nonterminal_workflow_operation(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()
    operation, created = workflow_repository.create_operation(
        assignment_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        operation_type="submission_recognition",
        input_hash=hashlib.sha256(b"active-delete-gate").hexdigest(),
    )
    assert created is True
    assert operation.status == "pending"

    storage = LocalStorage(tmp_path / "active-operation-delete")
    monkeypatch.setattr(task_deletion, "get_storage", lambda: storage)
    response = task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )
    assert response["status"] == "deletion_pending"
    assert workflow_repository.get_operation(
        operation.id, owner_id=seeded["owner_id"]
    ).status == "pending"
    await _finish_task_delete(_task_delete_worker(), seeded["task_id"])
    with pytest.raises(NotFound):
        workflow_repository.get_operation(
            operation.id, owner_id=seeded["owner_id"]
        )


def test_task_delete_tombstone_fences_producer_and_grading_heartbeats():
    seeded = _prepared_task()
    operation, _ = workflow_repository.create_operation(
        assignment_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        operation_type="submission_recognition",
        input_hash=hashlib.sha256(b"heartbeat-delete-fence").hexdigest(),
    )
    running = workflow_repository.claim_operation(
        operation.id,
        owner_id=seeded["owner_id"],
        worker_id="producer-before-delete",
        lease_seconds=60,
    )
    # The prepared grading run is terminal; create a fresh queued run solely to
    # exercise the grading lease gate.
    grading = grading_repository.create_run(
        seeded["task_id"], teacher_id=seeded["owner_id"], total_submissions=0
    )
    grading_repository.claim_lease(
        grading.id, worker_id="grading-before-delete", lease_seconds=60
    )

    task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )
    with pytest.raises(LeaseLost):
        workflow_repository.heartbeat_operation(
            running.id,
            owner_id=seeded["owner_id"],
            worker_id="producer-before-delete",
            lease_token=running.lease_token,
            lease_seconds=60,
        )
    with pytest.raises(LeaseLost):
        grading_repository.heartbeat(
            grading.id, worker_id="grading-before-delete", lease_seconds=60
        )


def test_task_delete_tombstone_hides_and_closes_student_submission_access():
    seeded = _prepared_task()
    from backend.db import submission_repository
    from backend.domain import education

    with session_scope() as session:
        submission_id = session.scalar(select(SubmissionRecord.id).where(
            SubmissionRecord.assignment_id == seeded["task_id"]
        ))
    assert submission_id
    task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )

    with pytest.raises(NotFound):
        submission_repository.get_submission(
            submission_id, actor_id=seeded["student_id"]
        )
    with pytest.raises(NotFound):
        submission_repository.list_submissions(
            seeded["task_id"], actor_id=seeded["student_id"]
        )
    with pytest.raises(NotFound):
        submission_repository.add_revision(
            submission_id,
            student_id=seeded["student_id"],
            source=education.SubmissionRevisionSource.ONLINE.value,
            answers=[],
        )
    with pytest.raises(NotFound):
        submission_repository.create_submission(
            seeded["task_id"], student_id=seeded["student_id"]
        )


def test_task_delete_tombstone_hides_workflow_results_and_mutations():
    seeded = _prepared_task()
    workflow = workflow_repository.get_live_workflow(
        seeded["task_id"], owner_id=seeded["owner_id"]
    )
    task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )

    with pytest.raises(NotFound):
        workflow_repository.get_live_workflow(
            seeded["task_id"], owner_id=seeded["owner_id"]
        )
    with pytest.raises(NotFound):
        workflow_repository.update_workflow(
            seeded["task_id"],
            owner_id=seeded["owner_id"],
            expected_revision=workflow.workflow_revision,
            presentation_status="draft",
        )
    with pytest.raises(NotFound):
        task_facade.task_results(
            task_id=seeded["task_id"], owner_id=seeded["owner_id"]
        )
    with pytest.raises(NotFound):
        task_facade.finalization(
            task_id=seeded["task_id"], owner_id=seeded["owner_id"]
        )
    with pytest.raises(NotFound):
        task_facade.artifact_index(
            task_id=seeded["task_id"], owner_id=seeded["owner_id"]
        )


@pytest.mark.asyncio
async def test_preparing_artifact_fence_leaves_late_delete_failure_recoverable(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()

    class FailingDeleteStorage(LocalStorage):
        fail_deletes = True
        after_save = None

        def save(self, key: str, content: bytes) -> None:
            super().save(key, content)
            callback = self.after_save
            self.after_save = None
            if callback is not None:
                callback()

        def delete(self, key: str) -> None:
            if self.fail_deletes:
                raise OSError("temporary delete failure")
            super().delete(key)

    storage = FailingDeleteStorage(tmp_path / "preparing-fence")
    draft, _ = workflow_repository.create_operation(
        assignment_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        operation_type="material_source",
        input_hash=hashlib.sha256(b"material-preparing-fence").hexdigest(),
        payload={"state": "preparing_artifact"},
        expires_at=time.time() + 60,
        initial_status="preparing",
    )
    tracked = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="material_import_text",
        original_name="tracked.txt",
        content=b"tracked before delete",
        content_type="text/plain",
        assignment_id=seeded["task_id"],
        fence_operation_id=draft.id,
        fence_operation_attempt=draft.attempt,
    )
    assert storage.exists(tracked.storage_key)

    # The exact-key artifact intent is committed before this PUT starts. Make
    # DELETE win immediately after the bytes land, then make compensation fail.
    storage.after_save = lambda: task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )
    with pytest.raises(NotFound):
        file_repository.save_file(
            storage=storage,
            owner_id=seeded["owner_id"],
            kind="material_import_text",
            original_name="late.txt",
            content=b"late after delete",
            content_type="text/plain",
            assignment_id=seeded["task_id"],
            fence_operation_id=draft.id,
            fence_operation_attempt=draft.attempt,
    )
    assert len(storage.list_keys(f"assignments/{seeded['task_id']}/")) == 2
    with session_scope() as session:
        intent = session.scalar(
            select(SourceStorageReservationRecord).where(
                SourceStorageReservationRecord.assignment_id == seeded["task_id"],
                SourceStorageReservationRecord.purpose == "artifact_write",
            )
        )
        assert intent is not None
        assert intent.state == "cleanup_pending"
    workflow_repository.update_operation(
        draft.id,
        owner_id=seeded["owner_id"],
        expected_attempt=draft.attempt,
        status="error",
        error_code="task_deleted",
        completed_at=time.time(),
    )

    storage.fail_deletes = False
    with session_scope() as session:
        session.execute(
            update(workflow_repository.WorkflowOperationRecord)
            .where(
                workflow_repository.WorkflowOperationRecord.assignment_id
                == seeded["task_id"],
                workflow_repository.WorkflowOperationRecord.operation_type
                == SOURCE_RESERVATION_CLEANUP_OPERATION,
                workflow_repository.WorkflowOperationRecord.status == "pending",
            )
            .values(expires_at=0.0)
        )
    reservation_worker = _reservation_worker()
    await reservation_worker.poll_once()
    await _drain(reservation_worker)
    monkeypatch.setattr(task_deletion, "get_storage", lambda: storage)
    await _finish_task_delete(_task_delete_worker(), seeded["task_id"])
    assert storage.list_keys(f"assignments/{seeded['task_id']}/") == []


@pytest.mark.asyncio
async def test_task_delete_retries_storage_failure_without_manual_action(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()

    class FailingDeleteStorage(LocalStorage):
        fail_deletes = True

        def delete(self, key: str) -> None:
            if self.fail_deletes:
                raise OSError("temporary delete failure")
            super().delete(key)

    storage = FailingDeleteStorage(tmp_path / "task-delete-retry")
    monkeypatch.setattr(task_deletion, "get_storage", lambda: storage)
    original = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="problem.pdf",
        content=b"charged-until-delete-succeeds",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )
    worker = _task_delete_worker()
    assert await worker.poll_once() == 1
    await _drain(worker)

    usage = source_storage_repository.source_quota_usage(seeded["owner_id"])
    assert usage.used_bytes == len(b"charged-until-delete-succeeds")
    assert usage.retrying_cleanup_bytes == usage.used_bytes
    assert storage.exists(original.storage_key)
    with session_scope() as session:
        assert session.get(AssignmentRecord, seeded["task_id"]) is not None

    storage.fail_deletes = False
    await _finish_task_delete(worker, seeded["task_id"])
    assert not storage.exists(original.storage_key)
    assert source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    ).used_bytes == 0


@pytest.mark.asyncio
async def test_task_delete_prefix_reconciliation_removes_untracked_orphan(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "task-delete-prefix-reconciliation")
    monkeypatch.setattr(task_deletion, "get_storage", lambda: storage)
    orphan_key = f"assignments/{seeded['task_id']}/crash-gap/orphan.bin"
    storage.save(orphan_key, b"written-before-metadata")

    task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )
    await _finish_task_delete(_task_delete_worker(), seeded["task_id"])
    assert not storage.exists(orphan_key)


@pytest.mark.asyncio
async def test_task_delete_final_cascade_removes_all_structured_grading_data(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "task-delete-structured-cascade")
    monkeypatch.setattr(task_deletion, "get_storage", lambda: storage)
    review = grading_repository.add_teacher_review(
        seeded["required_result_id"],
        teacher_id=seeded["owner_id"],
        new_score=7,
        new_comment="confirmed",
        confirm=True,
    )
    with session_scope() as session:
        question_ids = tuple(session.scalars(
            select(AssignmentQuestionRecord.id).where(
                AssignmentQuestionRecord.assignment_id == seeded["task_id"]
            )
        ))
        submission_ids = tuple(session.scalars(
            select(SubmissionRecord.id).where(
                SubmissionRecord.assignment_id == seeded["task_id"]
            )
        ))
        revision_ids = tuple(session.scalars(
            select(SubmissionRevisionRecord.id).where(
                SubmissionRevisionRecord.submission_id.in_(submission_ids)
            )
        ))
        answer_ids = tuple(session.scalars(
            select(SubmissionAnswerRecord.id).where(
                SubmissionAnswerRecord.revision_id.in_(revision_ids)
            )
        ))

    task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )
    await _finish_task_delete(_task_delete_worker(), seeded["task_id"])

    with session_scope() as session:
        assert session.get(AssignmentRecord, seeded["task_id"]) is None
        assert all(session.get(AssignmentQuestionRecord, value) is None for value in question_ids)
        assert all(session.get(SubmissionRecord, value) is None for value in submission_ids)
        assert all(session.get(SubmissionRevisionRecord, value) is None for value in revision_ids)
        assert all(session.get(SubmissionAnswerRecord, value) is None for value in answer_ids)
        assert session.get(GradingRunRecord, seeded["run_id"]) is None
        assert session.get(GradeResultRecord, seeded["required_result_id"]) is None
        assert session.get(GradeResultRecord, seeded["optional_result_id"]) is None
        assert session.get(TeacherReviewRecord, review.id) is None


@pytest.mark.asyncio
async def test_task_delete_removes_only_unreferenced_imported_synthetic_users(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "task-delete-imported-users")
    monkeypatch.setattr(task_deletion, "get_storage", lambda: storage)
    removable_id = "imported_task_only"
    protected_id = "imported_cross_task"
    other_task_id = "asg-imported-user-protection"
    with session_scope() as session:
        target = session.get(AssignmentRecord, seeded["task_id"])
        assert target is not None
        course_id = target.course_id
        session.add_all([
            UserRecord(
                id=removable_id,
                username="imported-task-only",
                email=None,
                password_hash="!disabled-imported-account",
                role="student",
                is_active=False,
            ),
            UserRecord(
                id=protected_id,
                username="imported-cross-task",
                email=None,
                password_hash="!disabled-imported-account",
                role="student",
                is_active=False,
            ),
            AssignmentRecord(
                id=other_task_id,
                course_id=course_id,
                teacher_id=seeded["owner_id"],
                name="Other task",
                description="",
                status=education.AssignmentStatus.DRAFT.value,
                version=1,
            ),
        ])
        session.flush()
        session.add_all([
            CourseEnrollmentRecord(course_id=course_id, student_id=removable_id),
            CourseEnrollmentRecord(course_id=course_id, student_id=protected_id),
            SubmissionRecord(
                id="sub-imported-task-only",
                assignment_id=seeded["task_id"],
                student_id=removable_id,
                current_revision_id=None,
            ),
            SubmissionRecord(
                id="sub-imported-current-task",
                assignment_id=seeded["task_id"],
                student_id=protected_id,
                current_revision_id=None,
            ),
            SubmissionRecord(
                id="sub-imported-other-task",
                assignment_id=other_task_id,
                student_id=protected_id,
                current_revision_id=None,
            ),
        ])

    task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )
    await _finish_task_delete(_task_delete_worker(), seeded["task_id"])

    with session_scope() as session:
        assert session.get(UserRecord, removable_id) is None
        assert session.get(
            CourseEnrollmentRecord,
            {"course_id": course_id, "student_id": removable_id},
        ) is None
        assert session.get(UserRecord, protected_id) is not None
        assert session.get(
            CourseEnrollmentRecord,
            {"course_id": course_id, "student_id": protected_id},
        ) is not None
        assert session.get(SubmissionRecord, "sub-imported-other-task") is not None
        assert session.get(UserRecord, seeded["student_id"]) is not None
        assert session.get(
            CourseEnrollmentRecord,
            {"course_id": course_id, "student_id": seeded["student_id"]},
        ) is not None


def test_cleanup_claim_rechecks_lease_after_acquiring_operation_lock(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "cleanup-lock-expiry")
    original = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="problem.pdf",
        content=b"expires-while-waiting-for-operation-lock",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    queued = _cleanup_operation(
        owner_id=seeded["owner_id"], assignment_id=seeded["task_id"]
    )
    running = workflow_repository.claim_operation(
        queued.id,
        owner_id=seeded["owner_id"],
        worker_id="cleanup-lock-expiry-worker",
        lease_seconds=60,
    )
    observed_times = iter((0.0, 0.0, 10**20))
    monkeypatch.setattr(
        source_storage_repository,
        "time",
        SimpleNamespace(time=lambda: next(observed_times)),
    )

    with pytest.raises(LeaseLost):
        source_storage_repository.claim_finalized_source_delete(
            operation_id=running.id,
            owner_id=running.owner_id,
            assignment_id=running.assignment_id,
            operation_attempt=running.attempt,
            worker_id="cleanup-lock-expiry-worker",
            lease_token=running.lease_token,
            grading_run_id=running.payload["grading_run_id"],
            final_result_version=running.payload["final_result_version"],
            finalized_at=running.payload["finalized_at"],
            entry=running.payload["files"][0],
        )

    current = file_repository.get_file(
        file_id=original.id, owner_id=seeded["owner_id"]
    )
    assert current.availability_status == "cleanup_pending"
    assert current.cleanup_attempt_count == 0
    assert current.cleanup_claim_token is None
    assert storage.exists(original.storage_key)


class _FlakyDeleteStorage(StorageBackend):
    name = "flaky"

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.fail_delete = True

    def save(self, key: str, content: bytes) -> None:
        self.objects[key] = content

    def open(self, key: str):
        if key not in self.objects:
            raise StorageObjectNotFound("missing")
        return io.BytesIO(self.objects[key])

    def delete(self, key: str) -> None:
        if self.fail_delete:
            raise OSError("private provider detail")
        if key not in self.objects:
            raise StorageObjectNotFound("missing")
        del self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects


@pytest.mark.asyncio
async def test_replacement_delete_failure_stays_charged_then_retries_automatically(
    monkeypatch,
):
    seeded = _prepared_task()
    storage = _FlakyDeleteStorage()
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    monkeypatch.setattr(settings, "source_cleanup_retry_base_seconds", 1)
    monkeypatch.setattr(settings, "source_cleanup_retry_max_seconds", 1)
    old = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="old.pdf",
        content=b"old-original",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    new = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="new.pdf",
        content=b"new-original",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
        replacement_file_ids=(old.id,),
        replacement_group_id="replacement-cleanup-group",
    )
    with session_scope() as session:
        cleanup_id = (
            source_storage_repository.enqueue_replaced_source_cleanup_in_session(
                session,
                assignment_id=seeded["task_id"],
                owner_id=seeded["owner_id"],
                producer_operation_id="replacement-producer",
                producer_operation_attempt=1,
                source_file_ids=(old.id,),
                keep_file_ids=(new.id,),
            )
        )
    assert cleanup_id is not None

    first_worker = _replacement_worker()
    assert await first_worker.poll_once() == 1
    await _drain(first_worker)

    pending = file_repository.get_file(file_id=old.id, owner_id=seeded["owner_id"])
    assert pending.availability_status == "cleanup_pending"
    assert pending.availability_reason == "storage_delete_failed"
    assert source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    ).used_bytes == len(b"old-original") + len(b"new-original")
    with session_scope() as session:
        operation = session.get(
            workflow_repository.WorkflowOperationRecord, cleanup_id
        )
        assert operation.status == "pending"
        assert operation.error_code == "source_replacement_cleanup_failed"
        operation.expires_at = 0.0

    storage.fail_delete = False
    restarted_worker = _replacement_worker()
    assert await restarted_worker.poll_once() == 1
    await _drain(restarted_worker)

    tombstone = file_repository.get_file(file_id=old.id, owner_id=seeded["owner_id"])
    assert tombstone.availability_status == "unavailable"
    assert tombstone.availability_reason == "replaced"
    assert not storage.exists(old.storage_key)
    assert storage.exists(new.storage_key)
    assert source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    ).used_bytes == len(b"new-original")


@pytest.mark.asyncio
async def test_replacement_physical_delete_survives_lease_fence_and_review_race(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "replacement-lease-fence")
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    old = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="old.pdf",
        content=b"old-physically-deleted",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    new = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="new.pdf",
        content=b"new-current-source",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
        replacement_file_ids=(old.id,),
        replacement_group_id="replacement-lease-fence-group",
    )
    with session_scope() as session:
        cleanup_id = (
            source_storage_repository.enqueue_replaced_source_cleanup_in_session(
                session,
                assignment_id=seeded["task_id"],
                owner_id=seeded["owner_id"],
                producer_operation_id="replacement-lease-fence-producer",
                producer_operation_attempt=1,
                source_file_ids=(old.id,),
                keep_file_ids=(new.id,),
            )
        )
    running = workflow_repository.claim_operation(
        cleanup_id,
        owner_id=seeded["owner_id"],
        worker_id="replacement-crashed-worker",
        lease_seconds=60,
    )
    entry = running.payload["files"][0]
    claim = source_storage_repository.claim_replaced_source_delete(
        operation_id=running.id,
        owner_id=running.owner_id,
        assignment_id=running.assignment_id,
        operation_attempt=running.attempt,
        worker_id="replacement-crashed-worker",
        lease_token=running.lease_token,
        entry=entry,
    )
    storage.delete(claim.storage_key)
    with session_scope() as session:
        session.execute(
            update(workflow_repository.WorkflowOperationRecord)
            .where(workflow_repository.WorkflowOperationRecord.id == running.id)
            .values(lease_expires_at=time.time() - 1)
        )
    with pytest.raises(LeaseLost):
        source_storage_repository.finish_replaced_source_delete(
            operation_id=running.id,
            owner_id=running.owner_id,
            assignment_id=running.assignment_id,
            operation_attempt=running.attempt,
            worker_id="replacement-crashed-worker",
            lease_token=running.lease_token,
            entry=entry,
            claim_token=claim.claim_token,
            deleted=True,
        )

    task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    with session_scope() as session:
        session.execute(
            update(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id
                == seeded["task_id"]
            )
            .values(presentation_status="graded", analysis_status="stale")
        )
    finalization_worker = _worker()
    assert await finalization_worker.poll_once() == 1
    await _drain(finalization_worker)

    fenced = file_repository.get_file(file_id=old.id, owner_id=seeded["owner_id"])
    assert fenced.availability_status == "cleanup_pending"
    assert fenced.availability_reason == "replaced"
    assert fenced.cleanup_operation_id == cleanup_id
    assert not storage.exists(old.storage_key)

    replacement_worker = _replacement_worker()
    assert await replacement_worker.poll_once() == 1
    await _drain(replacement_worker)
    tombstone = file_repository.get_file(
        file_id=old.id, owner_id=seeded["owner_id"]
    )
    assert tombstone.availability_status == "unavailable"
    assert tombstone.availability_reason == "replaced"


def test_finalization_does_not_steal_replacement_cleanup_generation(
    monkeypatch,
):
    seeded = _prepared_task()
    storage = _FlakyDeleteStorage()
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    old = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="old.pdf",
        content=b"old-before-replacement",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    new = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="new.pdf",
        content=b"new-after-replacement",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
        replacement_file_ids=(old.id,),
        replacement_group_id="replacement-finalization-race",
    )
    task_delete_owned = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="submission_source",
        original_name="task-delete-owned.pdf",
        content=b"already-owned-by-task-delete",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    with session_scope() as session:
        replacement_cleanup_id = (
            source_storage_repository.enqueue_replaced_source_cleanup_in_session(
                session,
                assignment_id=seeded["task_id"],
                owner_id=seeded["owner_id"],
                producer_operation_id="replacement-finalization-producer",
                producer_operation_attempt=1,
                source_file_ids=(old.id,),
                keep_file_ids=(new.id,),
            )
        )
    task_delete_operation, created = workflow_repository.create_operation(
        assignment_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        operation_type=TASK_DELETE_OPERATION,
        input_hash=hashlib.sha256(b"finalization-task-delete-race").hexdigest(),
        payload={"schema": 1},
    )
    assert created is True
    with session_scope() as session:
        row = session.get(StoredFileRecord, task_delete_owned.id)
        row.availability_status = "cleanup_pending"
        row.availability_reason = "task_deleted"
        row.cleanup_operation_id = task_delete_operation.id
        row.cleanup_requested_at = time.time()
        row.lifecycle_revision += 1

    task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )

    old_pending = file_repository.get_file(
        file_id=old.id, owner_id=seeded["owner_id"]
    )
    new_pending = file_repository.get_file(
        file_id=new.id, owner_id=seeded["owner_id"]
    )
    task_delete_pending = file_repository.get_file(
        file_id=task_delete_owned.id, owner_id=seeded["owner_id"]
    )
    assert old_pending.availability_reason == "replaced"
    assert old_pending.cleanup_operation_id == replacement_cleanup_id
    assert new_pending.availability_reason == "task_finalized"
    assert new_pending.cleanup_operation_id != replacement_cleanup_id
    assert task_delete_pending.availability_reason == "task_deleted"
    assert task_delete_pending.cleanup_operation_id == task_delete_operation.id


@pytest.mark.asyncio
async def test_delete_failure_remains_charged_and_retries_without_user_action(
    monkeypatch,
):
    seeded = _prepared_task()
    storage = _FlakyDeleteStorage()
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    monkeypatch.setattr(settings, "source_cleanup_retry_base_seconds", 1)
    monkeypatch.setattr(settings, "source_cleanup_retry_max_seconds", 1)
    original = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="problem.pdf",
        content=b"charged-until-deleted",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )

    first_worker = _worker()
    assert await first_worker.poll_once() == 1
    await _drain(first_worker)

    pending = file_repository.get_file(
        file_id=original.id, owner_id=seeded["owner_id"]
    )
    assert pending is not None
    assert pending.availability_status == "cleanup_pending"
    assert pending.availability_reason == "storage_delete_failed"
    usage = source_storage_repository.source_quota_usage(seeded["owner_id"])
    assert usage.used_bytes == len(b"charged-until-deleted")
    assert usage.retrying_cleanup_bytes == len(b"charged-until-deleted")
    assert usage.retrying_cleanup_count == 1
    operation = _cleanup_operation(
        owner_id=seeded["owner_id"], assignment_id=seeded["task_id"]
    )
    assert operation is not None
    assert operation.status == "pending"
    assert operation.error_code == "source_cleanup_failed"
    assert operation.expires_at > time.time()

    # Simulate the durable scheduler reaching its not-before time after a
    # process restart. No user retry endpoint or action is involved.
    storage.fail_delete = False
    with session_scope() as session:
        session.execute(
            update(workflow_repository.WorkflowOperationRecord)
            .where(workflow_repository.WorkflowOperationRecord.id == operation.id)
            .values(expires_at=time.time() - 1)
        )
    restarted_worker = _worker()
    assert await restarted_worker.poll_once() == 1
    await _drain(restarted_worker)

    tombstone = file_repository.get_file(
        file_id=original.id, owner_id=seeded["owner_id"]
    )
    assert tombstone is not None
    assert tombstone.availability_status == "unavailable"
    assert tombstone.availability_reason == "task_finalized"
    assert not storage.exists(original.storage_key)
    assert source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    ).used_bytes == 0
    completed = workflow_repository.get_operation(
        operation.id, owner_id=seeded["owner_id"]
    )
    assert completed.status == "completed"
    assert completed.terminal_summary["failed_count"] == 0


@pytest.mark.asyncio
async def test_superseded_before_claim_restores_untouched_source_and_charge(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "superseded-before-claim")
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    content = b"still-present-after-review-reopens"
    original = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="problem.pdf",
        content=content,
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    queued = _cleanup_operation(
        owner_id=seeded["owner_id"], assignment_id=seeded["task_id"]
    )
    assert queued is not None

    # Reopening review means the task is no longer complete.  The worker must
    # not delete an unclaimed source from the stale finalization generation,
    # but it must also not strand the source in cleanup_pending forever.
    with session_scope() as session:
        session.execute(
            update(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id
                == seeded["task_id"]
            )
            .values(presentation_status="graded", analysis_status="stale")
        )

    worker = _worker()
    assert await worker.poll_once() == 1
    await _drain(worker)

    restored = file_repository.get_file(
        file_id=original.id, owner_id=seeded["owner_id"]
    )
    assert restored is not None
    assert restored.availability_status == "available"
    assert restored.availability_reason is None
    assert restored.cleanup_operation_id is None
    assert restored.cleanup_claim_token is None
    assert storage.exists(original.storage_key)
    usage = source_storage_repository.source_quota_usage(seeded["owner_id"])
    assert usage.used_bytes == len(content)
    assert usage.available_source_bytes == len(content)
    assert usage.cleanup_pending_bytes == 0
    superseded = workflow_repository.get_operation(
        queued.id, owner_id=seeded["owner_id"]
    )
    assert superseded.status == "superseded"
    assert superseded.terminal_summary["restored_count"] == 1


@pytest.mark.asyncio
async def test_stale_generation_reconciles_a_claim_left_by_an_expired_worker(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "stale-claimed-delete")
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    content = b"claimed-before-worker-crash"
    original = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="problem.pdf",
        content=content,
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    queued = _cleanup_operation(
        owner_id=seeded["owner_id"], assignment_id=seeded["task_id"]
    )
    crashed = workflow_repository.claim_operation(
        queued.id,
        owner_id=seeded["owner_id"],
        worker_id="crashed-cleanup-worker",
        lease_seconds=60,
    )
    entry = crashed.payload["files"][0]
    claim = source_storage_repository.claim_finalized_source_delete(
        operation_id=crashed.id,
        owner_id=crashed.owner_id,
        assignment_id=crashed.assignment_id,
        operation_attempt=crashed.attempt,
        worker_id="crashed-cleanup-worker",
        lease_token=crashed.lease_token,
        grading_run_id=crashed.payload["grading_run_id"],
        final_result_version=crashed.payload["final_result_version"],
        finalized_at=crashed.payload["finalized_at"],
        entry=entry,
    )
    assert claim.status == "ready"

    with session_scope() as session:
        session.execute(
            update(workflow_repository.WorkflowOperationRecord)
            .where(workflow_repository.WorkflowOperationRecord.id == crashed.id)
            .values(lease_expires_at=time.time() - 1)
        )
        session.execute(
            update(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id
                == seeded["task_id"]
            )
            .values(presentation_status="graded", analysis_status="stale")
        )

    replacement = _worker()
    assert await replacement.poll_once() == 1
    await _drain(replacement)

    tombstone = file_repository.get_file(
        file_id=original.id, owner_id=seeded["owner_id"]
    )
    assert tombstone is not None
    assert tombstone.availability_status == "unavailable"
    assert not storage.exists(original.storage_key)
    assert source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    ).used_bytes == 0


@pytest.mark.asyncio
async def test_stale_generation_keeps_retrying_an_ambiguous_attempt(
    monkeypatch,
):
    seeded = _prepared_task()
    storage = _FlakyDeleteStorage()
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    monkeypatch.setattr(settings, "source_cleanup_retry_base_seconds", 1)
    monkeypatch.setattr(settings, "source_cleanup_retry_max_seconds", 1)
    content = b"ambiguous-delete-must-be-reconciled"
    original = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="problem.pdf",
        content=content,
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )

    first = _worker()
    assert await first.poll_once() == 1
    await _drain(first)
    operation = _cleanup_operation(
        owner_id=seeded["owner_id"], assignment_id=seeded["task_id"]
    )
    assert operation.status == "pending"

    with session_scope() as session:
        session.execute(
            update(workflow_repository.WorkflowOperationRecord)
            .where(workflow_repository.WorkflowOperationRecord.id == operation.id)
            .values(expires_at=time.time() - 1)
        )
        session.execute(
            update(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id
                == seeded["task_id"]
            )
            .values(presentation_status="graded", analysis_status="stale")
        )
    storage.fail_delete = False

    replacement = _worker()
    assert await replacement.poll_once() == 1
    await _drain(replacement)

    tombstone = file_repository.get_file(
        file_id=original.id, owner_id=seeded["owner_id"]
    )
    assert tombstone is not None
    assert tombstone.availability_status == "unavailable"
    assert not storage.exists(original.storage_key)
    assert source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    ).used_bytes == 0


@pytest.mark.asyncio
async def test_stale_mixed_manifest_retries_attempted_and_restores_untouched(
    monkeypatch,
):
    seeded = _prepared_task()
    storage = _FlakyDeleteStorage()
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    monkeypatch.setattr(settings, "source_cleanup_retry_base_seconds", 1)
    monkeypatch.setattr(settings, "source_cleanup_retry_max_seconds", 1)
    first = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="problem.pdf",
        content=b"attempted-source",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    second = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="submission_source",
        original_name="student.pdf",
        content=b"untouched-source",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    queued = _cleanup_operation(
        owner_id=seeded["owner_id"], assignment_id=seeded["task_id"]
    )
    crashed = workflow_repository.claim_operation(
        queued.id,
        owner_id=seeded["owner_id"],
        worker_id="mixed-crashed-worker",
        lease_seconds=60,
    )
    entry_by_id = {
        entry["file_id"]: entry for entry in crashed.payload["files"]
    }
    attempted_entry = entry_by_id[first.id]
    claim = source_storage_repository.claim_finalized_source_delete(
        operation_id=crashed.id,
        owner_id=crashed.owner_id,
        assignment_id=crashed.assignment_id,
        operation_attempt=crashed.attempt,
        worker_id="mixed-crashed-worker",
        lease_token=crashed.lease_token,
        grading_run_id=crashed.payload["grading_run_id"],
        final_result_version=crashed.payload["final_result_version"],
        finalized_at=crashed.payload["finalized_at"],
        entry=attempted_entry,
    )
    assert claim.status == "ready"
    failed = source_storage_repository.finish_finalized_source_delete(
        operation_id=crashed.id,
        owner_id=crashed.owner_id,
        assignment_id=crashed.assignment_id,
        operation_attempt=crashed.attempt,
        worker_id="mixed-crashed-worker",
        lease_token=crashed.lease_token,
        grading_run_id=crashed.payload["grading_run_id"],
        final_result_version=crashed.payload["final_result_version"],
        finalized_at=crashed.payload["finalized_at"],
        entry=attempted_entry,
        claim_token=claim.claim_token,
        deleted=False,
    )
    assert failed.status == "failed"

    with session_scope() as session:
        session.execute(
            update(workflow_repository.WorkflowOperationRecord)
            .where(workflow_repository.WorkflowOperationRecord.id == crashed.id)
            .values(lease_expires_at=time.time() - 1)
        )
        session.execute(
            update(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id
                == seeded["task_id"]
            )
            .values(presentation_status="graded", analysis_status="stale")
        )

    still_failing = _worker()
    assert await still_failing.poll_once() == 1
    await _drain(still_failing)

    attempted = file_repository.get_file(
        file_id=first.id, owner_id=seeded["owner_id"]
    )
    untouched = file_repository.get_file(
        file_id=second.id, owner_id=seeded["owner_id"]
    )
    assert attempted.availability_status == "cleanup_pending"
    assert attempted.availability_reason == "storage_delete_failed"
    assert untouched.availability_status == "available"
    assert untouched.availability_reason is None
    operation = workflow_repository.get_operation(
        queued.id, owner_id=seeded["owner_id"]
    )
    assert operation.status == "pending"
    usage = source_storage_repository.source_quota_usage(seeded["owner_id"])
    assert usage.cleanup_pending_bytes == len(b"attempted-source")
    assert usage.available_source_bytes == len(b"untouched-source")

    storage.fail_delete = False
    with session_scope() as session:
        session.execute(
            update(workflow_repository.WorkflowOperationRecord)
            .where(workflow_repository.WorkflowOperationRecord.id == queued.id)
            .values(expires_at=time.time() - 1)
        )
    recovered = _worker()
    assert await recovered.poll_once() == 1
    await _drain(recovered)

    attempted = file_repository.get_file(
        file_id=first.id, owner_id=seeded["owner_id"]
    )
    untouched = file_repository.get_file(
        file_id=second.id, owner_id=seeded["owner_id"]
    )
    assert attempted.availability_status == "unavailable"
    assert untouched.availability_status == "available"
    assert not storage.exists(first.storage_key)
    assert storage.exists(second.storage_key)
    usage = source_storage_repository.source_quota_usage(seeded["owner_id"])
    assert usage.used_bytes == len(b"untouched-source")
    assert usage.cleanup_pending_bytes == 0
    settled = workflow_repository.get_operation(
        queued.id, owner_id=seeded["owner_id"]
    )
    assert settled.status == "superseded"


class _BlockingDeleteStorage(StorageBackend):
    name = "blocking-delete"

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.delete_started = threading.Event()
        self.allow_delete = threading.Event()

    def save(self, key: str, content: bytes) -> None:
        self.objects[key] = content

    def open(self, key: str):
        if key not in self.objects:
            raise StorageObjectNotFound("missing")
        return io.BytesIO(self.objects[key])

    def delete(self, key: str) -> None:
        self.delete_started.set()
        if not self.allow_delete.wait(timeout=5):
            raise TimeoutError("test did not release blocked storage deletion")
        if key not in self.objects:
            raise StorageObjectNotFound("missing")
        del self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects


@pytest.mark.asyncio
async def test_storage_delete_phase_does_not_hold_the_operation_db_lock(
    monkeypatch,
):
    seeded = _prepared_task()
    storage = _BlockingDeleteStorage()
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    original = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="problem.pdf",
        content=b"delete-outside-the-transaction",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )

    worker = _worker()
    assert await worker.poll_once() == 1
    assert await asyncio.to_thread(storage.delete_started.wait, 2)

    try:
        running = await asyncio.to_thread(
            _cleanup_operation,
            owner_id=seeded["owner_id"],
            assignment_id=seeded["task_id"],
        )
        assert running is not None
        assert running.status == "running"
        assert running.lease_token
        original_expiry = running.lease_expires_at

        # The repository claim transaction has committed before object storage
        # I/O starts, so a concurrent heartbeat can update the operation row.
        heartbeat = await asyncio.wait_for(
            asyncio.to_thread(
                workflow_repository.heartbeat_operation,
                running.id,
                owner_id=seeded["owner_id"],
                worker_id="source-cleanup-test-worker",
                lease_token=running.lease_token,
                lease_seconds=60,
            ),
            timeout=2,
        )
        assert heartbeat is True
        refreshed = await asyncio.to_thread(
            workflow_repository.get_operation,
            running.id,
            owner_id=seeded["owner_id"],
        )
        assert refreshed.lease_expires_at >= original_expiry
    finally:
        storage.allow_delete.set()
        await _drain(worker)

    tombstone = file_repository.get_file(
        file_id=original.id, owner_id=seeded["owner_id"]
    )
    assert tombstone is not None
    assert tombstone.availability_status == "unavailable"
    assert not storage.exists(original.storage_key)


def test_expired_worker_cannot_tombstone_after_physical_delete(tmp_path):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "stale-delete-fence")
    original = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="problem.pdf",
        content=b"physically-deleted-before-finish",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    queued = _cleanup_operation(
        owner_id=seeded["owner_id"], assignment_id=seeded["task_id"]
    )
    assert queued is not None
    first = workflow_repository.claim_operation(
        queued.id,
        owner_id=seeded["owner_id"],
        worker_id="stale-source-worker",
        lease_seconds=60,
    )
    assert first.lease_token
    entry = first.payload["files"][0]
    first_claim = source_storage_repository.claim_finalized_source_delete(
        operation_id=first.id,
        owner_id=first.owner_id,
        assignment_id=first.assignment_id,
        operation_attempt=first.attempt,
        worker_id="stale-source-worker",
        lease_token=first.lease_token,
        grading_run_id=first.payload["grading_run_id"],
        final_result_version=first.payload["final_result_version"],
        finalized_at=first.payload["finalized_at"],
        entry=entry,
    )
    assert first_claim.status == "ready"
    assert first_claim.claim_token
    storage.delete(first_claim.storage_key)
    with session_scope() as session:
        session.execute(
            update(workflow_repository.WorkflowOperationRecord)
            .where(workflow_repository.WorkflowOperationRecord.id == first.id)
            .values(lease_expires_at=time.time() - 1)
        )

    with pytest.raises(LeaseLost):
        source_storage_repository.finish_finalized_source_delete(
            operation_id=first.id,
            owner_id=first.owner_id,
            assignment_id=first.assignment_id,
            operation_attempt=first.attempt,
            worker_id="stale-source-worker",
            lease_token=first.lease_token,
            grading_run_id=first.payload["grading_run_id"],
            final_result_version=first.payload["final_result_version"],
            finalized_at=first.payload["finalized_at"],
            entry=entry,
            claim_token=first_claim.claim_token,
            deleted=True,
        )

    still_charged = file_repository.get_file(
        file_id=original.id, owner_id=seeded["owner_id"]
    )
    assert still_charged is not None
    assert still_charged.availability_status == "cleanup_pending"
    assert still_charged.cleanup_claim_token == first_claim.claim_token
    assert source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    ).used_bytes == len(b"physically-deleted-before-finish")

    replacement = workflow_repository.claim_operation(
        first.id,
        owner_id=seeded["owner_id"],
        worker_id="replacement-source-worker",
        lease_seconds=60,
    )
    assert replacement.lease_token
    replacement_claim = source_storage_repository.claim_finalized_source_delete(
        operation_id=replacement.id,
        owner_id=replacement.owner_id,
        assignment_id=replacement.assignment_id,
        operation_attempt=replacement.attempt,
        worker_id="replacement-source-worker",
        lease_token=replacement.lease_token,
        grading_run_id=replacement.payload["grading_run_id"],
        final_result_version=replacement.payload["final_result_version"],
        finalized_at=replacement.payload["finalized_at"],
        entry=entry,
    )
    assert replacement_claim.status == "ready"
    assert replacement_claim.claim_token != first_claim.claim_token
    storage.delete(replacement_claim.storage_key)
    finished = source_storage_repository.finish_finalized_source_delete(
        operation_id=replacement.id,
        owner_id=replacement.owner_id,
        assignment_id=replacement.assignment_id,
        operation_attempt=replacement.attempt,
        worker_id="replacement-source-worker",
        lease_token=replacement.lease_token,
        grading_run_id=replacement.payload["grading_run_id"],
        final_result_version=replacement.payload["final_result_version"],
        finalized_at=replacement.payload["finalized_at"],
        entry=entry,
        claim_token=replacement_claim.claim_token,
        deleted=True,
    )
    assert finished.status == "deleted"
    tombstone = file_repository.get_file(
        file_id=original.id, owner_id=seeded["owner_id"]
    )
    assert tombstone is not None
    assert tombstone.availability_status == "unavailable"
    assert source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    ).used_bytes == 0


def test_claimed_delete_records_physical_truth_if_workflow_changes(tmp_path):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "generation-change-after-claim")
    original = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="problem.pdf",
        content=b"delete-was-authorized-before-review-change",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    queued = _cleanup_operation(
        owner_id=seeded["owner_id"], assignment_id=seeded["task_id"]
    )
    assert queued is not None
    running = workflow_repository.claim_operation(
        queued.id,
        owner_id=seeded["owner_id"],
        worker_id="generation-race-worker",
        lease_seconds=60,
    )
    assert running.lease_token
    entry = running.payload["files"][0]
    claim = source_storage_repository.claim_finalized_source_delete(
        operation_id=running.id,
        owner_id=running.owner_id,
        assignment_id=running.assignment_id,
        operation_attempt=running.attempt,
        worker_id="generation-race-worker",
        lease_token=running.lease_token,
        grading_run_id=running.payload["grading_run_id"],
        final_result_version=running.payload["final_result_version"],
        finalized_at=running.payload["finalized_at"],
        entry=entry,
    )
    assert claim.status == "ready"
    assert claim.claim_token
    storage.delete(claim.storage_key)

    # A teacher edit can supersede the current workflow after storage accepted
    # the authorized delete. The exact claim must still settle the already-made
    # physical change instead of leaving a phantom charge forever.
    with session_scope() as session:
        session.execute(
            update(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id
                == seeded["task_id"]
            )
            .values(presentation_status="graded", analysis_status="stale")
        )
    assert not source_storage_repository.cleanup_generation_is_authorized(
        assignment_id=running.assignment_id,
        owner_id=running.owner_id,
        grading_run_id=running.payload["grading_run_id"],
        final_result_version=running.payload["final_result_version"],
        finalized_at=running.payload["finalized_at"],
    )

    finished = source_storage_repository.finish_finalized_source_delete(
        operation_id=running.id,
        owner_id=running.owner_id,
        assignment_id=running.assignment_id,
        operation_attempt=running.attempt,
        worker_id="generation-race-worker",
        lease_token=running.lease_token,
        grading_run_id=running.payload["grading_run_id"],
        final_result_version=running.payload["final_result_version"],
        finalized_at=running.payload["finalized_at"],
        entry=entry,
        claim_token=claim.claim_token,
        deleted=True,
    )

    assert finished.status == "deleted"
    tombstone = file_repository.get_file(
        file_id=original.id, owner_id=seeded["owner_id"]
    )
    assert tombstone is not None
    assert tombstone.availability_status == "unavailable"
    assert tombstone.cleanup_claim_token is None
    assert source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    ).used_bytes == 0


class _MissingOnDeleteStorage(StorageBackend):
    name = "missing-on-delete"

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def save(self, key: str, content: bytes) -> None:
        self.objects[key] = content

    def open(self, key: str):
        if key not in self.objects:
            raise StorageObjectNotFound("missing")
        return io.BytesIO(self.objects[key])

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)
        raise StorageObjectNotFound("already absent")

    def exists(self, key: str) -> bool:
        return key in self.objects


@pytest.mark.asyncio
async def test_storage_object_not_found_is_a_successful_idempotent_delete(
    monkeypatch,
):
    seeded = _prepared_task()
    storage = _MissingOnDeleteStorage()
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    original = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="submission_source",
        original_name="student.pdf",
        content=b"object-disappeared-before-cleanup",
        content_type="application/pdf",
        assignment_id=seeded["task_id"],
    )
    task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )

    worker = _worker()
    assert await worker.poll_once() == 1
    await _drain(worker)

    tombstone = file_repository.get_file(
        file_id=original.id, owner_id=seeded["owner_id"]
    )
    assert tombstone is not None
    assert tombstone.availability_status == "unavailable"
    assert tombstone.availability_reason == "task_finalized"
    assert source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    ).used_bytes == 0
    completed = _cleanup_operation(
        owner_id=seeded["owner_id"], assignment_id=seeded["task_id"]
    )
    assert completed is not None
    assert completed.status == "completed"


@pytest.mark.asyncio
async def test_finalization_epoch_fences_an_inflight_source_reservation(
    tmp_path, monkeypatch,
):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "inflight-reservation")
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    content = b"reserved-before-finalization"
    file_id = "inflight-source-reservation"
    storage_key = (
        f"assignments/{seeded['task_id']}/inflight/{file_id}/problem.pdf"
    )
    reservation = source_storage_repository.reserve_source_upload(
        file_id=file_id,
        file_owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="problem.pdf",
        storage_backend=storage.name,
        storage_key=storage_key,
        content_type="application/pdf",
        requested_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        assignment_id=seeded["task_id"],
        submission_revision_id=None,
    )
    assert reservation.source_lifecycle_epoch == 0
    storage.save(storage_key, content)

    task_facade.confirm_finalization(
        task_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        expected_revision=0,
    )
    finalized = workflow_repository.get_workflow(
        seeded["task_id"], owner_id=seeded["owner_id"]
    )
    assert finalized.source_lifecycle_epoch == reservation.source_lifecycle_epoch + 1

    source_storage_repository.mark_reservation_object_written(reservation.id)
    with pytest.raises(SourceStorageReservationConflict):
        source_storage_repository.publish_source_reservation(
            reservation_id=reservation.id,
            assignment_id=seeded["task_id"],
            submission_revision_id=None,
            knowledge_document_id=None,
        )

    assert file_repository.get_file(
        file_id=file_id, owner_id=seeded["owner_id"]
    ) is None
    with session_scope() as session:
        pending = session.get(SourceStorageReservationRecord, reservation.id)
        assert pending is not None
        assert pending.state == "cleanup_pending"
        assert pending.error_code == "source_storage_generation_superseded"
        assert pending.source_lifecycle_epoch == reservation.source_lifecycle_epoch
    usage = source_storage_repository.source_quota_usage(seeded["owner_id"])
    assert usage.reserved_bytes == len(content)
    assert storage.exists(storage_key)

    collector = _reservation_worker()
    assert await collector.poll_once() == 1
    await _drain(collector)

    with session_scope() as session:
        assert session.get(SourceStorageReservationRecord, reservation.id) is None
    assert not storage.exists(storage_key)
    assert source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    ).used_bytes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("reservation_purpose", ["upload", "orphan_cleanup"])
async def test_reservation_delete_retry_is_visible_in_source_quota_usage(
    tmp_path, monkeypatch, reservation_purpose,
):
    seeded = _prepared_task()

    class FailingDeleteStorage(LocalStorage):
        def delete(self, key: str) -> None:
            raise OSError("temporary delete failure")

    storage = FailingDeleteStorage(tmp_path / reservation_purpose)
    monkeypatch.setattr(source_cleanup, "get_storage", lambda: storage)
    content = f"pending-{reservation_purpose}".encode()
    file_id = f"pending-{reservation_purpose}-reservation"
    storage_key = (
        f"assignments/{seeded['task_id']}/pending/{file_id}/problem.pdf"
    )
    reservation = source_storage_repository.reserve_source_upload(
        file_id=file_id,
        file_owner_id=seeded["owner_id"],
        kind="problem_source",
        original_name="problem.pdf",
        storage_backend=storage.name,
        storage_key=storage_key,
        content_type="application/pdf",
        requested_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        assignment_id=seeded["task_id"],
        submission_revision_id=None,
    )
    storage.save(storage_key, content)
    if reservation_purpose == "orphan_cleanup":
        with session_scope() as session:
            row = session.get(SourceStorageReservationRecord, reservation.id)
            assert row is not None
            row.purpose = "orphan_cleanup"
    source_storage_repository.retain_source_reservation_for_cleanup(
        reservation.id,
        error_code="source_storage_write_failed",
    )
    with session_scope() as session:
        session.execute(
            update(workflow_repository.WorkflowOperationRecord)
            .where(
                workflow_repository.WorkflowOperationRecord.id
                == reservation.operation_id
            )
            .values(expires_at=time.time() - 1)
        )

    pending_usage = source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    )
    assert pending_usage.used_bytes == len(content)
    assert pending_usage.reserved_bytes == len(content)
    assert pending_usage.cleanup_pending_bytes == len(content)
    assert pending_usage.cleanup_pending_count == 1
    assert pending_usage.retrying_cleanup_bytes == 0
    assert pending_usage.retrying_cleanup_count == 0

    worker = _reservation_worker()
    assert await worker.poll_once() == 1
    await _drain(worker)

    retrying_usage = source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    )
    assert retrying_usage.used_bytes == len(content)
    assert retrying_usage.reserved_bytes == len(content)
    assert retrying_usage.cleanup_pending_bytes == len(content)
    assert retrying_usage.cleanup_pending_count == 1
    assert retrying_usage.retrying_cleanup_bytes == len(content)
    assert retrying_usage.retrying_cleanup_count == 1
    with session_scope() as session:
        row = session.get(SourceStorageReservationRecord, reservation.id)
        assert row is not None
        assert row.state == "cleanup_pending"
        assert row.error_code == "source_storage_delete_failed"
    assert storage.exists(storage_key)
