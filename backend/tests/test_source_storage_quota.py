from __future__ import annotations

import hashlib
import io
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from backend.config import settings
from backend.db import (
    file_repository,
    source_storage_repository,
    workflow_repository,
)
from backend.db.models import AssignmentRecord, CourseRecord, StoredFileRecord, UserRecord
from backend.db.session import session_scope
from backend.domain.errors import (
    LeaseLost,
    SourceStorageIntegrityFailed,
    SourceStorageQuotaExceeded,
    SourceStorageReservationConflict,
    SourceStorageWriteFailed,
    VersionConflict,
)
from backend.services import task_facade
from backend.services import source_files as source_file_service
from backend.storage.base import StorageBackend, StorageObjectNotFound
from backend.storage.local import LocalStorage


def _seed_assignment(*, owner_id: str, assignment_id: str) -> None:
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id,
            username=owner_id,
            password_hash="hash",
            role="teacher",
            is_active=True,
        ))
        session.flush()
        session.add(CourseRecord(
            id=f"course-{assignment_id}",
            name="Quota course",
            teacher_id=owner_id,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=assignment_id,
            course_id=f"course-{assignment_id}",
            teacher_id=owner_id,
            name="Quota task",
            status="draft",
            version=1,
        ))


def _save_source(
    *, storage: StorageBackend, owner_id: str, assignment_id: str,
    content: bytes, kind: str = "problem_source",
    replacement_file_ids: tuple[str, ...] = (),
    replacement_group_id: str | None = None,
):
    return file_repository.save_file(
        storage=storage,
        owner_id=owner_id,
        kind=kind,
        original_name=f"{uuid.uuid4().hex}.pdf",
        content=content,
        content_type="application/pdf",
        assignment_id=assignment_id,
        replacement_file_ids=replacement_file_ids,
        replacement_group_id=replacement_group_id,
    )


def test_task_original_quota_accepts_exact_boundary_and_rejects_one_more(
    tmp_path, monkeypatch,
):
    owner_id = "quota-boundary-owner"
    assignment_id = "quota-boundary-task"
    _seed_assignment(owner_id=owner_id, assignment_id=assignment_id)
    monkeypatch.setattr(settings, "unfinished_source_quota_bytes", 10)
    storage = LocalStorage(tmp_path / "boundary")

    stored = _save_source(
        storage=storage,
        owner_id=owner_id,
        assignment_id=assignment_id,
        content=b"0123456789",
    )
    usage = source_storage_repository.source_quota_usage(owner_id)
    assert usage.used_bytes == usage.available_source_bytes == 10
    assert usage.as_dict()["available_bytes"] == 0
    assert stored.source_quota_owner_id == owner_id
    assert stored.source_quota_bytes == 10

    with pytest.raises(SourceStorageQuotaExceeded) as failure:
        _save_source(
            storage=storage,
            owner_id=owner_id,
            assignment_id=assignment_id,
            content=b"x",
        )
    assert failure.value.details == {
        **usage.as_dict(),
        "requested_bytes": 1,
        "next_step": "wait_for_automatic_cleanup_or_reduce_upload",
    }


def test_task_original_quota_is_owner_scoped_and_excludes_knowledge_files(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(settings, "unfinished_source_quota_bytes", 8)
    storage = LocalStorage(tmp_path / "owners")
    for owner_id, task_id in (("quota-owner-a", "quota-task-a"),
                              ("quota-owner-b", "quota-task-b")):
        _seed_assignment(owner_id=owner_id, assignment_id=task_id)

    first = _save_source(
        storage=storage,
        owner_id="quota-owner-a",
        assignment_id="quota-task-a",
        content=b"12345678",
    )
    second = _save_source(
        storage=storage,
        owner_id="quota-owner-b",
        assignment_id="quota-task-b",
        content=b"abcdefgh",
    )
    knowledge = file_repository.save_file(
        storage=storage,
        owner_id="quota-owner-a",
        kind="personal_knowledge",
        original_name="reference.pdf",
        content=b"knowledge-is-retained-and-separate",
        content_type="application/pdf",
    )

    assert source_storage_repository.source_quota_usage(
        "quota-owner-a"
    ).used_bytes == len(b"12345678")
    assert source_storage_repository.source_quota_usage(
        "quota-owner-b"
    ).used_bytes == len(b"abcdefgh")
    assert first.source_quota_owner_id == "quota-owner-a"
    assert second.source_quota_owner_id == "quota-owner-b"
    assert knowledge.source_quota_owner_id is None
    assert knowledge.source_quota_bytes == 0


def test_derived_task_artifact_intent_is_durable_but_zero_quota(
    tmp_path, monkeypatch,
):
    owner_id = "artifact-intent-owner"
    assignment_id = "artifact-intent-task"
    _seed_assignment(owner_id=owner_id, assignment_id=assignment_id)
    monkeypatch.setattr(settings, "unfinished_source_quota_bytes", 5)
    storage = LocalStorage(tmp_path / "artifact-intent")

    _save_source(
        storage=storage,
        owner_id=owner_id,
        assignment_id=assignment_id,
        content=b"12345",
    )
    artifact = file_repository.save_file(
        storage=storage,
        owner_id=owner_id,
        kind="submission_recognition_result",
        original_name="parsed.json",
        content=b"derived bytes do not consume original quota",
        content_type="application/json",
        assignment_id=assignment_id,
    )
    artifact_key = f"assignments/{assignment_id}/artifacts/pending.json"
    artifact_content = b"derived cleanup remains zero quota"
    artifact_intent = source_storage_repository.reserve_task_artifact_write(
        file_id="pending-artifact-intent",
        owner_id=owner_id,
        assignment_id=assignment_id,
        kind="submission_recognition_result",
        original_name="pending.json",
        storage_backend=storage.name,
        storage_key=artifact_key,
        content_type="application/json",
        requested_bytes=len(artifact_content),
        sha256=hashlib.sha256(artifact_content).hexdigest(),
    )
    storage.save(artifact_key, artifact_content)
    source_storage_repository.retain_source_reservation_for_cleanup(
        artifact_intent.id,
        error_code="source_storage_delete_failed",
    )

    usage = source_storage_repository.source_quota_usage(owner_id)
    assert usage.used_bytes == 5
    assert usage.reserved_bytes == 0
    assert usage.cleanup_pending_bytes == 0
    assert usage.retrying_cleanup_bytes == 0
    assert usage.cleanup_pending_count == 0
    assert usage.retrying_cleanup_count == 0
    assert artifact.source_quota_owner_id is None
    assert artifact.source_quota_bytes == 0


def test_concurrent_uploads_cannot_both_cross_one_owner_quota(
    tmp_path, monkeypatch,
):
    owner_id = "quota-concurrent-owner"
    assignment_id = "quota-concurrent-task"
    _seed_assignment(owner_id=owner_id, assignment_id=assignment_id)
    monkeypatch.setattr(settings, "unfinished_source_quota_bytes", 10)
    storage = LocalStorage(tmp_path / "concurrent")
    barrier = threading.Barrier(2)

    def upload(label: str):
        barrier.wait(timeout=5)
        try:
            return _save_source(
                storage=storage,
                owner_id=owner_id,
                assignment_id=assignment_id,
                content=(label.encode("ascii") * 6),
            )
        except Exception as exc:  # returned for deterministic cross-thread audit
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(upload, ("a", "b")))

    successes = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    rejected = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(successes) == 1
    assert len(rejected) == 1
    assert isinstance(rejected[0], SourceStorageQuotaExceeded)
    usage = source_storage_repository.source_quota_usage(owner_id)
    assert usage.used_bytes == 6
    assert usage.reserved_bytes == 0


def test_one_replacement_group_shares_old_credit_across_multiple_files(
    tmp_path, monkeypatch,
):
    owner_id = "quota-replacement-group-owner"
    assignment_id = "quota-replacement-group-task"
    _seed_assignment(owner_id=owner_id, assignment_id=assignment_id)
    monkeypatch.setattr(settings, "unfinished_source_quota_bytes", 10)
    storage = LocalStorage(tmp_path / "replacement-group")
    old = _save_source(
        storage=storage,
        owner_id=owner_id,
        assignment_id=assignment_id,
        content=b"old-source",
    )

    first = _save_source(
        storage=storage,
        owner_id=owner_id,
        assignment_id=assignment_id,
        content=b"1234",
        replacement_file_ids=(old.id,),
        replacement_group_id="replace-shared-group",
    )
    second = _save_source(
        storage=storage,
        owner_id=owner_id,
        assignment_id=assignment_id,
        content=b"567890",
        replacement_file_ids=(old.id,),
        replacement_group_id="replace-shared-group",
    )

    assert first.id != second.id
    # Old bytes remain truthfully charged until physical deletion succeeds.
    assert source_storage_repository.source_quota_usage(owner_id).used_bytes == 20
    with pytest.raises(SourceStorageQuotaExceeded) as failure:
        _save_source(
            storage=storage,
            owner_id=owner_id,
            assignment_id=assignment_id,
            content=b"x",
            replacement_file_ids=(old.id,),
            replacement_group_id="replace-shared-group",
        )
    assert failure.value.details["replacement_credit_bytes"] == 10


def test_concurrent_replacement_groups_cannot_reuse_one_old_file_credit(
    tmp_path, monkeypatch,
):
    owner_id = "quota-replacement-race-owner"
    assignment_id = "quota-replacement-race-task"
    _seed_assignment(owner_id=owner_id, assignment_id=assignment_id)
    monkeypatch.setattr(settings, "unfinished_source_quota_bytes", 10)
    storage = LocalStorage(tmp_path / "replacement-race")
    old = _save_source(
        storage=storage,
        owner_id=owner_id,
        assignment_id=assignment_id,
        content=b"old-source",
    )
    barrier = threading.Barrier(2)

    def replace(group_id: str):
        barrier.wait(timeout=5)
        try:
            return _save_source(
                storage=storage,
                owner_id=owner_id,
                assignment_id=assignment_id,
                content=group_id[-1:].encode("ascii") * 10,
                replacement_file_ids=(old.id,),
                replacement_group_id=group_id,
            )
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(replace, ("replacement-a", "replacement-b")))

    successes = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    rejected = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(successes) == 1
    assert len(rejected) == 1
    assert isinstance(rejected[0], SourceStorageReservationConflict)
    assert source_storage_repository.source_quota_usage(owner_id).used_bytes == 20


def test_missing_source_and_replacement_upload_complete_without_quota_drift(
    tmp_path, monkeypatch,
):
    """SQLite regression for the shared User -> StoredFile transition order."""

    owner_id = "quota-missing-replacement-owner"
    assignment_id = "quota-missing-replacement-task"
    _seed_assignment(owner_id=owner_id, assignment_id=assignment_id)
    monkeypatch.setattr(settings, "unfinished_source_quota_bytes", 10)
    storage = LocalStorage(tmp_path / "missing-replacement-race")
    old = _save_source(
        storage=storage,
        owner_id=owner_id,
        assignment_id=assignment_id,
        content=b"old-source",
    )
    barrier = threading.Barrier(2)

    def replace():
        barrier.wait(timeout=5)
        try:
            return _save_source(
                storage=storage,
                owner_id=owner_id,
                assignment_id=assignment_id,
                content=b"new-source",
                replacement_file_ids=(old.id,),
                replacement_group_id="missing-replacement-group",
            )
        except Exception as exc:  # deterministic cross-thread assertion
            return exc

    def mark_missing():
        barrier.wait(timeout=5)
        try:
            return source_storage_repository.mark_available_source_missing(
                file_id=old.id,
                owner_id=owner_id,
                assignment_id=assignment_id,
            )
        except Exception as exc:  # deterministic cross-thread assertion
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        replacement_future = executor.submit(replace)
        missing_future = executor.submit(mark_missing)
        replacement = replacement_future.result(timeout=10)
        marked_missing = missing_future.result(timeout=10)

    assert not isinstance(replacement, Exception)
    assert marked_missing is True
    retired = file_repository.get_file(file_id=old.id, owner_id=owner_id)
    assert retired.availability_status == "unavailable"
    assert retired.availability_reason == "missing"
    assert replacement.availability_status == "available"
    usage = source_storage_repository.source_quota_usage(owner_id)
    assert usage.used_bytes == 10
    assert usage.available_source_bytes == 10
    assert usage.reserved_bytes == 0


def test_disjoint_replacement_groups_share_global_net_delta_without_false_reject(
    tmp_path, monkeypatch,
):
    owner_id = "quota-disjoint-replacement-owner"
    first_task_id = "quota-disjoint-replacement-task-a"
    second_task_id = "quota-disjoint-replacement-task-b"
    _seed_assignment(owner_id=owner_id, assignment_id=first_task_id)
    with session_scope() as session:
        session.add(CourseRecord(
            id=f"course-{second_task_id}",
            name="Second quota course",
            teacher_id=owner_id,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=second_task_id,
            course_id=f"course-{second_task_id}",
            teacher_id=owner_id,
            name="Second quota task",
            status="draft",
            version=1,
        ))
    monkeypatch.setattr(settings, "unfinished_source_quota_bytes", 20)
    storage = LocalStorage(tmp_path / "disjoint-replacement")
    old_a = _save_source(
        storage=storage,
        owner_id=owner_id,
        assignment_id=first_task_id,
        content=b"old-task-a",
    )
    old_b = _save_source(
        storage=storage,
        owner_id=owner_id,
        assignment_id=second_task_id,
        content=b"old-task-b",
    )
    barrier = threading.Barrier(2)

    def replace(task_id: str, old_id: str, group_id: str):
        barrier.wait(timeout=5)
        return _save_source(
            storage=storage,
            owner_id=owner_id,
            assignment_id=task_id,
            content=b"new-source",
            replacement_file_ids=(old_id,),
            replacement_group_id=group_id,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                replace, first_task_id, old_a.id, "disjoint-group-a"
            ),
            executor.submit(
                replace, second_task_id, old_b.id, "disjoint-group-b"
            ),
        ]
        replacements = [future.result() for future in futures]

    assert len({stored.id for stored in replacements}) == 2
    assert source_storage_repository.source_quota_usage(owner_id).used_bytes == 40


def test_failed_replacement_write_releases_claim_for_immediate_new_group_retry(
    tmp_path, monkeypatch,
):
    class FailOnceStorage(LocalStorage):
        fail_next_save = False

        def save(self, key: str, content: bytes) -> None:
            super().save(key, content)
            if self.fail_next_save:
                self.fail_next_save = False
                raise OSError("write acknowledgement failed")

    owner_id = "quota-replacement-write-retry-owner"
    assignment_id = "quota-replacement-write-retry-task"
    _seed_assignment(owner_id=owner_id, assignment_id=assignment_id)
    monkeypatch.setattr(settings, "unfinished_source_quota_bytes", 10)
    storage = FailOnceStorage(tmp_path / "replacement-write-retry")
    old = _save_source(
        storage=storage,
        owner_id=owner_id,
        assignment_id=assignment_id,
        content=b"old-source",
    )

    storage.fail_next_save = True
    with pytest.raises(SourceStorageWriteFailed):
        _save_source(
            storage=storage,
            owner_id=owner_id,
            assignment_id=assignment_id,
            content=b"new-source",
            replacement_file_ids=(old.id,),
            replacement_group_id="failed-replacement-group",
        )

    assert source_storage_repository.source_quota_usage(owner_id).used_bytes == 10
    replacement = _save_source(
        storage=storage,
        owner_id=owner_id,
        assignment_id=assignment_id,
        content=b"new-source",
        replacement_file_ids=(old.id,),
        replacement_group_id="retry-replacement-group",
    )
    assert replacement.replacement_group_id == "retry-replacement-group"
    assert file_repository.get_file(
        file_id=old.id, owner_id=owner_id
    ).replacement_claim_group_id == "retry-replacement-group"


def test_deduplicated_staged_file_renews_an_expired_replacement_claim(
    tmp_path, monkeypatch,
):
    owner_id = "quota-replacement-dedupe-owner"
    assignment_id = "quota-replacement-dedupe-task"
    _seed_assignment(owner_id=owner_id, assignment_id=assignment_id)
    monkeypatch.setattr(settings, "unfinished_source_quota_bytes", 10)
    storage = LocalStorage(tmp_path / "replacement-dedupe-renewal")
    old = _save_source(
        storage=storage,
        owner_id=owner_id,
        assignment_id=assignment_id,
        content=b"old-source",
    )
    group_id = "reload-stable-replacement-group"
    staged, created = source_file_service.persist_problem_source(
        storage=storage,
        owner_id=owner_id,
        task_id=assignment_id,
        original_name="new.pdf",
        content=b"new-source",
        content_type="application/pdf",
        replacement_file_ids=(old.id,),
        replacement_group_id=group_id,
    )
    assert created is True
    with session_scope() as session:
        row = session.get(StoredFileRecord, old.id)
        row.replacement_claim_expires_at = 0.0

    reused, created = source_file_service.persist_problem_source(
        storage=storage,
        owner_id=owner_id,
        task_id=assignment_id,
        original_name="new.pdf",
        content=b"new-source",
        content_type="application/pdf",
        replacement_file_ids=(old.id,),
        replacement_group_id=group_id,
    )

    assert created is False
    assert reused.id == staged.id
    renewed = file_repository.get_file(file_id=old.id, owner_id=owner_id)
    assert renewed.replacement_claim_group_id == group_id
    assert renewed.replacement_claim_expires_at > time.time()


def test_stale_workflow_publication_can_retry_with_transferred_group_claim(
    tmp_path, monkeypatch,
):
    owner_id = "quota-replacement-stale-owner"
    assignment_id = "quota-replacement-stale-task"
    _seed_assignment(owner_id=owner_id, assignment_id=assignment_id)
    workflow_repository.ensure_workflow(
        assignment_id=assignment_id, owner_id=owner_id
    )
    monkeypatch.setattr(settings, "unfinished_source_quota_bytes", 10)
    storage = LocalStorage(tmp_path / "replacement-stale-publication")
    old = _save_source(
        storage=storage,
        owner_id=owner_id,
        assignment_id=assignment_id,
        content=b"old-source",
    )
    first_new = _save_source(
        storage=storage,
        owner_id=owner_id,
        assignment_id=assignment_id,
        content=b"new-",
        replacement_file_ids=(old.id,),
        replacement_group_id="stale-publication-group",
    )
    second_new = _save_source(
        storage=storage,
        owner_id=owner_id,
        assignment_id=assignment_id,
        content=b"source",
        replacement_file_ids=(old.id,),
        replacement_group_id="stale-publication-group",
    )
    publish = {
        "task_id": assignment_id,
        "owner_id": owner_id,
        "operation_type": "question_preparation",
        "input_hash": hashlib.sha256(b"stale-publication").hexdigest(),
        "operation_payload": {"schema": 1},
        "initial_checkpoint_stage": "sources_validated",
        "initial_checkpoint": {"schema": 1},
        "artifact_refs": [first_new.id, second_new.id],
        "workflow_changes": {
            "presentation_status": "extracting_problems",
            "active_operation": "question_preparation",
        },
        "workflow_job_id_fields": ("active_job_id", "extract_job_id"),
        "replacement_file_ids": (old.id,),
    }

    with pytest.raises(VersionConflict):
        task_facade.publish_checkpointed_operation_atomic(
            expected_workflow_revision=99,
            **publish,
        )

    operation, published, revision = (
        task_facade.publish_checkpointed_operation_atomic(
            expected_workflow_revision=0,
            **publish,
        )
    )
    assert published is True
    assert revision == 1
    assert operation.artifact_refs == [first_new.id, second_new.id]
    retired = file_repository.get_file(file_id=old.id, owner_id=owner_id)
    assert retired.availability_status == "cleanup_pending"
    assert retired.availability_reason == "replaced"


def test_formal_switch_rejects_replacement_after_old_claim_moves_to_new_group(
    tmp_path, monkeypatch,
):
    owner_id = "quota-replacement-stolen-owner"
    assignment_id = "quota-replacement-stolen-task"
    _seed_assignment(owner_id=owner_id, assignment_id=assignment_id)
    workflow_repository.ensure_workflow(
        assignment_id=assignment_id, owner_id=owner_id
    )
    monkeypatch.setattr(settings, "unfinished_source_quota_bytes", 30)
    storage = LocalStorage(tmp_path / "replacement-stolen-claim")
    old = _save_source(
        storage=storage,
        owner_id=owner_id,
        assignment_id=assignment_id,
        content=b"old-source",
    )
    first = _save_source(
        storage=storage,
        owner_id=owner_id,
        assignment_id=assignment_id,
        content=b"first-new!",
        replacement_file_ids=(old.id,),
        replacement_group_id="first-replacement-group",
    )
    with session_scope() as session:
        row = session.get(file_repository.StoredFileRecord, old.id)
        row.replacement_claim_expires_at = 0.0
    second = _save_source(
        storage=storage,
        owner_id=owner_id,
        assignment_id=assignment_id,
        content=b"second-new",
        replacement_file_ids=(old.id,),
        replacement_group_id="second-replacement-group",
    )
    assert second.replacement_group_id == "second-replacement-group"

    with pytest.raises(SourceStorageReservationConflict):
        task_facade.publish_checkpointed_operation_atomic(
            task_id=assignment_id,
            owner_id=owner_id,
            operation_type="question_preparation",
            input_hash=hashlib.sha256(b"stolen-claim-publication").hexdigest(),
            expected_workflow_revision=0,
            operation_payload={"schema": 1},
            initial_checkpoint_stage="sources_validated",
            initial_checkpoint={"schema": 1},
            artifact_refs=[first.id],
            workflow_changes={
                "presentation_status": "extracting_problems",
                "active_operation": "question_preparation",
            },
            workflow_job_id_fields=("active_job_id", "extract_job_id"),
            replacement_file_ids=(old.id,),
        )
    current = file_repository.get_file(file_id=old.id, owner_id=owner_id)
    assert current.availability_status == "available"
    assert current.replacement_claim_group_id == "second-replacement-group"
    assert workflow_repository.get_workflow(
        assignment_id, owner_id=owner_id
    ).workflow_revision == 0


def test_fenced_publication_locks_producer_before_workflow(
    tmp_path, monkeypatch,
):
    owner_id = "quota-publication-lock-owner"
    assignment_id = "quota-publication-lock-task"
    _seed_assignment(owner_id=owner_id, assignment_id=assignment_id)
    workflow_repository.ensure_workflow(
        assignment_id=assignment_id,
        owner_id=owner_id,
    )
    queued, created = workflow_repository.create_operation(
        assignment_id=assignment_id,
        owner_id=owner_id,
        operation_type="submission_recognition",
        input_hash=uuid.uuid4().hex,
    )
    assert created is True
    running = workflow_repository.claim_operation(
        queued.id,
        owner_id=owner_id,
        worker_id="publication-lock-worker",
        lease_seconds=60,
    )
    assert running.lease_token

    storage = LocalStorage(tmp_path / "publication-lock-order")
    content = b"lock-order"
    file_id = uuid.uuid4().hex
    storage_key = f"assignments/{assignment_id}/{file_id}/student.pdf"
    reservation = source_storage_repository.reserve_source_upload(
        file_id=file_id,
        file_owner_id=owner_id,
        kind="submission_source",
        original_name="student.pdf",
        storage_backend=storage.name,
        storage_key=storage_key,
        content_type="application/pdf",
        requested_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        assignment_id=assignment_id,
        submission_revision_id=None,
    )
    storage.save(storage_key, content)
    source_storage_repository.mark_reservation_object_written(reservation.id)

    lock_order: list[str] = []
    real_validate = source_storage_repository._validate_publication_fence
    real_lock_epoch = source_storage_repository._lock_source_workflow_epoch

    def record_producer_lock(*args, **kwargs):
        lock_order.append("producer_operation")
        return real_validate(*args, **kwargs)

    def record_workflow_lock(*args, **kwargs):
        lock_order.append("workflow")
        return real_lock_epoch(*args, **kwargs)

    monkeypatch.setattr(
        source_storage_repository,
        "_validate_publication_fence",
        record_producer_lock,
    )
    monkeypatch.setattr(
        source_storage_repository,
        "_lock_source_workflow_epoch",
        record_workflow_lock,
    )

    published = source_storage_repository.publish_source_reservation(
        reservation_id=reservation.id,
        assignment_id=assignment_id,
        submission_revision_id=None,
        knowledge_document_id=None,
        fence_operation_id=running.id,
        fence_operation_attempt=running.attempt,
        fence_lease_token=running.lease_token,
    )

    assert published.id == file_id
    assert lock_order == ["producer_operation", "workflow"]


def test_fenced_publication_rechecks_lease_after_acquiring_operation_lock(
    tmp_path, monkeypatch,
):
    owner_id = "quota-publication-expiry-owner"
    assignment_id = "quota-publication-expiry-task"
    _seed_assignment(owner_id=owner_id, assignment_id=assignment_id)
    workflow_repository.ensure_workflow(
        assignment_id=assignment_id,
        owner_id=owner_id,
    )
    queued, _created = workflow_repository.create_operation(
        assignment_id=assignment_id,
        owner_id=owner_id,
        operation_type="submission_recognition",
        input_hash=uuid.uuid4().hex,
    )
    running = workflow_repository.claim_operation(
        queued.id,
        owner_id=owner_id,
        worker_id="publication-expiry-worker",
        lease_seconds=60,
    )
    assert running.lease_token
    storage = LocalStorage(tmp_path / "publication-expiry")
    content = b"expires-during-lock-wait"
    file_id = uuid.uuid4().hex
    storage_key = f"assignments/{assignment_id}/{file_id}/student.pdf"
    reservation = source_storage_repository.reserve_source_upload(
        file_id=file_id,
        file_owner_id=owner_id,
        kind="submission_source",
        original_name="student.pdf",
        storage_backend=storage.name,
        storage_key=storage_key,
        content_type="application/pdf",
        requested_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        assignment_id=assignment_id,
        submission_revision_id=None,
    )
    storage.save(storage_key, content)
    source_storage_repository.mark_reservation_object_written(reservation.id)
    observed_times = iter((0.0, 10**20))
    monkeypatch.setattr(
        source_storage_repository,
        "time",
        SimpleNamespace(time=lambda: next(observed_times)),
    )

    with pytest.raises(LeaseLost):
        source_storage_repository.publish_source_reservation(
            reservation_id=reservation.id,
            assignment_id=assignment_id,
            submission_revision_id=None,
            knowledge_document_id=None,
            fence_operation_id=running.id,
            fence_operation_attempt=running.attempt,
            fence_lease_token=running.lease_token,
        )

    assert file_repository.get_file(file_id=file_id, owner_id=owner_id) is None


class _UncertainWriteStorage(StorageBackend):
    name = "uncertain"

    def __init__(self, *, corrupt: bool = False):
        self.objects: dict[str, bytes] = {}
        self.corrupt = corrupt

    def save(self, key: str, content: bytes) -> None:
        self.objects[key] = content + (b"corrupt" if self.corrupt else b"")
        if not self.corrupt:
            raise OSError("provider acknowledgement lost")

    def open(self, key: str):
        if key not in self.objects:
            raise StorageObjectNotFound("missing")
        return io.BytesIO(self.objects[key])

    def delete(self, key: str) -> None:
        raise OSError("provider deletion unavailable")

    def exists(self, key: str) -> bool:
        return key in self.objects


@pytest.mark.parametrize(
    ("storage", "expected_error"),
    [
        (_UncertainWriteStorage(), SourceStorageWriteFailed),
        (_UncertainWriteStorage(corrupt=True), SourceStorageIntegrityFailed),
    ],
)
def test_uncertain_write_or_integrity_cleanup_stays_charged(
    monkeypatch, storage, expected_error,
):
    owner_id = f"quota-uncertain-{expected_error.__name__}"
    assignment_id = f"quota-uncertain-task-{expected_error.__name__}"
    _seed_assignment(owner_id=owner_id, assignment_id=assignment_id)
    monkeypatch.setattr(settings, "unfinished_source_quota_bytes", 100)

    with pytest.raises(expected_error):
        _save_source(
            storage=storage,
            owner_id=owner_id,
            assignment_id=assignment_id,
            content=b"uncertain",
        )

    usage = source_storage_repository.source_quota_usage(owner_id)
    assert usage.used_bytes == len(b"uncertain")
    assert usage.reserved_bytes == len(b"uncertain")
    assert usage.available_source_bytes == 0
    assert usage.cleanup_pending_bytes == len(b"uncertain")
    assert usage.cleanup_pending_count == 1
    assert usage.retrying_cleanup_bytes == 0
    assert usage.retrying_cleanup_count == 0
