from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import delete, select

from backend.config import settings
from backend.db.knowledge_storage_repository import (
    claim_next_cleanup,
    claim_next_orphan_guard,
    detach_assignment_documents_in_session,
    finish_cleanup,
    get_document_storage,
    knowledge_storage_usage,
    lock_knowledge_owner_in_session,
    mark_document_attached_in_session,
    orphan_guard_count,
    request_document_cleanup,
    reserve_upload,
    visible_document_ids,
)
from backend.db.models import (
    AssignmentKnowledgeDocumentRecord,
    AssignmentRecord,
    CourseRecord,
    GradingRunRecord,
    KnowledgeStorageOrphanGuardRecord,
    KnowledgeStorageRecord,
    UserRecord,
)
from backend.db.session import session_scope
from backend.db.workflow_repository import GradingRunSetupRecord
from backend.domain.errors import (
    InvalidTransition,
    KnowledgeStorageQuotaExceeded,
    KnowledgeStorageReservationConflict,
    KnowledgeStorageWriteFailed,
    NotFound,
)
from backend.domain.knowledge_storage import (
    KNOWLEDGE_RETENTION_RETAINED,
    KNOWLEDGE_RETENTION_TASK_ONLY,
    KNOWLEDGE_STORAGE_AVAILABLE,
    KNOWLEDGE_STORAGE_CLEANUP_PENDING,
)
from backend.services.knowledge_storage import (
    KnowledgeStorageWorker,
    persist_knowledge_upload,
)
from backend.storage.local import LocalStorage


def _seed_owner(owner_id: str = "knowledge-owner") -> str:
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id,
            username=owner_id,
            role="teacher",
            password_hash="x",
            is_active=True,
        ))
    return owner_id


def _seed_assignment(owner_id: str, suffix: str = "one") -> str:
    course_id = f"knowledge-course-{suffix}"
    assignment_id = f"knowledge-assignment-{suffix}"
    with session_scope() as session:
        session.add(CourseRecord(
            id=course_id,
            name="Knowledge course",
            code="",
            description="",
            teacher_id=owner_id,
        ))
        session.flush()
        session.add(AssignmentRecord(
            id=assignment_id,
            course_id=course_id,
            teacher_id=owner_id,
            name="Knowledge assignment",
            description="",
            status="draft",
            version=1,
        ))
    return assignment_id


def test_reserved_bytes_are_independently_charged_and_quota_is_atomic(
    monkeypatch,
):
    owner_id = _seed_owner()
    monkeypatch.setattr(settings, "knowledge_storage_quota_bytes", 5)
    first = reserve_upload(
        owner_id=owner_id,
        original_name="first.txt",
        size_bytes=4,
        sha256=hashlib.sha256(b"1234").hexdigest(),
    )

    usage = knowledge_storage_usage(owner_id)
    assert first.created is True
    assert usage.used_bytes == usage.reserved_bytes == 4
    assert usage.available_bytes == 1
    with pytest.raises(KnowledgeStorageQuotaExceeded) as caught:
        reserve_upload(
            owner_id=owner_id,
            original_name="second.txt",
            size_bytes=2,
            sha256=hashlib.sha256(b"56").hexdigest(),
        )
    assert caught.value.details == {
        **usage.as_dict(),
        "requested_bytes": 2,
    }


def test_owner_hash_dedup_promotes_task_only_without_double_charge(
    tmp_path,
):
    owner_id = _seed_owner()
    assignment_id = _seed_assignment(owner_id)
    storage = LocalStorage(tmp_path / "knowledge")
    first = persist_knowledge_upload(
        storage=storage,
        owner_id=owner_id,
        original_name="task.txt",
        content=b"same bytes",
        retention_policy=KNOWLEDGE_RETENTION_TASK_ONLY,
        origin_assignment_id=assignment_id,
    )
    second = persist_knowledge_upload(
        storage=storage,
        owner_id=owner_id,
        original_name="library.txt",
        content=b"same bytes",
        retention_policy=KNOWLEDGE_RETENTION_RETAINED,
    )

    current = get_document_storage(first.document_id, owner_id)
    assert current is not None
    assert first.created is True
    assert second.created is False
    assert second.disposition == "existing"
    assert second.document_id == first.document_id
    assert current.retention_policy == KNOWLEDGE_RETENTION_RETAINED
    assert current.state == KNOWLEDGE_STORAGE_AVAILABLE
    assert current.unattached_expires_at is None
    assert knowledge_storage_usage(owner_id).used_bytes == len(b"same bytes")
    assert orphan_guard_count() == 0


def test_cleanup_failure_remains_visible_and_charged_until_retry_succeeds(
    tmp_path,
):
    class DeleteFails(LocalStorage):
        fail_delete = True

        def delete(self, key: str) -> None:
            if self.fail_delete:
                raise OSError("temporary delete failure")
            super().delete(key)

    owner_id = _seed_owner()
    storage = DeleteFails(tmp_path / "knowledge")
    uploaded = persist_knowledge_upload(
        storage=storage,
        owner_id=owner_id,
        original_name="retained.txt",
        content=b"charged",
    )
    request = request_document_cleanup(uploaded.document_id, owner_id)
    assert request.cleanup_operation_id

    assert asyncio.run(
        KnowledgeStorageWorker(storage=storage).run_once(max_claims=1)
    ) == 1
    usage = knowledge_storage_usage(owner_id)
    current = get_document_storage(uploaded.document_id, owner_id)
    assert usage.used_bytes == len(b"charged")
    assert usage.cleanup_pending_bytes == len(b"charged")
    assert usage.retrying_cleanup_bytes == len(b"charged")
    assert current is not None
    assert current.state == KNOWLEDGE_STORAGE_CLEANUP_PENDING
    assert current.retrying_cleanup is True

    storage.fail_delete = False
    claim = claim_next_cleanup(
        worker_id="retry-worker",
        now=time.time() + settings.knowledge_cleanup_retry_max_seconds + 1,
    )
    assert claim is not None
    storage.delete(claim.storage_key)
    result = finish_cleanup(
        claim, deleted=True,
        now=time.time() + settings.knowledge_cleanup_retry_max_seconds + 1,
    )
    assert result.status == "deleted"
    assert get_document_storage(uploaded.document_id, owner_id) is None
    assert knowledge_storage_usage(owner_id).used_bytes == 0


def test_partial_upload_and_failed_compensation_stays_charged_for_worker(
    tmp_path,
):
    class PartialWrite(LocalStorage):
        allow_delete = False

        def save(self, key: str, content: bytes) -> None:
            super().save(key, content)
            raise OSError("write acknowledgement lost")

        def delete(self, key: str) -> None:
            if not self.allow_delete:
                raise OSError("delete unavailable")
            super().delete(key)

    owner_id = _seed_owner()
    storage = PartialWrite(tmp_path / "knowledge")
    with pytest.raises(KnowledgeStorageWriteFailed):
        persist_knowledge_upload(
            storage=storage,
            owner_id=owner_id,
            original_name="partial.txt",
            content=b"partial",
        )
    usage = knowledge_storage_usage(owner_id)
    assert usage.used_bytes == len(b"partial")
    assert usage.cleanup_pending_bytes == len(b"partial")

    storage.allow_delete = True
    assert asyncio.run(
        KnowledgeStorageWorker(storage=storage).run_once(max_claims=1)
    ) == 1
    assert knowledge_storage_usage(owner_id).used_bytes == 0
    # A save exception is outcome-uncertain even after a compensating DELETE:
    # its zero-quota late-writer guard remains and is never a manual user action.
    assert orphan_guard_count() == 1


def test_known_complete_integrity_failure_retires_guard_and_charge(tmp_path):
    class CorruptRead(LocalStorage):
        def open(self, key: str):
            stream = super().open(key)
            stream.close()
            from io import BytesIO

            return BytesIO(b"corrupt")

    owner_id = _seed_owner()
    storage = CorruptRead(tmp_path / "knowledge")
    from backend.domain.errors import KnowledgeStorageIntegrityFailed

    with pytest.raises(KnowledgeStorageIntegrityFailed):
        persist_knowledge_upload(
            storage=storage,
            owner_id=owner_id,
            original_name="integrity.txt",
            content=b"expected",
        )
    assert knowledge_storage_usage(owner_id).used_bytes == 0
    assert orphan_guard_count() == 0
    assert storage.list_keys(f"users/{owner_id}/knowledge") == []


def test_blocked_put_survives_user_cascade_guard_and_late_object_is_removed(
    tmp_path,
):
    class BlockingWrite(LocalStorage):
        def __init__(self, root):
            super().__init__(root)
            self.started = threading.Event()
            self.release = threading.Event()

        def save(self, key: str, content: bytes) -> None:
            self.started.set()
            if not self.release.wait(timeout=10):
                raise TimeoutError("test did not release blocked PUT")
            super().save(key, content)

    owner_id = _seed_owner()
    storage = BlockingWrite(tmp_path / "knowledge")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            persist_knowledge_upload,
            storage=storage,
            owner_id=owner_id,
            original_name="private-name.pdf",
            content=b"late bytes",
        )
        assert storage.started.wait(timeout=5)
        with session_scope() as session:
            guard = session.scalar(select(KnowledgeStorageOrphanGuardRecord))
            assert guard is not None
            storage_key = guard.storage_key
            assert "private-name" not in storage_key
            assert session.scalar(select(KnowledgeStorageRecord.id)) is not None
            session.execute(delete(UserRecord).where(UserRecord.id == owner_id))
        # The pre-I/O guard has no User FK and survives the ledger cascade.
        assert orphan_guard_count(storage_key=storage_key) == 1
        storage.release.set()
        with pytest.raises(NotFound):
            future.result(timeout=10)

    # Foreground post-save CAS lost, so it exact-deleted and ACKed the guard.
    assert storage.exists(storage_key) is False
    assert orphan_guard_count(storage_key=storage_key) == 0
    with session_scope() as session:
        assert session.scalar(select(KnowledgeStorageRecord.id)) is None


def test_uncertain_writer_guard_rechecks_forever_without_charging_quota(
    tmp_path,
):
    class AcknowledgementLost(LocalStorage):
        def save(self, key: str, content: bytes) -> None:
            super().save(key, content)
            raise OSError("PUT outcome uncertain")

    owner_id = _seed_owner()
    storage = AcknowledgementLost(tmp_path / "knowledge")
    with pytest.raises(KnowledgeStorageWriteFailed):
        persist_knowledge_upload(
            storage=storage,
            owner_id=owner_id,
            original_name="uncertain.txt",
            content=b"uncertain",
        )
    with session_scope() as session:
        guard = session.scalar(select(KnowledgeStorageOrphanGuardRecord))
        assert guard is not None
        storage_key = guard.storage_key

    worker = KnowledgeStorageWorker(storage=storage)
    # A matching cleanup ledger suppresses guard claims; ledger cleanup owns the
    # first deletion and releases quota only after exact-key absence is proven.
    assert claim_next_orphan_guard(
        worker_id="must-not-race-ledger", now=time.time() + 10_000,
    ) is None
    assert asyncio.run(worker.run_once(max_claims=1)) == 1
    assert knowledge_storage_usage(owner_id).used_bytes == 0
    assert orphan_guard_count(storage_key=storage_key) == 1

    # Simulate a provider completing its timed-out PUT after quota was released.
    LocalStorage.save(storage, storage_key, b"uncertain")
    assert storage.exists(storage_key) is True
    assert asyncio.run(worker.run_once(max_claims=1)) == 1
    assert storage.exists(storage_key) is False
    assert orphan_guard_count(storage_key=storage_key) == 1
    assert knowledge_storage_usage(owner_id).used_bytes == 0

    # Even a much later second arrival is erased; only an explicit writer ACK
    # can retire this zero-quota internal guard.
    LocalStorage.save(storage, storage_key, b"uncertain")
    with session_scope() as session:
        guard = session.scalar(select(KnowledgeStorageOrphanGuardRecord))
        assert guard is not None
        guard.next_check_at = 0
    assert asyncio.run(worker.run_once(max_claims=1)) == 1
    assert storage.exists(storage_key) is False
    assert orphan_guard_count(storage_key=storage_key) == 1


def test_orphan_guard_is_not_starved_by_cleanup_backlog(tmp_path):
    orphan_owner = _seed_owner("orphan-owner")
    storage = LocalStorage(tmp_path / "knowledge")
    reservation = reserve_upload(
        owner_id=orphan_owner,
        original_name="late.txt",
        size_bytes=4,
        sha256=hashlib.sha256(b"late").hexdigest(),
    )
    with session_scope() as session:
        session.execute(delete(UserRecord).where(UserRecord.id == orphan_owner))
        guard = session.scalar(select(KnowledgeStorageOrphanGuardRecord))
        assert guard is not None
        guard.next_check_at = 0
    LocalStorage.save(storage, reservation.entry.storage_key, b"late")

    backlog_owner = _seed_owner("backlog-owner")
    first = persist_knowledge_upload(
        storage=storage, owner_id=backlog_owner,
        original_name="one.txt", content=b"one",
    )
    second = persist_knowledge_upload(
        storage=storage, owner_id=backlog_owner,
        original_name="two.txt", content=b"two",
    )
    request_document_cleanup(first.document_id, backlog_owner)
    request_document_cleanup(second.document_id, backlog_owner)
    assert knowledge_storage_usage(backlog_owner).cleanup_pending_count == 2

    worker = KnowledgeStorageWorker(storage=storage)
    assert asyncio.run(worker.run_once(max_claims=1)) == 1
    assert storage.exists(reservation.entry.storage_key) is False
    assert orphan_guard_count(storage_key=reservation.entry.storage_key) == 1
    assert knowledge_storage_usage(backlog_owner).cleanup_pending_count == 2


def test_task_only_last_reference_cleans_but_retained_only_detaches(tmp_path):
    owner_id = _seed_owner()
    assignment_id = _seed_assignment(owner_id)
    storage = LocalStorage(tmp_path / "knowledge")
    task_only = persist_knowledge_upload(
        storage=storage,
        owner_id=owner_id,
        original_name="temporary.txt",
        content=b"temporary",
        retention_policy=KNOWLEDGE_RETENTION_TASK_ONLY,
        origin_assignment_id=assignment_id,
    )
    retained = persist_knowledge_upload(
        storage=storage,
        owner_id=owner_id,
        original_name="retained.txt",
        content=b"retained",
    )
    with session_scope() as session:
        lock_knowledge_owner_in_session(session, owner_id)
        session.add_all([
            AssignmentKnowledgeDocumentRecord(
                assignment_id=assignment_id,
                document_id=task_only.document_id,
                source_kind="upload",
            ),
            AssignmentKnowledgeDocumentRecord(
                assignment_id=assignment_id,
                document_id=retained.document_id,
                source_kind="upload",
            ),
        ])
        session.flush()
        mark_document_attached_in_session(
            session,
            assignment_id=assignment_id,
            owner_id=owner_id,
            document_id=task_only.document_id,
        )
        mark_document_attached_in_session(
            session,
            assignment_id=assignment_id,
            owner_id=owner_id,
            document_id=retained.document_id,
        )
    assert get_document_storage(
        task_only.document_id, owner_id
    ).unattached_expires_at is None  # type: ignore[union-attr]

    with session_scope() as session:
        requests = detach_assignment_documents_in_session(
            session,
            assignment_id=assignment_id,
            owner_id=owner_id,
        )
    statuses = {request.document_id: request.status for request in requests}
    assert statuses[task_only.document_id] == "cleanup_pending"
    assert statuses[retained.document_id] == "retained"
    assert get_document_storage(
        task_only.document_id, owner_id
    ).state == KNOWLEDGE_STORAGE_CLEANUP_PENDING  # type: ignore[union-attr]
    assert get_document_storage(
        retained.document_id, owner_id
    ).state == KNOWLEDGE_STORAGE_AVAILABLE  # type: ignore[union-attr]


def test_visibility_hides_task_only_and_cleanup_pending_from_global_library(
    tmp_path,
):
    owner_id = _seed_owner()
    assignment_id = _seed_assignment(owner_id)
    storage = LocalStorage(tmp_path / "knowledge")
    retained = persist_knowledge_upload(
        storage=storage, owner_id=owner_id, original_name="r.txt", content=b"r"
    )
    task_only = persist_knowledge_upload(
        storage=storage,
        owner_id=owner_id,
        original_name="t.txt",
        content=b"t",
        retention_policy=KNOWLEDGE_RETENTION_TASK_ONLY,
        origin_assignment_id=assignment_id,
    )
    assert visible_document_ids(owner_id, include_task_only=False) == (
        retained.document_id,
    )
    assert set(visible_document_ids(owner_id, include_task_only=True)) == {
        retained.document_id,
        task_only.document_id,
    }
    request_document_cleanup(retained.document_id, owner_id)
    assert visible_document_ids(owner_id, include_task_only=False) == ()


def test_explicit_delete_fails_closed_for_frozen_active_grading_input(tmp_path):
    owner_id = _seed_owner()
    assignment_id = _seed_assignment(owner_id)
    storage = LocalStorage(tmp_path / "knowledge")
    uploaded = persist_knowledge_upload(
        storage=storage,
        owner_id=owner_id,
        original_name="frozen.txt",
        content=b"frozen",
    )
    with session_scope() as session:
        session.add(GradingRunRecord(
            id="knowledge-active-run",
            assignment_id=assignment_id,
            teacher_id=owner_id,
            status="queued",
            total_submissions=0,
            completed_submissions=0,
            failed_submissions=0,
        ))
        session.flush()
        session.add(GradingRunSetupRecord(
            grading_run_id="knowledge-active-run",
            assignment_id=assignment_id,
            owner_id=owner_id,
            setup={},
            input_manifest={
                "knowledge_document_ids": [uploaded.document_id],
            },
            fingerprint="f" * 64,
        ))

    with pytest.raises(InvalidTransition) as caught:
        request_document_cleanup(uploaded.document_id, owner_id)
    assert caught.value.code == "knowledge_document_in_active_grading_run"
    assert get_document_storage(
        uploaded.document_id, owner_id
    ).state == KNOWLEDGE_STORAGE_AVAILABLE  # type: ignore[union-attr]


def test_grading_bundle_rejects_cleanup_pending_knowledge_atomically(tmp_path):
    from backend.db import grading_repository, workflow_repository

    owner_id = _seed_owner()
    assignment_id = _seed_assignment(owner_id)
    workflow = workflow_repository.ensure_workflow(
        assignment_id=assignment_id, owner_id=owner_id,
    )
    uploaded = persist_knowledge_upload(
        storage=LocalStorage(tmp_path / "knowledge"),
        owner_id=owner_id,
        original_name="deleting.txt",
        content=b"deleting",
    )
    request_document_cleanup(uploaded.document_id, owner_id)

    with pytest.raises(InvalidTransition) as caught:
        grading_repository.create_run_bundle(
            assignment_id,
            teacher_id=owner_id,
            revision_ids=[],
            setup={},
            setup_fingerprint="f" * 64,
            input_manifest={
                "knowledge_document_ids": [uploaded.document_id],
            },
            workflow_expected_revision=workflow.workflow_revision,
        )
    assert caught.value.code == "knowledge_storage_input_unavailable"


def test_reupload_does_not_resurrect_cleanup_pending(tmp_path):
    owner_id = _seed_owner()
    storage = LocalStorage(tmp_path / "knowledge")
    uploaded = persist_knowledge_upload(
        storage=storage,
        owner_id=owner_id,
        original_name="same.txt",
        content=b"same",
    )
    request_document_cleanup(uploaded.document_id, owner_id)
    with pytest.raises(KnowledgeStorageReservationConflict) as caught:
        persist_knowledge_upload(
            storage=storage,
            owner_id=owner_id,
            original_name="same-again.txt",
            content=b"same",
        )
    assert caught.value.code == "knowledge_storage_cleanup_pending"
