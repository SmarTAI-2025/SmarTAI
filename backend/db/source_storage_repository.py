"""Atomic quota reservations and truthful raw-source lifecycle transitions."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.config import settings
from backend.db.models import (
    AssignmentRecord,
    GradingRunRecord,
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
    SourceStorageQuotaExceeded,
    SourceStorageReservationConflict,
    ValidationError,
)
from backend.domain.source_storage import (
    ASSIGNMENT_LINKED_RAW_SOURCE_KINDS,
    DELAYED_SOURCE_OPERATION_TYPES,
    RAW_SOURCE_KINDS,
    REVISION_LINKED_RAW_SOURCE_KINDS,
    SOURCE_CLEANUP_OPERATION,
    SOURCE_FILE_AVAILABLE,
    SOURCE_FILE_CLEANUP_PENDING,
    SOURCE_FILE_UNAVAILABLE,
    SOURCE_REASON_MISSING,
    SOURCE_REASON_STORAGE_DELETE_FAILED,
    SOURCE_REASON_TASK_FINALIZED,
    SOURCE_RESERVATION_CLEANUP_OPERATION,
)


@dataclass(frozen=True)
class SourceQuotaUsage:
    used_bytes: int
    limit_bytes: int
    available_source_bytes: int
    cleanup_pending_bytes: int
    retrying_cleanup_bytes: int
    reserved_bytes: int
    cleanup_pending_count: int
    retrying_cleanup_count: int

    def as_dict(self) -> dict[str, int]:
        return {
            "used_bytes": self.used_bytes,
            "limit_bytes": self.limit_bytes,
            "available_bytes": max(0, self.limit_bytes - self.used_bytes),
            "available_source_bytes": self.available_source_bytes,
            "cleanup_pending_bytes": self.cleanup_pending_bytes,
            "retrying_cleanup_bytes": self.retrying_cleanup_bytes,
            "reserved_bytes": self.reserved_bytes,
            "cleanup_pending_count": self.cleanup_pending_count,
            "retrying_cleanup_count": self.retrying_cleanup_count,
        }


@dataclass(frozen=True)
class SourceReservation:
    id: str
    operation_id: str
    file_owner_id: str
    quota_owner_id: str
    assignment_id: str
    submission_revision_id: str | None
    source_lifecycle_epoch: int
    kind: str
    original_name: str
    storage_backend: str
    storage_key: str
    content_type: str | None
    requested_bytes: int
    sha256: str
    expires_at: float


@dataclass(frozen=True)
class CleanupDeleteResult:
    status: str
    file_id: str
    size_bytes: int


@dataclass(frozen=True)
class StorageDeleteClaim:
    status: str
    file_id: str
    size_bytes: int
    storage_backend: str | None = None
    storage_key: str | None = None
    claim_token: str | None = None


def _new_operation_id() -> str:
    return f"op_{uuid.uuid4().hex}"


def _quota_limit() -> int:
    return max(0, int(settings.unfinished_source_quota_bytes))


def _retry_delay(retry_count: int) -> int:
    base = max(1, int(settings.source_cleanup_retry_base_seconds))
    maximum = max(base, int(settings.source_cleanup_retry_max_seconds))
    return min(maximum, base * (2 ** min(max(0, retry_count), 16)))


def _lock_quota_owner(session: Session, owner_id: str) -> None:
    # PostgreSQL obtains a row lock; SQLite obtains its database write lock at
    # this first UPDATE. Every raw-source reservation and lifecycle release uses
    # the same lock before calculating usage.
    result = session.execute(
        update(UserRecord)
        .where(UserRecord.id == owner_id)
        .values(updated_at=UserRecord.updated_at)
    )
    if result.rowcount != 1:
        raise NotFound("source_quota_owner")


def _lock_source_epoch(
    session: Session,
    *,
    assignment_id: str,
    owner_id: str,
    allow_finalized: bool = False,
) -> int:
    from backend.db.workflow_repository import AssignmentWorkflowRecord

    workflow = session.scalar(
        select(AssignmentWorkflowRecord)
        .where(
            AssignmentWorkflowRecord.assignment_id == assignment_id,
            AssignmentWorkflowRecord.owner_id == owner_id,
        )
        .with_for_update()
    )
    # Normalized legacy routes can persist originals without a presentation
    # workflow. Their epoch stays at zero; task-centric finalization always has
    # a workflow and therefore receives the upload/finalization fence.
    if workflow is None:
        return 0
    if not allow_finalized and workflow.presentation_status == "finalized":
        raise InvalidTransition(
            "The task is already finalized and no longer accepts originals.",
            code="source_storage_task_finalized",
        )
    return int(workflow.source_lifecycle_epoch)


def _task_revision_ids(assignment_id: str):
    return (
        select(SubmissionRevisionRecord.id)
        .join(
            SubmissionRecord,
            SubmissionRecord.id == SubmissionRevisionRecord.submission_id,
        )
        .where(SubmissionRecord.assignment_id == assignment_id)
    )


def _belongs_to_assignment(assignment_id: str):
    return or_(
        and_(
            StoredFileRecord.assignment_id == assignment_id,
            StoredFileRecord.kind.in_(tuple(ASSIGNMENT_LINKED_RAW_SOURCE_KINDS)),
        ),
        and_(
            StoredFileRecord.kind.in_(tuple(REVISION_LINKED_RAW_SOURCE_KINDS)),
            StoredFileRecord.submission_revision_id.in_(
                _task_revision_ids(assignment_id)
            ),
        ),
    )


def _resolve_source_context(
    session: Session,
    *,
    file_owner_id: str,
    kind: str,
    assignment_id: str | None,
    submission_revision_id: str | None,
) -> tuple[str, str]:
    if kind not in RAW_SOURCE_KINDS:
        raise ValidationError(
            "The file kind is not a quota-managed task original.",
            code="invalid_source_storage_kind",
        )
    if kind in ASSIGNMENT_LINKED_RAW_SOURCE_KINDS:
        if assignment_id is None or submission_revision_id is not None:
            raise ValidationError(
                "An assignment-linked source requires exactly one assignment.",
                code="invalid_source_storage_link",
            )
        assignment = session.get(AssignmentRecord, assignment_id)
        if assignment is None:
            raise NotFound("assignment")
        # Current task APIs persist teacher-owned originals. Keeping this strict
        # prevents one teacher from charging another teacher's allocation.
        if assignment.teacher_id != file_owner_id:
            raise ValidationError(
                "The source owner does not own the assignment.",
                code="invalid_source_storage_owner",
            )
        return assignment.id, assignment.teacher_id

    if submission_revision_id is None or assignment_id is not None:
        raise ValidationError(
            "A revision-linked source requires exactly one submission revision.",
            code="invalid_source_storage_link",
        )
    row = session.execute(
        select(
            SubmissionRecord.assignment_id,
            AssignmentRecord.teacher_id,
            SubmissionRecord.student_id,
        )
        .join(
            SubmissionRevisionRecord,
            SubmissionRevisionRecord.submission_id == SubmissionRecord.id,
        )
        .join(AssignmentRecord, AssignmentRecord.id == SubmissionRecord.assignment_id)
        .where(SubmissionRevisionRecord.id == submission_revision_id)
    ).one_or_none()
    if row is None:
        raise NotFound("submission_revision")
    resolved_assignment_id, teacher_id, student_id = row
    if file_owner_id not in {student_id, teacher_id}:
        raise ValidationError(
            "The source owner is outside the submission assignment.",
            code="invalid_source_storage_owner",
        )
    return str(resolved_assignment_id), str(teacher_id)


def _usage_in_session(session: Session, owner_id: str) -> SourceQuotaUsage:
    stored = session.execute(
        select(
            func.coalesce(func.sum(StoredFileRecord.source_quota_bytes), 0),
            func.coalesce(func.sum(
                case(
                    (StoredFileRecord.availability_status == SOURCE_FILE_AVAILABLE,
                     StoredFileRecord.source_quota_bytes),
                    else_=0,
                )
            ), 0),
            func.coalesce(func.sum(
                case(
                    (StoredFileRecord.availability_status == SOURCE_FILE_CLEANUP_PENDING,
                     StoredFileRecord.source_quota_bytes),
                    else_=0,
                )
            ), 0),
            func.coalesce(func.sum(
                case(
                    (and_(
                        StoredFileRecord.availability_status
                        == SOURCE_FILE_CLEANUP_PENDING,
                        StoredFileRecord.availability_reason
                        == SOURCE_REASON_STORAGE_DELETE_FAILED,
                    ), StoredFileRecord.source_quota_bytes),
                    else_=0,
                )
            ), 0),
            func.coalesce(func.sum(
                case(
                    (StoredFileRecord.availability_status
                     == SOURCE_FILE_CLEANUP_PENDING, 1),
                    else_=0,
                )
            ), 0),
            func.coalesce(func.sum(
                case(
                    (and_(
                        StoredFileRecord.availability_status
                        == SOURCE_FILE_CLEANUP_PENDING,
                        StoredFileRecord.availability_reason
                        == SOURCE_REASON_STORAGE_DELETE_FAILED,
                    ), 1),
                    else_=0,
                )
            ), 0),
        ).where(
            StoredFileRecord.source_quota_owner_id == owner_id,
            StoredFileRecord.availability_status != SOURCE_FILE_UNAVAILABLE,
        )
    ).one()
    reserved = int(session.scalar(
        select(func.coalesce(func.sum(SourceStorageReservationRecord.requested_bytes), 0))
        .where(SourceStorageReservationRecord.quota_owner_id == owner_id)
    ) or 0)
    stored_total = int(stored[0] or 0)
    return SourceQuotaUsage(
        used_bytes=stored_total + reserved,
        limit_bytes=_quota_limit(),
        available_source_bytes=int(stored[1] or 0),
        cleanup_pending_bytes=int(stored[2] or 0),
        retrying_cleanup_bytes=int(stored[3] or 0),
        reserved_bytes=reserved,
        cleanup_pending_count=int(stored[4] or 0),
        retrying_cleanup_count=int(stored[5] or 0),
    )


def source_quota_usage(owner_id: str) -> SourceQuotaUsage:
    with session_scope() as session:
        return _usage_in_session(session, owner_id)


def assert_assignment_storage_empty_in_session(
    session: Session,
    *,
    assignment_id: str,
) -> None:
    """Fail closed before an assignment cascade can orphan stored objects."""
    revision_ids = _task_revision_ids(assignment_id)
    live_file_id = session.scalar(
        select(StoredFileRecord.id)
        .where(
            StoredFileRecord.availability_status != SOURCE_FILE_UNAVAILABLE,
            or_(
                StoredFileRecord.assignment_id == assignment_id,
                StoredFileRecord.submission_revision_id.in_(revision_ids),
            ),
        )
        .limit(1)
        .with_for_update()
    )
    reservation_id = session.scalar(
        select(SourceStorageReservationRecord.id)
        .where(
            SourceStorageReservationRecord.assignment_id == assignment_id,
        )
        .limit(1)
        .with_for_update()
    )
    if live_file_id is not None or reservation_id is not None:
        raise InvalidTransition(
            "Task files are still stored or awaiting automatic cleanup.",
            code="task_storage_cleanup_required",
        )


def reserve_source_upload(
    *,
    file_id: str,
    file_owner_id: str,
    kind: str,
    original_name: str,
    storage_backend: str,
    storage_key: str,
    content_type: str | None,
    requested_bytes: int,
    sha256: str,
    assignment_id: str | None,
    submission_revision_id: str | None,
) -> SourceReservation:
    if requested_bytes < 0:
        raise ValidationError(
            "Source size cannot be negative.", code="invalid_source_storage_size"
        )
    now = time.time()
    ttl = max(1, int(settings.source_storage_reservation_ttl_seconds))
    try:
        with session_scope() as session:
            resolved_assignment_id, quota_owner_id = _resolve_source_context(
                session,
                file_owner_id=file_owner_id,
                kind=kind,
                assignment_id=assignment_id,
                submission_revision_id=submission_revision_id,
            )
            source_lifecycle_epoch = _lock_source_epoch(
                session,
                assignment_id=resolved_assignment_id,
                owner_id=quota_owner_id,
            )
            _lock_quota_owner(session, quota_owner_id)
            usage = _usage_in_session(session, quota_owner_id)
            if usage.used_bytes + requested_bytes > usage.limit_bytes:
                raise SourceStorageQuotaExceeded(
                    "The task-original storage allocation is full.",
                    details={
                        **usage.as_dict(),
                        "requested_bytes": requested_bytes,
                        "next_step": "wait_for_automatic_cleanup_or_reduce_upload",
                    },
                )

            from backend.db.workflow_repository import WorkflowOperationRecord

            operation_id = _new_operation_id()
            expires_at = now + ttl
            operation = WorkflowOperationRecord(
                id=operation_id,
                assignment_id=resolved_assignment_id,
                owner_id=quota_owner_id,
                operation_type=SOURCE_RESERVATION_CLEANUP_OPERATION,
                input_hash=hashlib.sha256(
                    f"source-reservation:{file_id}".encode("utf-8")
                ).hexdigest(),
                attempt=1,
                status="pending",
                payload={"reservation_id": file_id, "schema": 1},
                progress={"state": "waiting_for_upload"},
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
            reservation = SourceStorageReservationRecord(
                id=file_id,
                operation_id=operation_id,
                file_owner_id=file_owner_id,
                quota_owner_id=quota_owner_id,
                assignment_id=resolved_assignment_id,
                submission_revision_id=submission_revision_id,
                source_lifecycle_epoch=source_lifecycle_epoch,
                kind=kind,
                original_name=original_name,
                storage_backend=storage_backend,
                storage_key=storage_key,
                content_type=content_type,
                requested_bytes=requested_bytes,
                sha256=sha256,
                purpose="upload",
                state="reserved",
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            # There is intentionally no ORM relationship between the generic
            # operation model and the reservation model. Insert the FK parent
            # explicitly before the reservation on SQLite as well as Postgres.
            session.flush([operation])
            session.add(reservation)
            session.flush()
            return SourceReservation(
                id=reservation.id,
                operation_id=operation_id,
                file_owner_id=file_owner_id,
                quota_owner_id=quota_owner_id,
                assignment_id=resolved_assignment_id,
                submission_revision_id=submission_revision_id,
                source_lifecycle_epoch=source_lifecycle_epoch,
                kind=kind,
                original_name=original_name,
                storage_backend=storage_backend,
                storage_key=storage_key,
                content_type=content_type,
                requested_bytes=requested_bytes,
                sha256=sha256,
                expires_at=expires_at,
            )
    except SourceStorageQuotaExceeded:
        raise
    except OperationalError as exc:
        raise SourceStorageReservationConflict(
            "The source allocation is busy; retry the upload shortly."
        ) from exc


def mark_reservation_object_written(reservation_id: str) -> None:
    from backend.db.workflow_repository import WorkflowOperationRecord

    with session_scope() as session:
        hint = session.get(SourceStorageReservationRecord, reservation_id)
        if hint is None:
            raise SourceStorageReservationConflict(
                "The source reservation is no longer active."
            )
        collector = session.scalar(
            select(WorkflowOperationRecord)
            .where(WorkflowOperationRecord.id == hint.operation_id)
            .with_for_update()
        )
        reservation = session.scalar(
            select(SourceStorageReservationRecord)
            .where(
                SourceStorageReservationRecord.id == reservation_id,
                SourceStorageReservationRecord.operation_id == hint.operation_id,
            )
            .with_for_update()
        )
        if (
            collector is None
            or collector.status != "pending"
            or collector.lease_owner is not None
            or reservation is None
            or reservation.state != "reserved"
        ):
            raise SourceStorageReservationConflict(
                "The source reservation is no longer active."
            )
        reservation.state = "object_written"
        reservation.updated_at = time.time()


def _validate_publication_fence(
    session: Session,
    *,
    owner_id: str,
    assignment_id: str,
    operation_id: str | None,
    operation_attempt: int | None,
    lease_token: str | None,
) -> None:
    values = (operation_id, operation_attempt, lease_token)
    if not any(value is not None for value in values):
        return
    if not all(value is not None for value in values):
        raise ValueError("Operation artifact fence must be provided in full.")
    from backend.db.workflow_repository import WorkflowOperationRecord

    checked_at = time.time()
    fenced = session.execute(
        update(WorkflowOperationRecord)
        .where(
            WorkflowOperationRecord.id == operation_id,
            WorkflowOperationRecord.owner_id == owner_id,
            WorkflowOperationRecord.assignment_id == assignment_id,
            WorkflowOperationRecord.attempt == operation_attempt,
            WorkflowOperationRecord.status == "running",
            WorkflowOperationRecord.lease_token == lease_token,
            WorkflowOperationRecord.lease_expires_at.is_not(None),
            WorkflowOperationRecord.lease_expires_at > checked_at,
        )
        .values(updated_at=WorkflowOperationRecord.updated_at)
    )
    if fenced.rowcount != 1:
        raise LeaseLost("Operation lease lost before artifact publication.")
    operation = session.scalar(select(WorkflowOperationRecord).where(
        WorkflowOperationRecord.id == operation_id,
        WorkflowOperationRecord.owner_id == owner_id,
        WorkflowOperationRecord.assignment_id == assignment_id,
    ))
    if (
        operation is None
        or operation.attempt != operation_attempt
        or operation.status != "running"
        or operation.lease_token != lease_token
        or operation.lease_expires_at is None
        or operation.lease_expires_at <= time.time()
    ):
        raise LeaseLost("Operation lease lost before artifact publication.")


def publish_source_reservation(
    *,
    reservation_id: str,
    assignment_id: str | None,
    submission_revision_id: str | None,
    knowledge_document_id: str | None,
    fence_operation_id: str | None = None,
    fence_operation_attempt: int | None = None,
    fence_lease_token: str | None = None,
) -> StoredFileRecord:
    from backend.db.workflow_repository import WorkflowOperationRecord

    stale_generation = False
    published: StoredFileRecord | None = None
    with session_scope() as session:
        hint = session.get(SourceStorageReservationRecord, reservation_id)
        if hint is None:
            raise SourceStorageReservationConflict(
                "The source reservation cannot be published."
            )
        # Keep the producer-operation -> workflow lock order used by
        # supersede/failure/commit paths.  The no-op UPDATE holds the producer
        # lease row through this transaction, so neither lease rotation nor a
        # superseding operation can race between validation and publication.
        # The unlocked reservation read is only an identity hint; the same row
        # and business links are locked and revalidated below before insertion.
        _validate_publication_fence(
            session,
            owner_id=hint.quota_owner_id,
            assignment_id=hint.assignment_id,
            operation_id=fence_operation_id,
            operation_attempt=fence_operation_attempt,
            lease_token=fence_lease_token,
        )
        current_epoch = _lock_source_epoch(
            session,
            assignment_id=hint.assignment_id,
            owner_id=hint.quota_owner_id,
            allow_finalized=True,
        )
        collector = session.scalar(
            select(WorkflowOperationRecord)
            .where(WorkflowOperationRecord.id == hint.operation_id)
            .with_for_update()
        )
        reservation = session.scalar(
            select(SourceStorageReservationRecord)
            .where(
                SourceStorageReservationRecord.id == reservation_id,
                SourceStorageReservationRecord.operation_id == hint.operation_id,
            )
            .with_for_update()
        )
        if reservation is None or reservation.state != "object_written":
            raise SourceStorageReservationConflict(
                "The source reservation cannot be published."
            )
        if (
            collector is None
            or collector.status != "pending"
            or collector.lease_owner is not None
        ):
            raise SourceStorageReservationConflict(
                "The source reservation is already being reconciled."
            )
        assignment_link_valid = (
            reservation.kind in ASSIGNMENT_LINKED_RAW_SOURCE_KINDS
            and assignment_id == reservation.assignment_id
            and submission_revision_id is None
        )
        revision_link_valid = (
            reservation.kind in REVISION_LINKED_RAW_SOURCE_KINDS
            and assignment_id is None
            and submission_revision_id == reservation.submission_revision_id
        )
        if not (assignment_link_valid or revision_link_valid):
            raise SourceStorageReservationConflict(
                "The source reservation business link changed."
            )
        if knowledge_document_id is not None:
            raise SourceStorageReservationConflict(
                "A task original cannot become a knowledge document."
            )
        if current_epoch != reservation.source_lifecycle_epoch:
            now = time.time()
            reservation.state = "cleanup_pending"
            reservation.error_code = "source_storage_generation_superseded"
            reservation.retry_count += 1
            reservation.expires_at = now
            reservation.updated_at = now
            collector.status = "pending"
            collector.progress = {
                "state": "cleanup_pending",
                "retry_count": reservation.retry_count,
            }
            collector.error_code = "source_storage_generation_superseded"
            collector.expires_at = now
            collector.updated_at = now
            stale_generation = True
        else:
            _lock_quota_owner(session, reservation.quota_owner_id)
            published = StoredFileRecord(
                id=reservation.id,
                owner_id=reservation.file_owner_id,
                kind=reservation.kind,
                original_name=reservation.original_name,
                storage_backend=reservation.storage_backend,
                storage_key=reservation.storage_key,
                content_type=reservation.content_type,
                size_bytes=reservation.requested_bytes,
                sha256=reservation.sha256,
                source_quota_owner_id=reservation.quota_owner_id,
                source_quota_bytes=reservation.requested_bytes,
                availability_status=SOURCE_FILE_AVAILABLE,
                availability_reason=None,
                lifecycle_revision=0,
                cleanup_attempt_count=0,
                assignment_id=assignment_id,
                submission_revision_id=submission_revision_id,
                knowledge_document_id=None,
                created_at=time.time(),
            )
            session.add(published)
            session.flush()
            session.delete(reservation)
            session.delete(collector)
            session.flush()
    if stale_generation:
        raise SourceStorageReservationConflict(
            "The task finalized before the source could be published."
        )
    assert published is not None
    return published


def release_source_reservation(reservation_id: str) -> bool:
    """Release only after its object was confirmed deleted."""
    from backend.db.workflow_repository import WorkflowOperationRecord

    with session_scope() as session:
        hint = session.get(SourceStorageReservationRecord, reservation_id)
        if hint is None:
            return False
        operation = session.scalar(
            select(WorkflowOperationRecord)
            .where(WorkflowOperationRecord.id == hint.operation_id)
            .with_for_update()
        )
        reservation = session.scalar(
            select(SourceStorageReservationRecord)
            .where(
                SourceStorageReservationRecord.id == reservation_id,
                SourceStorageReservationRecord.operation_id == hint.operation_id,
            )
            .with_for_update()
        )
        if reservation is None:
            return False
        if operation is not None and operation.lease_owner is not None:
            return False
        _lock_quota_owner(session, reservation.quota_owner_id)
        session.delete(reservation)
        if operation is not None:
            session.delete(operation)
        return True


def retain_source_reservation_for_cleanup(
    reservation_id: str, *, error_code: str
) -> None:
    now = time.time()
    from backend.db.workflow_repository import WorkflowOperationRecord

    with session_scope() as session:
        hint = session.get(SourceStorageReservationRecord, reservation_id)
        if hint is None:
            return
        operation = session.scalar(
            select(WorkflowOperationRecord)
            .where(WorkflowOperationRecord.id == hint.operation_id)
            .with_for_update()
        )
        reservation = session.scalar(
            select(SourceStorageReservationRecord)
            .where(
                SourceStorageReservationRecord.id == reservation_id,
                SourceStorageReservationRecord.operation_id == hint.operation_id,
            )
            .with_for_update()
        )
        if reservation is None:
            return
        reservation.state = "cleanup_pending"
        reservation.error_code = error_code
        reservation.retry_count += 1
        reservation.updated_at = now
        reservation.expires_at = now + _retry_delay(reservation.retry_count)
        if operation is not None and operation.lease_owner is None:
            operation.status = "pending"
            operation.error_code = error_code
            operation.expires_at = reservation.expires_at
            operation.updated_at = now


def _lock_live_source_operation(
    session: Session,
    *,
    operation_id: str,
    operation_type: str,
    owner_id: str,
    assignment_id: str,
    attempt: int,
    worker_id: str,
    lease_token: str,
):
    from backend.db.workflow_repository import WorkflowOperationRecord

    checked_at = time.time()
    fenced = session.execute(
        update(WorkflowOperationRecord)
        .where(
            WorkflowOperationRecord.id == operation_id,
            WorkflowOperationRecord.owner_id == owner_id,
            WorkflowOperationRecord.assignment_id == assignment_id,
            WorkflowOperationRecord.operation_type == operation_type,
            WorkflowOperationRecord.attempt == attempt,
            WorkflowOperationRecord.status == "running",
            WorkflowOperationRecord.lease_owner == worker_id,
            WorkflowOperationRecord.lease_token == lease_token,
            WorkflowOperationRecord.lease_expires_at >= checked_at,
        )
        .values(updated_at=WorkflowOperationRecord.updated_at)
    )
    if fenced.rowcount != 1:
        raise LeaseLost("Source cleanup lease was lost.")
    operation = session.scalar(select(WorkflowOperationRecord).where(
        WorkflowOperationRecord.id == operation_id,
        WorkflowOperationRecord.owner_id == owner_id,
        WorkflowOperationRecord.assignment_id == assignment_id,
    ))
    if (
        operation is None
        or operation.operation_type != operation_type
        or operation.attempt != attempt
        or operation.status != "running"
        or operation.lease_owner != worker_id
        or operation.lease_token != lease_token
        or operation.lease_expires_at is None
        or operation.lease_expires_at < time.time()
    ):
        raise LeaseLost("Source cleanup lease was lost.")
    return operation


def claim_reserved_object_delete(
    *,
    operation_id: str,
    owner_id: str,
    assignment_id: str,
    attempt: int,
    worker_id: str,
    lease_token: str,
    reservation_id: str,
) -> StorageDeleteClaim:
    """Claim one orphan in a short transaction before external storage I/O."""
    with session_scope() as session:
        _lock_live_source_operation(
            session,
            operation_id=operation_id,
            operation_type=SOURCE_RESERVATION_CLEANUP_OPERATION,
            owner_id=owner_id,
            assignment_id=assignment_id,
            attempt=attempt,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        reservation = session.scalar(
            select(SourceStorageReservationRecord)
            .where(
                SourceStorageReservationRecord.id == reservation_id,
                SourceStorageReservationRecord.operation_id == operation_id,
                SourceStorageReservationRecord.quota_owner_id == owner_id,
                SourceStorageReservationRecord.assignment_id == assignment_id,
            )
            .with_for_update()
        )
        if reservation is None:
            return StorageDeleteClaim("already_deleted", reservation_id, 0)
        claim_token = uuid.uuid4().hex
        reservation.cleanup_claim_token = claim_token
        reservation.cleanup_claimed_at = time.time()
        reservation.updated_at = reservation.cleanup_claimed_at
        return StorageDeleteClaim(
            "ready",
            reservation_id,
            reservation.requested_bytes,
            storage_backend=reservation.storage_backend,
            storage_key=reservation.storage_key,
            claim_token=claim_token,
        )


def finish_reserved_object_delete(
    *,
    operation_id: str,
    owner_id: str,
    assignment_id: str,
    attempt: int,
    worker_id: str,
    lease_token: str,
    reservation_id: str,
    claim_token: str,
    deleted: bool,
    error_code: str = "source_storage_delete_failed",
) -> CleanupDeleteResult:
    """Commit delete truth only while the same operation lease is still live."""
    now = time.time()
    with session_scope() as session:
        _lock_live_source_operation(
            session,
            operation_id=operation_id,
            operation_type=SOURCE_RESERVATION_CLEANUP_OPERATION,
            owner_id=owner_id,
            assignment_id=assignment_id,
            attempt=attempt,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        reservation = session.scalar(
            select(SourceStorageReservationRecord)
            .where(
                SourceStorageReservationRecord.id == reservation_id,
                SourceStorageReservationRecord.operation_id == operation_id,
                SourceStorageReservationRecord.quota_owner_id == owner_id,
                SourceStorageReservationRecord.assignment_id == assignment_id,
                SourceStorageReservationRecord.cleanup_claim_token == claim_token,
            )
            .with_for_update()
        )
        if reservation is None:
            return CleanupDeleteResult("already_deleted", reservation_id, 0)
        size = reservation.requested_bytes
        if not deleted:
            reservation.state = "cleanup_pending"
            reservation.error_code = error_code
            reservation.retry_count += 1
            reservation.cleanup_claim_token = None
            reservation.cleanup_claimed_at = None
            reservation.updated_at = now
            return CleanupDeleteResult("failed", reservation_id, size)
        _lock_quota_owner(session, reservation.quota_owner_id)
        session.delete(reservation)
        return CleanupDeleteResult("deleted", reservation_id, size)


def enqueue_finalized_source_cleanup_in_session(
    session: Session,
    *,
    assignment_id: str,
    owner_id: str,
    grading_run_id: str,
    final_result_version: int,
    finalized_at: float,
) -> str | None:
    """Snapshot and mark task originals in the finalization transaction."""
    from backend.db.workflow_repository import WorkflowOperationRecord

    existing = list(session.scalars(
        select(WorkflowOperationRecord).where(
            WorkflowOperationRecord.assignment_id == assignment_id,
            WorkflowOperationRecord.owner_id == owner_id,
            WorkflowOperationRecord.operation_type == SOURCE_CLEANUP_OPERATION,
        )
    ))
    matching: list[Any] = []
    for operation in existing:
        payload = operation.payload if isinstance(operation.payload, dict) else {}
        if (
            payload.get("grading_run_id") == grading_run_id
            and payload.get("final_result_version") == final_result_version
            and payload.get("finalized_at") == finalized_at
        ):
            matching.append(operation)

    candidates = list(session.scalars(
        select(StoredFileRecord)
        .where(
            StoredFileRecord.source_quota_owner_id == owner_id,
            StoredFileRecord.kind.in_(tuple(RAW_SOURCE_KINDS)),
            StoredFileRecord.availability_status.in_((
                SOURCE_FILE_AVAILABLE,
                SOURCE_FILE_CLEANUP_PENDING,
            )),
            _belongs_to_assignment(assignment_id),
        )
        .order_by(StoredFileRecord.id.asc())
        .with_for_update()
    ))
    active_match = next(
        (
            operation
            for operation in matching
            if operation.status in {"pending", "running"}
        ),
        None,
    )
    if active_match is not None and all(
        row.availability_status == SOURCE_FILE_CLEANUP_PENDING
        and row.cleanup_operation_id == active_match.id
        and row.cleanup_final_result_version == final_result_version
        for row in candidates
    ):
        return active_match.id
    if not candidates:
        completed_match = next(
            (operation for operation in matching if operation.status == "completed"),
            None,
        )
        return completed_match.id if completed_match is not None else None

    now = time.time()
    operation_id = _new_operation_id()
    entries: list[dict[str, Any]] = []
    for row in candidates:
        row.availability_status = SOURCE_FILE_CLEANUP_PENDING
        row.availability_reason = SOURCE_REASON_TASK_FINALIZED
        row.cleanup_operation_id = operation_id
        row.cleanup_final_result_version = final_result_version
        row.cleanup_requested_at = now
        row.cleanup_last_attempt_at = None
        row.cleanup_attempt_count = 0
        row.cleanup_claim_token = None
        row.cleanup_claimed_at = None
        row.unavailable_at = None
        row.lifecycle_revision += 1
        entries.append({
            "file_id": row.id,
            "kind": row.kind,
            "sha256": row.sha256,
            "size_bytes": row.source_quota_bytes,
            "lifecycle_revision": row.lifecycle_revision,
        })

    payload = {
        "schema": 1,
        "grading_run_id": grading_run_id,
        "final_result_version": final_result_version,
        "finalized_at": finalized_at,
        "files": entries,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > 4 * 1024 * 1024:
        raise ValidationError(
            "The source cleanup manifest exceeds its storage limit.",
            code="source_cleanup_manifest_too_large",
        )
    operation = WorkflowOperationRecord(
        id=operation_id,
        assignment_id=assignment_id,
        owner_id=owner_id,
        operation_type=SOURCE_CLEANUP_OPERATION,
        input_hash=hashlib.sha256(encoded).hexdigest(),
        attempt=1,
        status="pending",
        payload=payload,
        progress={
            "state": "pending",
            "total": len(entries),
            "deleted": 0,
            "failed": 0,
            "retry_count": 0,
        },
        expires_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(operation)
    session.flush([operation])
    # Persist lifecycle rows only after the operation FK parent exists.
    session.flush()
    return operation_id


def _cleanup_generation_authorized(
    session: Session,
    *,
    assignment_id: str,
    owner_id: str,
    grading_run_id: str,
    final_result_version: int,
    finalized_at: float,
) -> bool:
    from backend.db.workflow_repository import AssignmentWorkflowRecord

    workflow = session.scalar(
        select(AssignmentWorkflowRecord)
        .where(
            AssignmentWorkflowRecord.assignment_id == assignment_id,
            AssignmentWorkflowRecord.owner_id == owner_id,
        )
        .with_for_update()
    )
    if workflow is None:
        return False
    if (
        workflow.presentation_status != "finalized"
        or workflow.final_result_version != final_result_version
        or workflow.final_result_updated_at != finalized_at
        or workflow.analysis_status == "stale"
        or workflow.grading_job_id != grading_run_id
    ):
        return False
    run = session.scalar(select(GradingRunRecord).where(
        GradingRunRecord.id == grading_run_id,
        GradingRunRecord.assignment_id == assignment_id,
        GradingRunRecord.teacher_id == owner_id,
        GradingRunRecord.released_at == finalized_at,
    ))
    return run is not None


def cleanup_generation_is_authorized(
    *,
    assignment_id: str,
    owner_id: str,
    grading_run_id: str,
    final_result_version: int,
    finalized_at: float,
) -> bool:
    with session_scope() as session:
        return _cleanup_generation_authorized(
            session,
            assignment_id=assignment_id,
            owner_id=owner_id,
            grading_run_id=grading_run_id,
            final_result_version=final_result_version,
            finalized_at=finalized_at,
        )


def restore_unclaimed_sources_for_superseded_cleanup(
    *,
    operation_id: str,
    owner_id: str,
    assignment_id: str,
    operation_attempt: int,
    worker_id: str,
    lease_token: str,
) -> int:
    """Return untouched files to available when a cleanup generation is stale.

    A review can supersede a finalized workflow before its cleanup worker starts,
    or between two manifest entries.  Only rows that still belong to this exact
    operation and have never crossed the external-delete attempt boundary are
    safe to restore.  Attempted rows are deliberately left for physical-truth
    reconciliation, and rows reassigned to a newer cleanup are never touched.
    """
    with session_scope() as session:
        _lock_live_source_operation(
            session,
            operation_id=operation_id,
            operation_type=SOURCE_CLEANUP_OPERATION,
            owner_id=owner_id,
            assignment_id=assignment_id,
            attempt=operation_attempt,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        rows = list(session.scalars(
            select(StoredFileRecord)
            .where(
                StoredFileRecord.source_quota_owner_id == owner_id,
                StoredFileRecord.kind.in_(tuple(RAW_SOURCE_KINDS)),
                StoredFileRecord.availability_status
                == SOURCE_FILE_CLEANUP_PENDING,
                StoredFileRecord.cleanup_operation_id == operation_id,
                StoredFileRecord.cleanup_claim_token.is_(None),
                StoredFileRecord.cleanup_attempt_count == 0,
                _belongs_to_assignment(assignment_id),
            )
            .order_by(StoredFileRecord.id.asc())
            .with_for_update()
        ))
        for row in rows:
            row.availability_status = SOURCE_FILE_AVAILABLE
            row.availability_reason = None
            row.cleanup_operation_id = None
            row.cleanup_final_result_version = None
            row.cleanup_requested_at = None
            row.cleanup_last_attempt_at = None
            row.cleanup_attempt_count = 0
            row.cleanup_claim_token = None
            row.cleanup_claimed_at = None
            row.unavailable_at = None
            row.lifecycle_revision += 1
        return len(rows)


def cleanup_reconciliation_file_ids(
    *,
    operation_id: str,
    owner_id: str,
    assignment_id: str,
    operation_attempt: int,
    worker_id: str,
    lease_token: str,
) -> frozenset[str]:
    """Return files whose delete outcome can no longer be assumed untouched."""
    with session_scope() as session:
        _lock_live_source_operation(
            session,
            operation_id=operation_id,
            operation_type=SOURCE_CLEANUP_OPERATION,
            owner_id=owner_id,
            assignment_id=assignment_id,
            attempt=operation_attempt,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        return frozenset(str(file_id) for file_id in session.scalars(
            select(StoredFileRecord.id).where(
                StoredFileRecord.source_quota_owner_id == owner_id,
                StoredFileRecord.kind.in_(tuple(RAW_SOURCE_KINDS)),
                StoredFileRecord.availability_status
                == SOURCE_FILE_CLEANUP_PENDING,
                StoredFileRecord.cleanup_operation_id == operation_id,
                or_(
                    StoredFileRecord.cleanup_claim_token.is_not(None),
                    StoredFileRecord.cleanup_attempt_count > 0,
                ),
                _belongs_to_assignment(assignment_id),
            )
        ))


def _cleanup_entry_matches(
    row: StoredFileRecord,
    *,
    operation_id: str,
    final_result_version: int,
    entry: dict[str, Any],
) -> bool:
    lifecycle_revision = entry.get("lifecycle_revision")
    return (
        row.kind == entry.get("kind")
        and row.sha256 == entry.get("sha256")
        and row.source_quota_bytes == entry.get("size_bytes")
        and isinstance(lifecycle_revision, int)
        and not isinstance(lifecycle_revision, bool)
        and row.lifecycle_revision >= lifecycle_revision
        and row.cleanup_operation_id == operation_id
        and row.cleanup_final_result_version == final_result_version
    )


def claim_finalized_source_delete(
    *,
    operation_id: str,
    owner_id: str,
    assignment_id: str,
    operation_attempt: int,
    worker_id: str,
    lease_token: str,
    grading_run_id: str,
    final_result_version: int,
    finalized_at: float,
    entry: dict[str, Any],
) -> StorageDeleteClaim:
    """Claim an exact finalized source without holding locks during storage I/O."""
    file_id = str(entry.get("file_id") or "")
    now = time.time()
    with session_scope() as session:
        _lock_live_source_operation(
            session,
            operation_id=operation_id,
            operation_type=SOURCE_CLEANUP_OPERATION,
            owner_id=owner_id,
            assignment_id=assignment_id,
            attempt=operation_attempt,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        generation_authorized = _cleanup_generation_authorized(
            session,
            assignment_id=assignment_id,
            owner_id=owner_id,
            grading_run_id=grading_run_id,
            final_result_version=final_result_version,
            finalized_at=finalized_at,
        )
        row = session.scalar(
            select(StoredFileRecord)
            .where(
                StoredFileRecord.id == file_id,
                StoredFileRecord.source_quota_owner_id == owner_id,
                StoredFileRecord.kind.in_(tuple(RAW_SOURCE_KINDS)),
                _belongs_to_assignment(assignment_id),
            )
            .with_for_update()
        )
        if row is None:
            return StorageDeleteClaim("missing_record", file_id, 0)
        if row.availability_status == SOURCE_FILE_UNAVAILABLE:
            return StorageDeleteClaim("already_deleted", file_id, 0)
        if (
            row.availability_status != SOURCE_FILE_CLEANUP_PENDING
            or not _cleanup_entry_matches(
                row,
                operation_id=operation_id,
                final_result_version=final_result_version,
                entry=entry,
            )
        ):
            return StorageDeleteClaim("superseded", file_id, 0)
        # A stale finalization may not begin deleting another untouched file.
        # Once an exact row has crossed the external-delete attempt boundary,
        # however, the physical outcome is ambiguous after a crash or provider
        # exception.  A replacement lease must keep issuing idempotent deletes
        # until absence is confirmed and the durable quota row can be settled.
        if (
            not generation_authorized
            and row.cleanup_claim_token is None
            and row.cleanup_attempt_count == 0
        ):
            return StorageDeleteClaim("superseded", file_id, 0)
        claim_token = uuid.uuid4().hex
        row.cleanup_claim_token = claim_token
        row.cleanup_claimed_at = now
        row.cleanup_last_attempt_at = now
        row.cleanup_attempt_count += 1
        return StorageDeleteClaim(
            "ready",
            file_id,
            row.source_quota_bytes,
            storage_backend=row.storage_backend,
            storage_key=row.storage_key,
            claim_token=claim_token,
        )


def finish_finalized_source_delete(
    *,
    operation_id: str,
    owner_id: str,
    assignment_id: str,
    operation_attempt: int,
    worker_id: str,
    lease_token: str,
    grading_run_id: str,
    final_result_version: int,
    finalized_at: float,
    entry: dict[str, Any],
    claim_token: str,
    deleted: bool,
) -> CleanupDeleteResult:
    """Record delete truth only while the same worker lease and claim are live."""
    file_id = str(entry.get("file_id") or "")
    now = time.time()
    with session_scope() as session:
        _lock_live_source_operation(
            session,
            operation_id=operation_id,
            operation_type=SOURCE_CLEANUP_OPERATION,
            owner_id=owner_id,
            assignment_id=assignment_id,
            attempt=operation_attempt,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        # The mutable workflow generation was fully authorized immediately
        # before this exact row received its claim token. Storage I/O has now
        # already happened outside the transaction. Record that physical truth
        # using the immutable manifest identity + claim token even if a review
        # changed the workflow meanwhile; rejecting the finish would leave a
        # deleted object permanently charged. The next row must still pass the
        # full current-generation authorization in its own claim.
        row = session.scalar(
            select(StoredFileRecord)
            .where(
                StoredFileRecord.id == file_id,
                StoredFileRecord.source_quota_owner_id == owner_id,
                StoredFileRecord.kind.in_(tuple(RAW_SOURCE_KINDS)),
                _belongs_to_assignment(assignment_id),
            )
            .with_for_update()
        )
        if row is None:
            return CleanupDeleteResult("missing_record", file_id, 0)
        if row.availability_status == SOURCE_FILE_UNAVAILABLE:
            return CleanupDeleteResult("already_deleted", file_id, 0)
        if (
            row.availability_status != SOURCE_FILE_CLEANUP_PENDING
            or row.cleanup_claim_token != claim_token
            or not _cleanup_entry_matches(
                row,
                operation_id=operation_id,
                final_result_version=final_result_version,
                entry=entry,
            )
        ):
            return CleanupDeleteResult("superseded", file_id, 0)
        size = row.source_quota_bytes
        row.cleanup_claim_token = None
        row.cleanup_claimed_at = None
        if not deleted:
            row.availability_reason = SOURCE_REASON_STORAGE_DELETE_FAILED
            row.lifecycle_revision += 1
            return CleanupDeleteResult("failed", file_id, size)

        _lock_quota_owner(session, owner_id)
        row.availability_status = SOURCE_FILE_UNAVAILABLE
        row.availability_reason = SOURCE_REASON_TASK_FINALIZED
        row.unavailable_at = now
        row.lifecycle_revision += 1
        return CleanupDeleteResult("deleted", file_id, size)


def mark_available_source_missing(
    *, file_id: str, owner_id: str, assignment_id: str
) -> bool:
    now = time.time()
    with session_scope() as session:
        row = session.scalar(
            select(StoredFileRecord)
            .where(
                StoredFileRecord.id == file_id,
                StoredFileRecord.owner_id == owner_id,
                StoredFileRecord.availability_status == SOURCE_FILE_AVAILABLE,
                _belongs_to_assignment(assignment_id),
            )
            .with_for_update()
        )
        if row is None:
            return False
        if row.source_quota_owner_id is not None:
            _lock_quota_owner(session, row.source_quota_owner_id)
        row.availability_status = SOURCE_FILE_UNAVAILABLE
        row.availability_reason = SOURCE_REASON_MISSING
        row.unavailable_at = now
        row.lifecycle_revision += 1
        return True


def cleanup_summary(*, assignment_id: str, owner_id: str) -> dict[str, Any]:
    with session_scope() as session:
        rows = list(session.scalars(
            select(StoredFileRecord).where(
                StoredFileRecord.source_quota_owner_id == owner_id,
                StoredFileRecord.kind.in_(tuple(RAW_SOURCE_KINDS)),
                _belongs_to_assignment(assignment_id),
            )
        ))
    pending = [row for row in rows if row.availability_status == SOURCE_FILE_CLEANUP_PENDING]
    retrying = [
        row for row in pending
        if row.availability_reason == SOURCE_REASON_STORAGE_DELETE_FAILED
    ]
    deleted = [
        row for row in rows
        if row.availability_status == SOURCE_FILE_UNAVAILABLE
        and row.availability_reason == SOURCE_REASON_TASK_FINALIZED
    ]
    if retrying:
        status = "retrying"
    elif pending:
        status = "pending"
    elif deleted:
        status = "completed"
    else:
        status = "not_requested"
    return {
        "status": status,
        "total_count": len(rows),
        "deleted_count": len(deleted),
        "pending_count": len(pending),
        "retrying_count": len(retrying),
        "pending_bytes": sum(row.source_quota_bytes for row in pending),
        "retrying_bytes": sum(row.source_quota_bytes for row in retrying),
        "automatic_retry": bool(retrying),
    }


def delayed_source_operation_types() -> frozenset[str]:
    return DELAYED_SOURCE_OPERATION_TYPES
