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
    file_repository,
    grading_repository,
    source_storage_repository,
    workflow_repository,
)
from backend.db.models import (
    AssignmentKnowledgeDocumentRecord,
    KnowledgeDocumentRecord,
    SourceStorageReservationRecord,
    StoredFileRecord,
    SubmissionRecord,
    SubmissionRevisionRecord,
    UserRecord,
)
from backend.db.session import session_scope
from backend.domain.errors import (
    InvalidTransition,
    LeaseLost,
    NotFound,
    SourceStorageReservationConflict,
)
from backend.domain.source_storage import (
    SOURCE_CLEANUP_OPERATION,
    SOURCE_RESERVATION_CLEANUP_OPERATION,
)
from backend.services import source_cleanup, task_facade
from backend.services.workflow_worker import WorkflowWorker
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

    def delete_object(self, *, Bucket, Key):
        assert Bucket == "private-test-bucket"
        self.objects.pop(Key, None)
        return {}

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


def test_task_delete_refuses_to_orphan_an_available_original(tmp_path):
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

    with pytest.raises(InvalidTransition) as error:
        task_facade.delete_task(
            task_id=seeded["task_id"], owner_id=seeded["owner_id"]
        )

    assert error.value.code == "task_storage_cleanup_required"
    assert storage.exists(original.storage_key)
    assert file_repository.get_file(
        file_id=original.id, owner_id=seeded["owner_id"]
    ) is not None
    assert source_storage_repository.source_quota_usage(
        seeded["owner_id"]
    ).used_bytes == len(b"must-remain-tracked")
    assert task_facade.get_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"], full=False
    )["task_id"] == seeded["task_id"]


def test_task_delete_blocks_zero_byte_revision_source_and_active_reservation(
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

    with pytest.raises(InvalidTransition) as error:
        task_facade.delete_task(
            task_id=seeded["task_id"], owner_id=seeded["owner_id"]
        )

    assert error.value.code == "task_storage_cleanup_required"
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


def test_task_delete_refuses_to_orphan_available_derived_artifact(tmp_path):
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

    with pytest.raises(InvalidTransition) as error:
        task_facade.delete_task(
            task_id=seeded["task_id"], owner_id=seeded["owner_id"]
        )

    assert error.value.code == "task_storage_cleanup_required"
    assert storage.exists(artifact.storage_key)
    assert file_repository.get_file(
        file_id=artifact.id, owner_id=seeded["owner_id"]
    ) is not None


def test_task_delete_removes_only_knowledge_link_and_retains_library_object(
    tmp_path,
):
    seeded = _prepared_task()
    storage = LocalStorage(tmp_path / "retained-knowledge")
    document_id = "retained-task-knowledge"
    content = b"long-lived-knowledge"
    with session_scope() as session:
        session.add(KnowledgeDocumentRecord(
            id=document_id,
            owner_id=seeded["owner_id"],
            stored_file_id=None,
            title="Reference",
            original_name="reference.pdf",
            content_type="application/pdf",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            status="ready",
            parser_version="v1",
            chunk_count=1,
        ))
    knowledge = file_repository.save_file(
        storage=storage,
        owner_id=seeded["owner_id"],
        kind="personal_knowledge",
        original_name="reference.pdf",
        content=content,
        content_type="application/pdf",
        knowledge_document_id=document_id,
    )
    with session_scope() as session:
        document = session.get(KnowledgeDocumentRecord, document_id)
        assert document is not None
        document.stored_file_id = knowledge.id
        session.add(AssignmentKnowledgeDocumentRecord(
            assignment_id=seeded["task_id"],
            document_id=document_id,
            selected_at=time.time(),
        ))

    task_facade.delete_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"]
    )

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


def test_task_delete_waits_for_running_cleanup_after_last_tombstone(tmp_path):
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

    with pytest.raises(InvalidTransition) as error:
        task_facade.delete_task(
            task_id=seeded["task_id"], owner_id=seeded["owner_id"]
        )

    assert error.value.code == "task_storage_cleanup_required"
    assert task_facade.get_task(
        task_id=seeded["task_id"], owner_id=seeded["owner_id"], full=False
    )["task_id"] == seeded["task_id"]


def test_task_delete_rejects_any_nonterminal_workflow_operation():
    seeded = _prepared_task()
    operation, created = workflow_repository.create_operation(
        assignment_id=seeded["task_id"],
        owner_id=seeded["owner_id"],
        operation_type="submission_recognition",
        input_hash=hashlib.sha256(b"active-delete-gate").hexdigest(),
    )
    assert created is True
    assert operation.status == "pending"

    with pytest.raises(InvalidTransition) as error:
        task_facade.delete_task(
            task_id=seeded["task_id"], owner_id=seeded["owner_id"]
        )

    assert error.value.code == "workflow_busy"
    assert workflow_repository.get_operation(
        operation.id, owner_id=seeded["owner_id"]
    ).status == "pending"


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
