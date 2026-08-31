"""Durable file metadata linked to assignments, submission revisions, or
knowledge documents.

The legacy ``task_id`` link is gone: a stored file now carries explicit
nullable FK columns (``assignment_id`` / ``submission_revision_id`` /
``knowledge_document_id``). Exactly one business link is expected per file,
enforced in application code; the columns stay nullable so a knowledge-only
upload that predates its document row can still be recorded.

Owner-scoped reads use the owner predicate in SQL so a non-owner reads nothing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, exists, select, update

from backend.db.models import (
    AssignmentRecord,
    SourceStorageReservationRecord,
    StoredFileRecord,
)
from backend.db.session import session_scope
from backend.domain.errors import (
    InvalidTransition,
    LeaseLost,
    NotFound,
    SourceStorageIntegrityFailed,
    SourceStorageWriteFailed,
)
from backend.domain.source_storage import (
    RAW_SOURCE_KINDS,
    SOURCE_FILE_AVAILABLE,
    TASK_SOURCE_CLEANUP_KINDS,
)
from backend.storage.base import StorageBackend, StorageObjectNotFound


logger = logging.getLogger(__name__)
MAX_ORIGINAL_NAME_LENGTH = 512


def _bounded_original_name(value: str) -> str:
    name = str(value or "upload.bin")
    if len(name) <= MAX_ORIGINAL_NAME_LENGTH:
        return name
    digest = hashlib.sha256(name.encode("utf-8", errors="replace")).hexdigest()[:16]
    suffix = Path(name).suffix[:20]
    prefix_length = MAX_ORIGINAL_NAME_LENGTH - len(suffix) - len(digest) - 1
    return f"{name[:prefix_length]}~{digest}{suffix}"


@dataclass(frozen=True)
class StoredFile:
    id: str
    owner_id: str
    kind: str
    original_name: str
    storage_backend: str
    storage_key: str
    content_type: str | None
    size_bytes: int
    sha256: str
    created_at: float
    source_quota_owner_id: str | None = None
    source_quota_bytes: int = 0
    availability_status: str = "available"
    availability_reason: str | None = None
    lifecycle_revision: int = 0
    cleanup_operation_id: str | None = None
    cleanup_final_result_version: int | None = None
    cleanup_requested_at: float | None = None
    cleanup_last_attempt_at: float | None = None
    cleanup_attempt_count: int = 0
    cleanup_claim_token: str | None = None
    cleanup_claimed_at: float | None = None
    replacement_claim_group_id: str | None = None
    replacement_claim_expires_at: float | None = None
    replacement_group_id: str | None = None
    unavailable_at: float | None = None
    assignment_id: str | None = None
    submission_revision_id: str | None = None
    knowledge_document_id: str | None = None


def _record_to_dto(record: StoredFileRecord) -> StoredFile:
    return StoredFile(
        id=record.id,
        owner_id=record.owner_id,
        kind=record.kind,
        original_name=record.original_name,
        storage_backend=record.storage_backend,
        storage_key=record.storage_key,
        content_type=record.content_type,
        size_bytes=record.size_bytes,
        sha256=record.sha256,
        created_at=record.created_at,
        source_quota_owner_id=record.source_quota_owner_id,
        source_quota_bytes=record.source_quota_bytes,
        availability_status=record.availability_status,
        availability_reason=record.availability_reason,
        lifecycle_revision=record.lifecycle_revision,
        cleanup_operation_id=record.cleanup_operation_id,
        cleanup_final_result_version=record.cleanup_final_result_version,
        cleanup_requested_at=record.cleanup_requested_at,
        cleanup_last_attempt_at=record.cleanup_last_attempt_at,
        cleanup_attempt_count=record.cleanup_attempt_count,
        cleanup_claim_token=record.cleanup_claim_token,
        cleanup_claimed_at=record.cleanup_claimed_at,
        replacement_claim_group_id=record.replacement_claim_group_id,
        replacement_claim_expires_at=record.replacement_claim_expires_at,
        replacement_group_id=record.replacement_group_id,
        unavailable_at=record.unavailable_at,
        assignment_id=record.assignment_id,
        submission_revision_id=record.submission_revision_id,
        knowledge_document_id=record.knowledge_document_id,
    )


def save_file(*, storage: StorageBackend, owner_id: str, kind: str,
              original_name: str, content: bytes, content_type: str | None = None,
              storage_prefix: str | None = None, assignment_id: str | None = None,
              submission_revision_id: str | None = None,
              knowledge_document_id: str | None = None,
              fence_operation_id: str | None = None,
              fence_operation_attempt: int | None = None,
              fence_lease_token: str | None = None,
              replacement_file_ids: tuple[str, ...] = (),
              replacement_group_id: str | None = None) -> StoredFile:
    if (fence_operation_id is None) != (fence_operation_attempt is None):
        raise ValueError("Operation artifact fence requires id and attempt.")
    if fence_lease_token is not None and fence_operation_id is None:
        raise ValueError("Operation artifact lease requires an operation fence.")
    if fence_operation_id is not None and assignment_id is None:
        raise ValueError("Operation artifact fence requires assignment_id.")
    file_id = uuid.uuid4().hex
    bounded_original_name = _bounded_original_name(original_name)
    safe_name = Path(bounded_original_name).name or "upload.bin"
    # Prefix reflects the business link so object-storage listings stay organized.
    if storage_prefix:
        prefix = storage_prefix
    elif submission_revision_id:
        prefix = f"revisions/{submission_revision_id}"
    elif assignment_id:
        prefix = f"assignments/{assignment_id}"
    else:
        prefix = f"users/{owner_id}/files"
    key = f"{prefix}/{file_id}/{safe_name}"
    digest = hashlib.sha256(content).hexdigest()
    if kind in RAW_SOURCE_KINDS:
        return _save_quota_managed_source(
            storage=storage,
            file_id=file_id,
            owner_id=owner_id,
            kind=kind,
            original_name=bounded_original_name,
            storage_key=key,
            content=content,
            content_type=content_type,
            digest=digest,
            assignment_id=assignment_id,
            submission_revision_id=submission_revision_id,
            knowledge_document_id=knowledge_document_id,
            fence_operation_id=fence_operation_id,
            fence_operation_attempt=fence_operation_attempt,
            fence_lease_token=fence_lease_token,
            replacement_file_ids=replacement_file_ids,
            replacement_group_id=replacement_group_id,
        )
    if replacement_file_ids or replacement_group_id is not None:
        raise ValueError("Only task originals support replacement quota credit.")
    if assignment_id is not None and knowledge_document_id is None:
        if submission_revision_id is not None:
            raise ValueError(
                "A task artifact cannot link an assignment and revision together."
            )
        return _save_task_artifact_with_intent(
            storage=storage,
            file_id=file_id,
            owner_id=owner_id,
            kind=kind,
            original_name=bounded_original_name,
            storage_key=key,
            content=content,
            content_type=content_type,
            digest=digest,
            assignment_id=assignment_id,
            fence_operation_id=fence_operation_id,
            fence_operation_attempt=fence_operation_attempt,
            fence_lease_token=fence_lease_token,
        )
    storage.save(key, content)
    record = StoredFile(
        id=file_id, owner_id=owner_id, kind=kind, original_name=bounded_original_name,
        storage_backend=getattr(storage, "name", "unknown"), storage_key=key,
        content_type=content_type, size_bytes=len(content), sha256=digest,
        created_at=time.time(), assignment_id=assignment_id,
        submission_revision_id=submission_revision_id,
        knowledge_document_id=knowledge_document_id,
    )
    try:
        with session_scope() as session:
            if fence_operation_id is not None:
                # Import lazily to keep the normalized file-model module free
                # of a module-initialization cycle with workflow_repository.
                from backend.db.workflow_repository import (
                    AssignmentWorkflowRecord,
                    WorkflowOperationRecord,
                )

                checked_at = time.time()
                fence_conditions = [
                    WorkflowOperationRecord.id == fence_operation_id,
                    WorkflowOperationRecord.owner_id == owner_id,
                    WorkflowOperationRecord.assignment_id == assignment_id,
                    WorkflowOperationRecord.attempt == fence_operation_attempt,
                ]
                if fence_lease_token is None:
                    # HTTP preflights create a durable preparing operation
                    # before the external object write.  This keeps task
                    # deletion from completing its prefix scan while the
                    # upload is in flight, without inventing a worker lease.
                    fence_conditions.extend((
                        WorkflowOperationRecord.status == "preparing",
                        WorkflowOperationRecord.expires_at.is_not(None),
                        WorkflowOperationRecord.expires_at > checked_at,
                    ))
                else:
                    fence_conditions.extend((
                        WorkflowOperationRecord.status == "running",
                        WorkflowOperationRecord.lease_token
                        == fence_lease_token,
                        WorkflowOperationRecord.lease_expires_at.is_not(None),
                        WorkflowOperationRecord.lease_expires_at > checked_at,
                    ))
                fenced = session.execute(
                    update(WorkflowOperationRecord)
                    .where(*fence_conditions)
                    # A matched UPDATE both validates and locks the operation
                    # row until the StoredFile insert commits. Reclaim cannot
                    # rotate the lease between the fence and publication.
                    .values(updated_at=WorkflowOperationRecord.updated_at)
                )
                if fenced.rowcount != 1:
                    raise LeaseLost(
                        "Operation lease lost before artifact publication."
                    )
                operation = session.scalar(select(WorkflowOperationRecord).where(
                    WorkflowOperationRecord.id == fence_operation_id,
                    WorkflowOperationRecord.owner_id == owner_id,
                    WorkflowOperationRecord.assignment_id == assignment_id,
                ))
                if (
                    operation is None
                    or operation.attempt != fence_operation_attempt
                    or (
                        fence_lease_token is None
                        and (
                            operation.status != "preparing"
                            or operation.expires_at is None
                            or operation.expires_at <= time.time()
                        )
                    )
                    or (
                        fence_lease_token is not None
                        and (
                            operation.status != "running"
                            or operation.lease_token != fence_lease_token
                            or operation.lease_expires_at is None
                            or operation.lease_expires_at <= time.time()
                        )
                    )
                ):
                    raise LeaseLost(
                        "Operation lease lost before artifact publication."
                    )
                # O -> W -> A is the canonical producer publication order.
                # Task deletion owns W -> A, so once its tombstone commits a
                # stale producer can no longer publish task metadata.
                session.scalar(
                    select(AssignmentWorkflowRecord)
                    .where(
                        AssignmentWorkflowRecord.assignment_id == assignment_id,
                        AssignmentWorkflowRecord.owner_id == owner_id,
                    )
                    .with_for_update()
                )
                live_assignment = session.scalar(
                    select(AssignmentRecord)
                    .where(
                        AssignmentRecord.id == assignment_id,
                        AssignmentRecord.teacher_id == owner_id,
                        AssignmentRecord.deletion_requested_at.is_(None),
                    )
                    .with_for_update()
                )
                if live_assignment is None:
                    raise LeaseLost(
                        "Task deletion fenced artifact publication.",
                        code="task_deleted",
                    )
            elif assignment_id is not None:
                # Non-worker setup paths still fail closed once the tombstone
                # exists. Long-running external writes must use the operation
                # fence above so deletion also waits for their in-flight span.
                from backend.db.workflow_repository import AssignmentWorkflowRecord

                session.scalar(
                    select(AssignmentWorkflowRecord)
                    .where(
                        AssignmentWorkflowRecord.assignment_id == assignment_id,
                        AssignmentWorkflowRecord.owner_id == owner_id,
                    )
                    .with_for_update()
                )
                live_assignment = session.scalar(
                    select(AssignmentRecord)
                    .where(
                        AssignmentRecord.id == assignment_id,
                        AssignmentRecord.teacher_id == owner_id,
                        AssignmentRecord.deletion_requested_at.is_(None),
                    )
                    .with_for_update()
                )
                if live_assignment is None:
                    raise NotFound("assignment")
            session.add(StoredFileRecord(**record.__dict__))
    except Exception:
        persisted: StoredFile | None = None
        verification_completed = False
        try:
            with session_scope() as session:
                existing = session.scalar(
                    select(StoredFileRecord).where(
                        StoredFileRecord.id == file_id,
                        StoredFileRecord.owner_id == owner_id,
                    )
                )
                persisted = _record_to_dto(existing) if existing is not None else None
                verification_completed = True
        except Exception:
            logger.warning(
                "Stored file commit state could not be verified; file_id=%s",
                file_id,
            )
        if persisted is not None and (
            persisted.storage_key == record.storage_key
            and persisted.sha256 == record.sha256
            and persisted.size_bytes == record.size_bytes
        ):
            return persisted
        if verification_completed and persisted is None:
            try:
                storage.delete(key)
            except Exception:
                logger.warning(
                    "Uncommitted storage object cleanup failed; file_id=%s",
                    file_id,
                )
        raise
    return record


def _verify_source_object(
    *, storage: StorageBackend, storage_key: str, expected_size: int,
    expected_sha256: str,
) -> None:
    actual_size = 0
    digest = hashlib.sha256()
    try:
        with storage.open(storage_key) as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if chunk in (b"", None):
                    break
                if not isinstance(chunk, bytes):
                    raise SourceStorageIntegrityFailed(
                        "Stored source bytes could not be verified."
                    )
                actual_size += len(chunk)
                digest.update(chunk)
    except SourceStorageIntegrityFailed:
        raise
    except Exception as exc:
        raise SourceStorageIntegrityFailed(
            "Stored source bytes could not be verified."
        ) from exc
    if actual_size != expected_size or digest.hexdigest() != expected_sha256:
        raise SourceStorageIntegrityFailed(
            "Stored source bytes failed integrity verification."
        )


def _cleanup_failed_source_reservation(
    *, storage: StorageBackend, reservation_id: str, storage_key: str,
    error_code: str,
) -> None:
    from backend.db import source_storage_repository

    try:
        storage.delete(storage_key)
    except StorageObjectNotFound:
        pass
    except Exception:
        try:
            source_storage_repository.retain_source_reservation_for_cleanup(
                reservation_id, error_code=error_code
            )
        except Exception:
            # The reservation and delayed collector were committed before any
            # object write. If this bookkeeping update is temporarily
            # unavailable, their original expiry still makes cleanup recoverable.
            logger.warning(
                "Source reservation cleanup state could not be updated; file_id=%s",
                reservation_id,
            )
        return
    try:
        source_storage_repository.release_source_reservation(reservation_id)
    except Exception:
        # Deletion is already confirmed. Leaving the durable charge in place is
        # conservative; its collector will observe a missing object and release
        # the reservation after database service recovers.
        logger.warning(
            "Deleted source reservation could not yet be released; file_id=%s",
            reservation_id,
        )


def _save_quota_managed_source(
    *,
    storage: StorageBackend,
    file_id: str,
    owner_id: str,
    kind: str,
    original_name: str,
    storage_key: str,
    content: bytes,
    content_type: str | None,
    digest: str,
    assignment_id: str | None,
    submission_revision_id: str | None,
    knowledge_document_id: str | None,
    fence_operation_id: str | None,
    fence_operation_attempt: int | None,
    fence_lease_token: str | None,
    replacement_file_ids: tuple[str, ...],
    replacement_group_id: str | None,
) -> StoredFile:
    from backend.db import source_storage_repository

    if knowledge_document_id is not None:
        raise ValueError("Task originals cannot be linked to a knowledge document.")
    reservation = source_storage_repository.reserve_source_upload(
        file_id=file_id,
        file_owner_id=owner_id,
        kind=kind,
        original_name=original_name,
        storage_backend=getattr(storage, "name", "unknown"),
        storage_key=storage_key,
        content_type=content_type,
        requested_bytes=len(content),
        sha256=digest,
        assignment_id=assignment_id,
        submission_revision_id=submission_revision_id,
        replacement_file_ids=replacement_file_ids,
        replacement_group_id=replacement_group_id,
    )
    try:
        storage.save(storage_key, content)
    except Exception as exc:
        _cleanup_failed_source_reservation(
            storage=storage,
            reservation_id=reservation.id,
            storage_key=storage_key,
            error_code="source_storage_write_failed",
        )
        raise SourceStorageWriteFailed(
            "Task-original storage did not confirm the write."
        ) from exc
    try:
        source_storage_repository.mark_reservation_object_written(reservation.id)
        _verify_source_object(
            storage=storage,
            storage_key=storage_key,
            expected_size=len(content),
            expected_sha256=digest,
        )
        row = source_storage_repository.publish_source_reservation(
            reservation_id=reservation.id,
            assignment_id=assignment_id,
            submission_revision_id=submission_revision_id,
            knowledge_document_id=None,
            fence_operation_id=fence_operation_id,
            fence_operation_attempt=fence_operation_attempt,
            fence_lease_token=fence_lease_token,
        )
        return _record_to_dto(row)
    except SourceStorageIntegrityFailed:
        _cleanup_failed_source_reservation(
            storage=storage,
            reservation_id=reservation.id,
            storage_key=storage_key,
            error_code="source_storage_integrity_failed",
        )
        raise
    except Exception:
        # A commit response can be lost after the StoredFile row commits. Read
        # it back before deleting the object, preserving idempotent publication.
        persisted: StoredFile | None = None
        verification_completed = False
        try:
            persisted = get_file(file_id=file_id, owner_id=owner_id)
            verification_completed = True
        except Exception:
            logger.warning(
                "Source file commit state could not be verified; file_id=%s",
                file_id,
            )
        if (
            persisted is not None
            and persisted.storage_key == storage_key
            and persisted.sha256 == digest
            and persisted.size_bytes == len(content)
        ):
            return persisted
        if verification_completed and persisted is None:
            _cleanup_failed_source_reservation(
                storage=storage,
                reservation_id=reservation.id,
                storage_key=storage_key,
                error_code="source_storage_write_failed",
            )
        raise


def _save_task_artifact_with_intent(
    *,
    storage: StorageBackend,
    file_id: str,
    owner_id: str,
    kind: str,
    original_name: str,
    storage_key: str,
    content: bytes,
    content_type: str | None,
    digest: str,
    assignment_id: str,
    fence_operation_id: str | None,
    fence_operation_attempt: int | None,
    fence_lease_token: str | None,
) -> StoredFile:
    """Persist a derived task object behind a restart-safe exact-key intent."""

    from backend.db import source_storage_repository

    reservation = source_storage_repository.reserve_task_artifact_write(
        file_id=file_id,
        owner_id=owner_id,
        assignment_id=assignment_id,
        kind=kind,
        original_name=original_name,
        storage_backend=getattr(storage, "name", "unknown"),
        storage_key=storage_key,
        content_type=content_type,
        requested_bytes=len(content),
        sha256=digest,
    )
    try:
        storage.save(storage_key, content)
    except Exception as exc:
        _cleanup_failed_source_reservation(
            storage=storage,
            reservation_id=reservation.id,
            storage_key=storage_key,
            error_code="artifact_storage_write_failed",
        )
        raise SourceStorageWriteFailed(
            "Task artifact storage did not confirm the write."
        ) from exc
    try:
        source_storage_repository.mark_reservation_object_written(reservation.id)
        _verify_source_object(
            storage=storage,
            storage_key=storage_key,
            expected_size=len(content),
            expected_sha256=digest,
        )
        row = source_storage_repository.publish_task_artifact_reservation(
            reservation_id=reservation.id,
            assignment_id=assignment_id,
            fence_operation_id=fence_operation_id,
            fence_operation_attempt=fence_operation_attempt,
            fence_lease_token=fence_lease_token,
        )
        return _record_to_dto(row)
    except SourceStorageIntegrityFailed:
        _cleanup_failed_source_reservation(
            storage=storage,
            reservation_id=reservation.id,
            storage_key=storage_key,
            error_code="artifact_storage_integrity_failed",
        )
        raise
    except Exception:
        persisted: StoredFile | None = None
        verification_completed = False
        try:
            persisted = get_file(file_id=file_id, owner_id=owner_id)
            verification_completed = True
        except Exception:
            logger.warning(
                "Task artifact commit state could not be verified; file_id=%s",
                file_id,
            )
        if (
            persisted is not None
            and persisted.storage_key == storage_key
            and persisted.sha256 == digest
            and persisted.size_bytes == len(content)
        ):
            return persisted
        if verification_completed and persisted is None:
            _cleanup_failed_source_reservation(
                storage=storage,
                reservation_id=reservation.id,
                storage_key=storage_key,
                error_code="artifact_storage_write_failed",
            )
        raise
def archive_member_reference_content(
    *,
    container_sha256: str,
    member_name: str,
) -> bytes:
    """Return a small deterministic pointer to a preserved archive member."""
    return json.dumps(
        {
            "container_sha256": container_sha256,
            "member_name": _bounded_original_name(member_name),
            "schema": "smartai.archive-member-reference.v1",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def create_archive_member_reference(
    *,
    storage: StorageBackend,
    source_file_id: str,
    owner_id: str,
    assignment_id: str,
    member_name: str,
    fence_operation_id: str | None = None,
    fence_operation_attempt: int | None = None,
    fence_lease_token: str | None = None,
) -> StoredFile:
    """Persist a bounded pointer for one member of a durable archive.

    The full archive is already durable and checkpoint-referenced.  A small
    independent object avoids duplicating a potentially 100 MB archive for
    every failed member while giving each logical source its own StoredFile row.
    """
    with session_scope() as session:
        source = session.scalar(
            select(StoredFileRecord).where(
                StoredFileRecord.id == source_file_id,
                StoredFileRecord.owner_id == owner_id,
                StoredFileRecord.assignment_id == assignment_id,
            )
        )
        if source is None:
            raise ValueError("source_file_not_found")
        container_name = source.original_name
        container_sha256 = source.sha256
    content = archive_member_reference_content(
        container_sha256=container_sha256,
        member_name=member_name,
    )
    storage_prefix = f"assignments/{assignment_id}/submission-source-references"
    if fence_operation_id is not None and fence_operation_attempt is not None:
        storage_prefix = (
            f"{storage_prefix}/{fence_operation_id}/{fence_operation_attempt}"
        )
    return save_file(
        storage=storage,
        owner_id=owner_id,
        kind="submission_archive_member_reference",
        original_name=f"{container_name} :: {member_name}",
        content=content,
        content_type="application/vnd.smartai.archive-member-reference+json",
        storage_prefix=storage_prefix,
        assignment_id=assignment_id,
        fence_operation_id=fence_operation_id,
        fence_operation_attempt=fence_operation_attempt,
        fence_lease_token=fence_lease_token,
    )


def delete_unlinked_file(
    *,
    storage: StorageBackend,
    file_id: str,
    owner_id: str,
    assignment_id: str,
    delete_object: bool = True,
    fence_operation_id: str | None = None,
    fence_operation_attempt: int | None = None,
    fence_lease_token: str | None = None,
) -> bool:
    """Remove an unlinked row without losing track of physical source bytes.

    Quota-managed originals atomically move their charge to a durable orphan
    reservation before metadata deletion. If the immediate storage delete
    fails, the existing worker retries it and the bytes remain accounted.
    """
    stored: StoredFile | None = None
    staged_reservation_id: str | None = None
    try:
        with session_scope() as session:
            if fence_operation_id is not None:
                if fence_operation_attempt is None:
                    return False
                from backend.db.source_outcome_repository import (
                    _lock_source_write_operation,
                )

                _lock_source_write_operation(
                    session,
                    owner_id=owner_id,
                    assignment_id=assignment_id,
                    operation_id=fence_operation_id,
                    expected_attempt=fence_operation_attempt,
                    expected_lease_token=fence_lease_token,
                )
            from backend.db.workflow_repository import (
                AssignmentWorkflowRecord,
                WorkflowOperationRecord,
            )

            # Compensation may insert an operation/reservation with an
            # assignment FK. Lock workflow -> assignment before the file so a
            # concurrent task cascade cannot form Assignment -> File versus
            # File -> Assignment.
            session.scalar(
                select(AssignmentWorkflowRecord)
                .where(
                    AssignmentWorkflowRecord.assignment_id == assignment_id,
                    AssignmentWorkflowRecord.owner_id == owner_id,
                )
                .with_for_update()
            )
            assignment = session.scalar(
                select(AssignmentRecord.id)
                .where(AssignmentRecord.id == assignment_id)
                .with_for_update()
            )
            if assignment is None:
                return False
            record = session.scalar(
                select(StoredFileRecord).where(
                    StoredFileRecord.id == file_id,
                    StoredFileRecord.owner_id == owner_id,
                    StoredFileRecord.assignment_id == assignment_id,
                ).with_for_update()
            )
            if record is None:
                return False
            operation_artifact_refs = session.scalars(
                select(WorkflowOperationRecord.artifact_refs).where(
                    WorkflowOperationRecord.owner_id == owner_id,
                    WorkflowOperationRecord.assignment_id == assignment_id,
                )
            ).all()
            if any(
                file_id in refs
                for refs in operation_artifact_refs
                if isinstance(refs, list)
            ):
                return False
            stored = _record_to_dto(record)
            if (
                record.kind in TASK_SOURCE_CLEANUP_KINDS
                and record.source_quota_owner_id is not None
            ):
                operation_id = f"op_{uuid.uuid4().hex}"
                now = time.time()
                operation = WorkflowOperationRecord(
                    id=operation_id,
                    assignment_id=assignment_id,
                    owner_id=str(record.source_quota_owner_id),
                    operation_type="source_reservation_cleanup",
                    input_hash=hashlib.sha256(
                        f"source-orphan:{record.id}".encode("utf-8")
                    ).hexdigest(),
                    attempt=1,
                    status="pending",
                    payload={"reservation_id": record.id, "schema": 1},
                    progress={"state": "cleanup_pending", "retry_count": 0},
                    expires_at=now,
                    created_at=now,
                    updated_at=now,
                )
                reservation = SourceStorageReservationRecord(
                    id=record.id,
                    operation_id=operation_id,
                    file_owner_id=record.owner_id,
                    quota_owner_id=str(record.source_quota_owner_id),
                    assignment_id=assignment_id,
                    submission_revision_id=record.submission_revision_id,
                    source_lifecycle_epoch=0,
                    kind=record.kind,
                    original_name=record.original_name,
                    storage_backend=record.storage_backend,
                    storage_key=record.storage_key,
                    content_type=record.content_type,
                    requested_bytes=record.source_quota_bytes,
                    sha256=record.sha256,
                    replacement_group_id=record.replacement_group_id,
                    replacement_credit_bytes=record.source_quota_bytes,
                    purpose="orphan_cleanup",
                    state="cleanup_pending",
                    expires_at=now,
                    created_at=now,
                    updated_at=now,
                )
                session.add(operation)
                session.flush([operation])
                session.add(reservation)
                staged_reservation_id = record.id
            session.delete(record)
            session.flush()
    except Exception:
        # A committed workflow source may now reference the row. In that case
        # preserving the object is safer than guessing whether the commit won.
        return False
    if delete_object and stored is not None:
        try:
            storage.delete(stored.storage_key)
        except Exception:
            logger.warning(
                "Unlinked storage object cleanup failed; file_id=%s",
                file_id,
            )
            if staged_reservation_id is not None:
                from backend.db import source_storage_repository

                source_storage_repository.retain_source_reservation_for_cleanup(
                    staged_reservation_id,
                    error_code="source_storage_delete_failed",
                )
        else:
            if staged_reservation_id is not None:
                from backend.db import source_storage_repository

                source_storage_repository.release_source_reservation(
                    staged_reservation_id
                )
    return True


def list_files(*, owner_id: str, assignment_id: str | None = None,
               submission_revision_id: str | None = None) -> list[StoredFile]:
    with session_scope() as session:
        stmt = select(StoredFileRecord).where(StoredFileRecord.owner_id == owner_id)
        if assignment_id is not None:
            stmt = stmt.where(StoredFileRecord.assignment_id == assignment_id)
        if submission_revision_id is not None:
            stmt = stmt.where(StoredFileRecord.submission_revision_id == submission_revision_id)
        records = list(session.scalars(stmt))
    return [_record_to_dto(r) for r in records]


def get_file(*, file_id: str, owner_id: str) -> StoredFile | None:
    with session_scope() as session:
        record = session.get(StoredFileRecord, file_id)
        if record is None or record.owner_id != owner_id:
            return None
        return _record_to_dto(record)


def find_unlinked_source_file(
    *,
    owner_id: str,
    assignment_id: str,
    kind: str,
    sha256: str,
    storage_prefix: str,
) -> StoredFile | None:
    """Recover a published operation source after a pre-registration crash.

    The prefix contains the exact operation id and attempt. A source already
    linked at any logical position is deliberately excluded so equal files at
    two positions never collide with the per-operation file uniqueness rule.
    """
    from backend.db.source_outcome_repository import WorkflowSourceItemRecord

    exact_prefix = storage_prefix.rstrip("/") + "/"
    with session_scope() as session:
        candidates = list(session.scalars(
            select(StoredFileRecord)
            .where(
                StoredFileRecord.owner_id == owner_id,
                StoredFileRecord.assignment_id == assignment_id,
                StoredFileRecord.kind == kind,
                StoredFileRecord.sha256 == sha256,
                StoredFileRecord.availability_status == SOURCE_FILE_AVAILABLE,
                ~exists(select(WorkflowSourceItemRecord.id).where(
                    WorkflowSourceItemRecord.stored_file_id
                    == StoredFileRecord.id
                )),
            )
            .order_by(StoredFileRecord.created_at, StoredFileRecord.id)
        ))
        for candidate in candidates:
            if candidate.storage_key.startswith(exact_prefix):
                return _record_to_dto(candidate)
    return None


def delete_files_for_revision(*, storage: StorageBackend, submission_revision_id: str,
                              owner_id: str) -> int:
    files = list_files(owner_id=owner_id, submission_revision_id=submission_revision_id)
    if any(file.kind in TASK_SOURCE_CLEANUP_KINDS for file in files):
        raise InvalidTransition(
            "Task originals require durable lifecycle cleanup.",
            code="source_storage_lifecycle_required",
        )
    with session_scope() as session:
        session.execute(delete(StoredFileRecord).where(
            StoredFileRecord.submission_revision_id == submission_revision_id,
            StoredFileRecord.owner_id == owner_id,
        ))
    for file in files:
        storage.delete(file.storage_key)
    return len(files)


def delete_files_for_assignment(*, storage: StorageBackend, assignment_id: str,
                                owner_id: str) -> int:
    files = list_files(owner_id=owner_id, assignment_id=assignment_id)
    if any(file.kind in TASK_SOURCE_CLEANUP_KINDS for file in files):
        raise InvalidTransition(
            "Task originals require durable lifecycle cleanup.",
            code="source_storage_lifecycle_required",
        )
    with session_scope() as session:
        session.execute(delete(StoredFileRecord).where(
            StoredFileRecord.assignment_id == assignment_id,
            StoredFileRecord.owner_id == owner_id,
        ))
    for file in files:
        storage.delete(file.storage_key)
    return len(files)


def delete_file_record(*, file_id: str, owner_id: str) -> bool:
    with session_scope() as session:
        record = session.scalar(
            select(StoredFileRecord)
            .where(
                StoredFileRecord.id == file_id,
                StoredFileRecord.owner_id == owner_id,
            )
            .with_for_update()
        )
        if record is None:
            return False
        if record.kind in TASK_SOURCE_CLEANUP_KINDS:
            raise InvalidTransition(
                "Task originals require durable lifecycle cleanup.",
                code="source_storage_lifecycle_required",
            )
        session.delete(record)
        return True
