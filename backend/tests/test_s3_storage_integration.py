"""Opt-in integration checks against a real S3-compatible object store.

Set ``SMARTAI_TEST_S3_INTEGRATION=1`` together with the normal
``SMARTAI_STORAGE_S3_*`` application settings to enable this module.  The
configured bucket must already exist and should be dedicated to tests.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest


_RUN_S3_INTEGRATION = os.environ.get(
    "SMARTAI_TEST_S3_INTEGRATION", ""
).strip().lower() in {"1", "true", "yes", "on"}
_REQUIRED_ENV = (
    "SMARTAI_STORAGE_S3_BUCKET",
    "SMARTAI_STORAGE_S3_ACCESS_KEY",
    "SMARTAI_STORAGE_S3_SECRET_KEY",
)
_MISSING_ENV = tuple(name for name in _REQUIRED_ENV if not os.environ.get(name))


pytestmark = pytest.mark.skipif(
    not _RUN_S3_INTEGRATION or bool(_MISSING_ENV),
    reason=(
        "Set SMARTAI_TEST_S3_INTEGRATION=1 and SMARTAI_STORAGE_S3_* to run "
        "the real S3-compatible storage integration test."
    ),
)


def test_s3_storage_round_trip_listing_delete_and_missing_object() -> None:
    from backend.storage.base import StorageObjectNotFound
    from backend.storage.object import S3Storage

    storage = S3Storage()
    namespace = uuid.uuid4().hex
    prefix = f"integration-tests/s3-storage/{namespace}/"
    first_key = f"{prefix}nested/first.bin"
    second_key = f"{prefix}second.txt"
    adjacent_key = f"integration-tests/s3-storage/{namespace}-adjacent/outside.txt"
    owned_keys = (first_key, second_key, adjacent_key)

    try:
        assert storage.ready()
        assert storage.client.get_bucket_versioning(Bucket=storage.bucket).get(
            "Status"
        ) == "Enabled", (
            "The dedicated source bucket must have versioning enabled so the "
            "permanent-deletion contract is exercised."
        )

        storage.save(first_key, b"older physical version")
        storage.save(first_key, b"\x00SmarTAI real S3 round trip\xff")
        storage.save(second_key, "第二个对象".encode())
        storage.save(adjacent_key, b"must not match the exact prefix")

        assert storage.exists(first_key)
        assert storage.open(first_key).read() == b"\x00SmarTAI real S3 round trip\xff"
        assert storage.open(second_key).read() == "第二个对象".encode()
        assert storage.list_keys(prefix) == sorted((first_key, second_key))

        # Simulate the historical unsafe behavior: a versioned DeleteObject
        # only hides the current key and creates another physical marker.
        storage.client.delete_object(Bucket=storage.bucket, Key=first_key)
        assert not storage.exists(first_key)
        assert storage.list_keys(prefix) == sorted((first_key, second_key))

        # The storage contract must now purge the marker and every old version.
        storage.delete(first_key)

        assert not storage.exists(first_key)
        assert storage.list_keys(prefix) == [second_key]
        with pytest.raises(StorageObjectNotFound, match="storage_object_not_found"):
            storage.open(first_key)
    finally:
        for key in owned_keys:
            storage.delete(key)

    assert storage.list_keys(prefix) == []


async def _drain_worker(worker) -> None:
    for _ in range(500):
        if worker.in_flight_count == 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("task deletion worker did not drain")


@pytest.mark.asyncio
async def test_task_delete_worker_reconciles_only_the_exact_assignment_prefix(
    monkeypatch,
) -> None:
    from sqlalchemy import update

    from backend.db.models import AssignmentRecord, UserRecord
    from backend.db.session import session_scope
    from backend.db.workflow_repository import WorkflowOperationRecord
    from backend.domain.source_storage import TASK_DELETE_OPERATION
    from backend.services import task_deletion, task_facade
    from backend.services.workflow_worker import WorkflowWorker
    from backend.storage.object import S3Storage

    storage = S3Storage()
    owner_id = f"s3-task-owner-{uuid.uuid4().hex}"
    with session_scope() as session:
        session.add(
            UserRecord(
                id=owner_id,
                username=owner_id,
                password_hash="test-only",
                role="teacher",
                is_active=True,
            )
        )
    task = task_facade.create_task(
        owner_id=owner_id,
        name="Real S3 prefix reconciliation",
        semester_id=None,
        course_id=None,
        idempotency_key=f"real-s3-{uuid.uuid4().hex}",
    )
    task_id = task["task_id"]
    orphan_key = f"assignments/{task_id}/crash-gap/orphan.bin"
    adjacent_key = f"assignments/{task_id}-adjacent/must-survive.bin"
    worker = WorkflowWorker(
        handlers={TASK_DELETE_OPERATION: task_deletion.run_task_deletion},
        worker_id="real-s3-task-delete-worker",
        lease_seconds=60,
        heartbeat_seconds=60,
        poll_seconds=60,
        claim_batch_size=1,
        max_in_flight=1,
        shutdown_seconds=1,
    )

    monkeypatch.setattr(task_deletion, "get_storage", lambda: storage)
    try:
        storage.save(orphan_key, b"saved before metadata publication")
        storage.save(adjacent_key, b"similar key outside the exact prefix")

        result = task_facade.delete_task(task_id=task_id, owner_id=owner_id)
        assert result["status"] == "deletion_pending"

        for _ in range(12):
            with session_scope() as session:
                if session.get(AssignmentRecord, task_id) is None:
                    break
                session.execute(
                    update(WorkflowOperationRecord)
                    .where(
                        WorkflowOperationRecord.assignment_id == task_id,
                        WorkflowOperationRecord.operation_type
                        == TASK_DELETE_OPERATION,
                        WorkflowOperationRecord.status == "pending",
                    )
                    .values(expires_at=0.0)
                )
            await worker.poll_once()
            await _drain_worker(worker)
        else:
            raise AssertionError("task deletion did not finish")

        assert not storage.exists(orphan_key)
        assert storage.exists(adjacent_key)
    finally:
        storage.delete(orphan_key)
        storage.delete(adjacent_key)


@pytest.mark.asyncio
async def test_knowledge_cleanup_worker_permanently_removes_every_object_version() -> None:
    """The knowledge ledger is released only after version-aware deletion."""
    from backend.db.knowledge_storage_repository import (
        get_document_storage,
        knowledge_storage_usage,
        request_document_cleanup,
    )
    from backend.db.models import UserRecord
    from backend.db.session import session_scope
    from backend.services.knowledge_storage import (
        KnowledgeStorageWorker,
        persist_knowledge_upload,
    )
    from backend.storage.object import S3Storage

    storage = S3Storage()
    owner_id = f"s3-knowledge-owner-{uuid.uuid4().hex}"
    with session_scope() as session:
        session.add(UserRecord(
            id=owner_id,
            username=owner_id,
            password_hash="test-only",
            role="teacher",
            is_active=True,
        ))

    body = b"knowledge bytes with multiple physical versions"
    uploaded = persist_knowledge_upload(
        storage=storage,
        owner_id=owner_id,
        original_name="versioned-knowledge.txt",
        content=body,
        content_type="text/plain",
    )
    key = uploaded.entry.storage_key
    prefix = key.rsplit("/", 1)[0] + "/"
    try:
        # Create a second physical version with identical verified bytes. A
        # plain DeleteObject would leave both versions billable in the bucket.
        storage.save(key, body)
        assert storage.list_keys(prefix) == [key]
        request = request_document_cleanup(uploaded.document_id, owner_id)
        assert request.status == "cleanup_pending"
        assert knowledge_storage_usage(owner_id).used_bytes == len(body)

        assert await KnowledgeStorageWorker(storage=storage).run_once(
            max_claims=1
        ) == 1

        assert get_document_storage(uploaded.document_id, owner_id) is None
        assert knowledge_storage_usage(owner_id).used_bytes == 0
        assert storage.list_keys(prefix) == []
    finally:
        storage.delete(key)
