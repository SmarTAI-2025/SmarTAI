from __future__ import annotations

import hashlib
import io
import threading
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
from backend.db.models import AssignmentRecord, CourseRecord, UserRecord
from backend.db.session import session_scope
from backend.domain.errors import (
    LeaseLost,
    SourceStorageIntegrityFailed,
    SourceStorageQuotaExceeded,
    SourceStorageWriteFailed,
)
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
):
    return file_repository.save_file(
        storage=storage,
        owner_id=owner_id,
        kind=kind,
        original_name=f"{uuid.uuid4().hex}.pdf",
        content=content,
        content_type="application/pdf",
        assignment_id=assignment_id,
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
    real_lock_epoch = source_storage_repository._lock_source_epoch

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
        "_lock_source_epoch",
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
