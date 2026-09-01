"""Atomic quota and lifecycle repository for retained knowledge objects.

Lock order for every mutation is ``User -> ledger -> KnowledgeDocument ->
StoredFile``.  Callers that already hold task/workflow locks must preserve the
same relative order for these four records.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import fields, replace
from pathlib import PurePosixPath
from typing import Iterable

from sqlalchemy import and_, case, delete, exists, func, or_, select, update
from sqlalchemy.orm import Session

from backend.config import settings
from backend.db.models import (
    AssignmentKnowledgeDocumentRecord,
    AssignmentRecord,
    GradingRunRecord,
    KnowledgeDocumentRecord,
    KnowledgeStorageOrphanGuardRecord,
    KnowledgeStorageRecord,
    StoredFileRecord,
    UserRecord,
)
from backend.db.session import session_scope
from backend.domain.errors import (
    InvalidTransition,
    KnowledgeStorageQuotaExceeded,
    KnowledgeStorageReservationConflict,
    NotFound,
    ValidationError,
)
from backend.domain.knowledge_storage import (
    KNOWLEDGE_CLEANUP_EXPLICIT_DELETE,
    KNOWLEDGE_CLEANUP_REASONS,
    KNOWLEDGE_CLEANUP_STORAGE_DELETE_FAILED,
    KNOWLEDGE_CLEANUP_TASK_DELETED,
    KNOWLEDGE_CLEANUP_TASK_UNREFERENCED,
    KNOWLEDGE_CLEANUP_UPLOAD_ABANDONED,
    KNOWLEDGE_RETENTION_POLICIES,
    KNOWLEDGE_RETENTION_RETAINED,
    KNOWLEDGE_RETENTION_TASK_ONLY,
    KNOWLEDGE_STORAGE_AVAILABLE,
    KNOWLEDGE_STORAGE_CLEANUP_PENDING,
    KNOWLEDGE_STORAGE_RESERVED,
    KnowledgeCleanupClaim,
    KnowledgeCleanupRequest,
    KnowledgeCleanupResult,
    KnowledgeOrphanGuardClaim,
    KnowledgeStorageEntry,
    KnowledgeStorageUsage,
    KnowledgeUploadReservation,
)


def _entry(record: KnowledgeStorageRecord) -> KnowledgeStorageEntry:
    return KnowledgeStorageEntry(**{
        field.name: getattr(record, field.name)
        for field in fields(KnowledgeStorageEntry)
    })


def _quota_limit() -> int:
    return max(0, int(settings.knowledge_storage_quota_bytes))


def _retry_delay(attempt: int) -> int:
    base = max(1, int(settings.knowledge_cleanup_retry_base_seconds))
    maximum = max(base, int(settings.knowledge_cleanup_retry_max_seconds))
    return min(maximum, base * (2 ** min(max(0, attempt - 1), 16)))


def _safe_name(value: str) -> str:
    normalized = str(value or "knowledge.txt").replace("\\", "/")
    name = PurePosixPath(normalized).name or "knowledge.txt"
    name = "".join(
        character for character in name
        if ord(character) >= 32 and ord(character) != 127
    ).strip() or "knowledge.txt"
    return name[:512]


def _validate_digest(sha256: str) -> str:
    digest = sha256.strip().lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValidationError(
            "Knowledge content digest is invalid.",
            code="invalid_knowledge_storage_digest",
        )
    return digest


def _lock_owner(session: Session, owner_id: str) -> None:
    # PostgreSQL obtains the row lock; SQLite obtains its write lock at this
    # first UPDATE. All quota admission and release paths use this same gate.
    result = session.execute(
        update(UserRecord)
        .where(UserRecord.id == owner_id)
        .values(updated_at=UserRecord.updated_at)
    )
    if result.rowcount != 1:
        raise NotFound("knowledge_storage_owner")


def lock_knowledge_owner_in_session(session: Session, owner_id: str) -> None:
    """Acquire the quota gate before any assignment/document/file mutation.

    Integration repositories must call this immediately after opening their
    transaction, before selecting or writing assignment knowledge links.
    Calling it again from a lower-level hook is idempotent in that transaction.
    """
    _lock_owner(session, owner_id)


def _lock_entry_by_document(
    session: Session, *, document_id: str, owner_id: str,
) -> KnowledgeStorageRecord | None:
    return session.scalar(
        select(KnowledgeStorageRecord)
        .where(
            KnowledgeStorageRecord.document_id == document_id,
            KnowledgeStorageRecord.owner_id == owner_id,
        )
        .with_for_update()
    )


def _lock_linked_records(
    session: Session, record: KnowledgeStorageRecord,
) -> tuple[KnowledgeDocumentRecord | None, StoredFileRecord | None]:
    document = None
    stored = None
    if record.document_id is not None:
        document = session.scalar(
            select(KnowledgeDocumentRecord)
            .where(
                KnowledgeDocumentRecord.id == record.document_id,
                KnowledgeDocumentRecord.owner_id == record.owner_id,
            )
            .with_for_update()
        )
    if record.stored_file_id is not None:
        stored = session.scalar(
            select(StoredFileRecord)
            .where(
                StoredFileRecord.id == record.stored_file_id,
                StoredFileRecord.owner_id == record.owner_id,
            )
            .with_for_update()
        )
    return document, stored


def _validate_origin_assignment(
    session: Session, *, assignment_id: str, owner_id: str,
) -> AssignmentRecord:
    assignment = session.scalar(
        select(AssignmentRecord)
        .where(
            AssignmentRecord.id == assignment_id,
            AssignmentRecord.teacher_id == owner_id,
            AssignmentRecord.deletion_requested_at.is_(None),
        )
        .with_for_update()
    )
    if assignment is None:
        raise InvalidTransition(
            "The task no longer accepts knowledge uploads.",
            code="knowledge_storage_task_unavailable",
        )
    return assignment


def _usage_in_session(session: Session, owner_id: str) -> KnowledgeStorageUsage:
    row = session.execute(
        select(
            func.coalesce(func.sum(KnowledgeStorageRecord.size_bytes), 0),
            func.coalesce(func.sum(case(
                (KnowledgeStorageRecord.state == KNOWLEDGE_STORAGE_AVAILABLE,
                 KnowledgeStorageRecord.size_bytes), else_=0,
            )), 0),
            func.coalesce(func.sum(case(
                (KnowledgeStorageRecord.state == KNOWLEDGE_STORAGE_CLEANUP_PENDING,
                 KnowledgeStorageRecord.size_bytes), else_=0,
            )), 0),
            func.coalesce(func.sum(case(
                (and_(
                    KnowledgeStorageRecord.state
                    == KNOWLEDGE_STORAGE_CLEANUP_PENDING,
                    KnowledgeStorageRecord.cleanup_reason
                    == KNOWLEDGE_CLEANUP_STORAGE_DELETE_FAILED,
                ), KnowledgeStorageRecord.size_bytes), else_=0,
            )), 0),
            func.coalesce(func.sum(case(
                (KnowledgeStorageRecord.state == KNOWLEDGE_STORAGE_RESERVED,
                 KnowledgeStorageRecord.size_bytes), else_=0,
            )), 0),
            func.coalesce(func.sum(case(
                (KnowledgeStorageRecord.state == KNOWLEDGE_STORAGE_CLEANUP_PENDING, 1),
                else_=0,
            )), 0),
            func.coalesce(func.sum(case(
                (and_(
                    KnowledgeStorageRecord.state
                    == KNOWLEDGE_STORAGE_CLEANUP_PENDING,
                    KnowledgeStorageRecord.cleanup_reason
                    == KNOWLEDGE_CLEANUP_STORAGE_DELETE_FAILED,
                ), 1), else_=0,
            )), 0),
            func.coalesce(func.sum(case(
                (KnowledgeStorageRecord.state == KNOWLEDGE_STORAGE_RESERVED, 1),
                else_=0,
            )), 0),
        ).where(KnowledgeStorageRecord.owner_id == owner_id)
    ).one()
    return KnowledgeStorageUsage(
        used_bytes=int(row[0] or 0),
        limit_bytes=_quota_limit(),
        available_document_bytes=int(row[1] or 0),
        cleanup_pending_bytes=int(row[2] or 0),
        retrying_cleanup_bytes=int(row[3] or 0),
        reserved_bytes=int(row[4] or 0),
        cleanup_pending_count=int(row[5] or 0),
        retrying_cleanup_count=int(row[6] or 0),
        reserved_count=int(row[7] or 0),
    )


def knowledge_storage_usage(owner_id: str) -> KnowledgeStorageUsage:
    with session_scope() as session:
        return _usage_in_session(session, owner_id)


def get_document_storage(
    document_id: str, owner_id: str,
) -> KnowledgeStorageEntry | None:
    with session_scope() as session:
        record = session.scalar(select(KnowledgeStorageRecord).where(
            KnowledgeStorageRecord.document_id == document_id,
            KnowledgeStorageRecord.owner_id == owner_id,
        ))
        return _entry(record) if record is not None else None


def get_storage_entry(entry_id: str, owner_id: str) -> KnowledgeStorageEntry | None:
    with session_scope() as session:
        record = session.scalar(select(KnowledgeStorageRecord).where(
            KnowledgeStorageRecord.id == entry_id,
            KnowledgeStorageRecord.owner_id == owner_id,
        ))
        return _entry(record) if record is not None else None


def visible_document_ids(
    owner_id: str,
    *,
    include_task_only: bool,
    document_ids: Iterable[str] | None = None,
) -> tuple[str, ...]:
    requested = None if document_ids is None else tuple(dict.fromkeys(document_ids))
    if requested == ():
        return ()
    statement = select(KnowledgeStorageRecord.document_id).where(
        KnowledgeStorageRecord.owner_id == owner_id,
        KnowledgeStorageRecord.state == KNOWLEDGE_STORAGE_AVAILABLE,
        KnowledgeStorageRecord.document_id.is_not(None),
    )
    if not include_task_only:
        statement = statement.where(
            KnowledgeStorageRecord.retention_policy == KNOWLEDGE_RETENTION_RETAINED
        )
    if requested is not None:
        statement = statement.where(KnowledgeStorageRecord.document_id.in_(requested))
    with session_scope() as session:
        return tuple(str(value) for value in session.scalars(
            statement.order_by(KnowledgeStorageRecord.document_id.asc())
        ))


def fence_grading_knowledge_in_session(
    session: Session,
    *,
    owner_id: str,
    document_ids: Iterable[str],
) -> tuple[KnowledgeStorageEntry, ...]:
    """Freeze an available knowledge set inside grading-run creation.

    The task façade acquires its Workflow row first, then calls this fence before
    taking Assignment. Holding the User gate through the run/setup commit makes
    the active grading manifest and cleanup mutually exclusive: cleanup either
    observes the run, or run creation observes cleanup_pending.
    """
    raw_ids = tuple(document_ids)
    if len(raw_ids) > 5000 or any(
        not isinstance(document_id, str)
        or not document_id
        or len(document_id) > 64
        for document_id in raw_ids
    ):
        raise ValidationError(
            "The grading knowledge manifest is invalid.",
            code="knowledge_storage_manifest_invalid",
        )
    ids = tuple(dict.fromkeys(raw_ids))
    _lock_owner(session, owner_id)
    if not ids:
        return ()
    records = list(session.scalars(
        select(KnowledgeStorageRecord)
        .where(
            KnowledgeStorageRecord.owner_id == owner_id,
            KnowledgeStorageRecord.document_id.in_(ids),
        )
        .order_by(KnowledgeStorageRecord.document_id.asc())
        .with_for_update()
    ))
    by_document = {
        record.document_id: record for record in records
        if record.document_id is not None
    }
    unavailable = [
        document_id for document_id in ids
        if document_id not in by_document
        or by_document[document_id].state != KNOWLEDGE_STORAGE_AVAILABLE
        or by_document[document_id].stored_file_id is None
    ]
    if unavailable:
        raise InvalidTransition(
            "One or more frozen knowledge documents are unavailable.",
            code="knowledge_storage_input_unavailable",
            details={"unavailable_count": len(unavailable)},
        )
    # Lock all documents before any files, both in stable identifier order.
    # The User gate serializes lifecycle mutations, while this explicit order
    # also keeps future multi-document callers deterministic.
    locked_document_ids = sorted(str(value) for value in by_document)
    documents = list(session.scalars(
        select(KnowledgeDocumentRecord)
        .where(
            KnowledgeDocumentRecord.owner_id == owner_id,
            KnowledgeDocumentRecord.id.in_(locked_document_ids),
        )
        .order_by(KnowledgeDocumentRecord.id.asc())
        .with_for_update()
    ))
    stored_file_ids = sorted(
        str(record.stored_file_id) for record in records
        if record.stored_file_id is not None
    )
    files = list(session.scalars(
        select(StoredFileRecord)
        .where(
            StoredFileRecord.owner_id == owner_id,
            StoredFileRecord.id.in_(stored_file_ids),
        )
        .order_by(StoredFileRecord.id.asc())
        .with_for_update()
    ))
    if len(documents) != len(ids) or len(files) != len(ids):
        raise InvalidTransition(
            "One or more frozen knowledge documents are unavailable.",
            code="knowledge_storage_input_unavailable",
            details={"unavailable_count": max(
                len(ids) - len(documents), len(ids) - len(files),
            )},
        )
    return tuple(_entry(by_document[document_id]) for document_id in ids)


def reserve_upload(
    *,
    owner_id: str,
    original_name: str,
    size_bytes: int,
    sha256: str,
    content_type: str | None = None,
    title: str | None = None,
    retention_policy: str = KNOWLEDGE_RETENTION_RETAINED,
    origin_assignment_id: str | None = None,
    storage_backend: str | None = None,
    parser_version: str = "v1",
    now: float | None = None,
) -> KnowledgeUploadReservation:
    if size_bytes < 0:
        raise ValidationError(
            "Knowledge object size is invalid.",
            code="invalid_knowledge_storage_size",
        )
    digest = _validate_digest(sha256)
    if retention_policy not in KNOWLEDGE_RETENTION_POLICIES:
        raise ValidationError(
            "Knowledge retention policy is invalid.",
            code="invalid_knowledge_retention_policy",
        )
    if (
        retention_policy == KNOWLEDGE_RETENTION_TASK_ONLY
        and origin_assignment_id is None
    ):
        raise ValidationError(
            "Task-only knowledge requires an origin task.",
            code="knowledge_task_origin_required",
        )
    timestamp = time.time() if now is None else now
    safe_name = _safe_name(original_name)
    backend = storage_backend or settings.storage_backend
    if backend not in {"local", "object"}:
        raise ValidationError(
            "Knowledge storage backend is invalid.",
            code="invalid_knowledge_storage_backend",
        )

    with session_scope() as session:
        _lock_owner(session, owner_id)
        if origin_assignment_id is not None:
            _validate_origin_assignment(
                session, assignment_id=origin_assignment_id, owner_id=owner_id
            )
        existing = session.scalar(
            select(KnowledgeStorageRecord)
            .where(
                KnowledgeStorageRecord.owner_id == owner_id,
                KnowledgeStorageRecord.sha256 == digest,
            )
            .with_for_update()
        )
        if existing is not None:
            _lock_linked_records(session, existing)
            if existing.state == KNOWLEDGE_STORAGE_CLEANUP_PENDING:
                raise KnowledgeStorageReservationConflict(
                    "The matching knowledge object is still being deleted.",
                    code="knowledge_storage_cleanup_pending",
                    details={
                        "cleanup_operation_id": existing.cleanup_operation_id,
                        "state": existing.state,
                    },
                )
            if retention_policy == KNOWLEDGE_RETENTION_RETAINED:
                existing.retention_policy = KNOWLEDGE_RETENTION_RETAINED
                existing.origin_assignment_id = None
                existing.unattached_expires_at = None
                existing.updated_at = timestamp
            elif existing.state == KNOWLEDGE_STORAGE_AVAILABLE:
                reference_count = int(session.scalar(
                    select(func.count()).select_from(
                        AssignmentKnowledgeDocumentRecord
                    ).where(
                        AssignmentKnowledgeDocumentRecord.document_id
                        == existing.document_id
                    )
                ) or 0)
                if reference_count == 0:
                    # A retry/reuse gets a fresh bounded attach window. This
                    # cannot resurrect cleanup_pending (handled above).
                    existing.origin_assignment_id = origin_assignment_id
                    existing.unattached_expires_at = (
                        timestamp
                        + max(
                            1,
                            int(settings.knowledge_storage_unattached_ttl_seconds),
                        )
                    )
                    existing.updated_at = timestamp
            disposition = (
                    "existing"
                    if existing.state == KNOWLEDGE_STORAGE_AVAILABLE
                    else "in_progress"
                )
            exposed = _entry(existing)
            if disposition == "in_progress":
                exposed = replace(
                    exposed,
                    writer_claim_token=None,
                    writer_claimed_at=None,
                    writer_heartbeat_at=None,
                    writer_lease_expires_at=None,
                )
            return KnowledgeUploadReservation(
                entry=exposed,
                disposition=disposition,
            )

        usage = _usage_in_session(session, owner_id)
        if usage.used_bytes + size_bytes > usage.limit_bytes:
            raise KnowledgeStorageQuotaExceeded(
                "The knowledge-library storage allocation is full.",
                details={
                    **usage.as_dict(),
                    "requested_bytes": size_bytes,
                },
            )

        document_id = f"doc_{uuid.uuid4().hex[:24]}"
        entry_id = f"kfile_{uuid.uuid4().hex[:24]}"
        # Opaque exact keys keep permanent late-writer guards free of user
        # filenames and make each reservation impossible to reuse accidentally.
        storage_key = f"users/{owner_id}/knowledge/{entry_id}/content"
        writer_claim_token = uuid.uuid4().hex
        writer_lease = max(
            1, int(settings.knowledge_storage_writer_lease_seconds)
        )
        document = KnowledgeDocumentRecord(
            id=document_id,
            owner_id=owner_id,
            stored_file_id=None,
            title=(title or PurePosixPath(safe_name).stem or safe_name)[:512],
            original_name=safe_name,
            content_type=content_type,
            size_bytes=size_bytes,
            sha256=digest,
            status="processing",
            parser_version=parser_version,
            chunk_count=0,
            created_at=timestamp,
            updated_at=timestamp,
        )
        record = KnowledgeStorageRecord(
            id=entry_id,
            owner_id=owner_id,
            document_id=document_id,
            stored_file_id=None,
            origin_assignment_id=origin_assignment_id,
            retention_policy=retention_policy,
            state=KNOWLEDGE_STORAGE_RESERVED,
            original_name=safe_name,
            content_type=content_type,
            size_bytes=size_bytes,
            sha256=digest,
            storage_backend=backend,
            storage_key=storage_key,
            cleanup_reason=None,
            error_code=None,
            cleanup_operation_id=None,
            reservation_expires_at=(
                timestamp
                + max(
                    writer_lease,
                    int(settings.knowledge_storage_reservation_ttl_seconds),
                )
            ),
            unattached_expires_at=None,
            available_at=None,
            cleanup_requested_at=None,
            cleanup_last_attempt_at=None,
            cleanup_retry_at=None,
            cleanup_attempt_count=0,
            cleanup_claim_token=None,
            cleanup_claimed_at=None,
            writer_claim_token=writer_claim_token,
            writer_claimed_at=timestamp,
            writer_heartbeat_at=timestamp,
            writer_lease_expires_at=timestamp + writer_lease,
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(document)
        session.flush()
        session.add(record)
        session.flush()
        # The guard is created before any object I/O and has no User FK. It is
        # therefore still able to erase a late PUT if an administrative account
        # deletion cascades the charged ledger while the writer is blocked.
        session.add(KnowledgeStorageOrphanGuardRecord(
            id=f"kguard_{uuid.uuid4().hex[:24]}",
            owner_id=owner_id,
            storage_backend=backend,
            storage_key=storage_key,
            writer_claim_token=writer_claim_token,
            next_check_at=timestamp + writer_lease,
            last_attempt_at=None,
            attempt_count=0,
            error_code=None,
            claim_token=None,
            claimed_at=None,
            created_at=timestamp,
            updated_at=timestamp,
        ))
        session.flush()
        return KnowledgeUploadReservation(entry=_entry(record), disposition="reserved")


def renew_upload_writer_claim(
    *,
    reservation_id: str,
    owner_id: str,
    writer_claim_token: str,
    lease_seconds: int | None = None,
    now: float | None = None,
) -> KnowledgeStorageEntry:
    lease = max(1, int(
        settings.knowledge_storage_writer_lease_seconds
        if lease_seconds is None else lease_seconds
    ))
    with session_scope() as session:
        _lock_owner(session, owner_id)
        record = session.scalar(
            select(KnowledgeStorageRecord)
            .where(
                KnowledgeStorageRecord.id == reservation_id,
                KnowledgeStorageRecord.owner_id == owner_id,
            )
            .with_for_update()
        )
        guard = None
        if record is not None:
            guard = session.scalar(
                select(KnowledgeStorageOrphanGuardRecord)
                .where(
                    KnowledgeStorageOrphanGuardRecord.storage_key
                    == record.storage_key,
                    KnowledgeStorageOrphanGuardRecord.writer_claim_token
                    == writer_claim_token,
                )
                .with_for_update()
            )
        # For production calls, evaluate expiry after all required locks are
        # held. A heartbeat delayed behind a due guard claim cannot renew using
        # a timestamp captured before that claim.
        timestamp = time.time() if now is None else now
        if (
            record is None
            or record.state != KNOWLEDGE_STORAGE_RESERVED
            or record.writer_claim_token != writer_claim_token
            or record.writer_lease_expires_at is None
            or record.writer_lease_expires_at <= timestamp
            or guard is None
            or guard.claim_token is not None
        ):
            raise KnowledgeStorageReservationConflict(
                "The knowledge upload writer no longer owns its reservation.",
                code="knowledge_storage_writer_claim_lost",
            )
        record.writer_heartbeat_at = timestamp
        record.writer_lease_expires_at = timestamp + lease
        record.reservation_expires_at = timestamp + max(
            lease,
            int(settings.knowledge_storage_reservation_ttl_seconds),
        )
        record.updated_at = timestamp
        guard.next_check_at = timestamp + lease
        guard.error_code = None
        guard.updated_at = timestamp
        session.flush()
        return _entry(record)


def publish_upload(
    *,
    reservation_id: str,
    owner_id: str,
    writer_claim_token: str,
    now: float | None = None,
) -> KnowledgeUploadReservation:
    with session_scope() as session:
        _lock_owner(session, owner_id)
        record = session.scalar(
            select(KnowledgeStorageRecord)
            .where(
                KnowledgeStorageRecord.id == reservation_id,
                KnowledgeStorageRecord.owner_id == owner_id,
            )
            .with_for_update()
        )
        if record is None:
            raise NotFound("knowledge_storage_reservation")
        document, stored = _lock_linked_records(session, record)
        if record.state == KNOWLEDGE_STORAGE_CLEANUP_PENDING:
            raise KnowledgeStorageReservationConflict(
                "The knowledge upload is already pending cleanup.",
                code="knowledge_storage_cleanup_pending",
            )
        if record.state == KNOWLEDGE_STORAGE_AVAILABLE:
            return KnowledgeUploadReservation(entry=_entry(record), disposition="existing")
        guard = session.scalar(
            select(KnowledgeStorageOrphanGuardRecord)
            .where(
                KnowledgeStorageOrphanGuardRecord.storage_key
                == record.storage_key,
                KnowledgeStorageOrphanGuardRecord.writer_claim_token
                == writer_claim_token,
            )
            .with_for_update()
        )
        # Production expiry is evaluated only after User, ledger, linked
        # document/file, and guard locks are held. A publication delayed behind
        # another transaction cannot use a stale pre-wait timestamp.
        timestamp = time.time() if now is None else now
        if (
            record.writer_claim_token != writer_claim_token
            or record.writer_lease_expires_at is None
            or record.writer_lease_expires_at <= timestamp
            or guard is None
            or guard.claim_token is not None
        ):
            raise KnowledgeStorageReservationConflict(
                "The knowledge upload writer no longer owns its reservation.",
                code="knowledge_storage_writer_claim_lost",
            )
        if document is None:
            raise KnowledgeStorageReservationConflict(
                "The knowledge reservation lost its document.",
                code="knowledge_storage_reservation_corrupt",
            )
        if record.origin_assignment_id is not None:
            _validate_origin_assignment(
                session,
                assignment_id=record.origin_assignment_id,
                owner_id=owner_id,
            )
        if stored is None:
            stored = StoredFileRecord(
                id=record.id,
                owner_id=owner_id,
                kind="personal_knowledge",
                original_name=record.original_name,
                storage_backend=record.storage_backend,
                storage_key=record.storage_key,
                content_type=record.content_type,
                size_bytes=record.size_bytes,
                sha256=record.sha256,
                source_quota_owner_id=None,
                source_quota_bytes=0,
                assignment_id=None,
                submission_revision_id=None,
                knowledge_document_id=document.id,
                created_at=timestamp,
            )
            session.add(stored)
            session.flush()
        document.stored_file_id = stored.id
        document.updated_at = timestamp
        record.stored_file_id = stored.id
        record.state = KNOWLEDGE_STORAGE_AVAILABLE
        record.available_at = timestamp
        record.reservation_expires_at = None
        record.cleanup_reason = None
        record.error_code = None
        record.cleanup_retry_at = None
        record.unattached_expires_at = (
            timestamp
            + max(1, int(settings.knowledge_storage_unattached_ttl_seconds))
            if record.retention_policy == KNOWLEDGE_RETENTION_TASK_ONLY
            else None
        )
        record.writer_claim_token = None
        record.writer_claimed_at = None
        record.writer_heartbeat_at = None
        record.writer_lease_expires_at = None
        record.updated_at = timestamp
        session.delete(guard)
        session.flush()
        return KnowledgeUploadReservation(entry=_entry(record), disposition="reserved")


def promote_retained_in_session(
    session: Session,
    *,
    document_id: str,
    owner_id: str,
    now: float | None = None,
) -> KnowledgeStorageEntry:
    timestamp = time.time() if now is None else now
    _lock_owner(session, owner_id)
    record = _lock_entry_by_document(
        session, document_id=document_id, owner_id=owner_id
    )
    if record is None:
        raise NotFound("knowledge_document")
    _lock_linked_records(session, record)
    if record.state == KNOWLEDGE_STORAGE_CLEANUP_PENDING:
        raise KnowledgeStorageReservationConflict(
            "A deleting knowledge document cannot be retained.",
            code="knowledge_storage_cleanup_pending",
        )
    record.retention_policy = KNOWLEDGE_RETENTION_RETAINED
    record.origin_assignment_id = None
    record.unattached_expires_at = None
    record.updated_at = timestamp
    session.flush()
    return _entry(record)


def promote_retained(document_id: str, owner_id: str) -> KnowledgeStorageEntry:
    with session_scope() as session:
        return promote_retained_in_session(
            session, document_id=document_id, owner_id=owner_id
        )


def mark_document_attached_in_session(
    session: Session,
    *,
    assignment_id: str,
    owner_id: str,
    document_id: str,
    now: float | None = None,
) -> KnowledgeStorageEntry:
    timestamp = time.time() if now is None else now
    _lock_owner(session, owner_id)
    _validate_origin_assignment(
        session, assignment_id=assignment_id, owner_id=owner_id
    )
    record = _lock_entry_by_document(
        session, document_id=document_id, owner_id=owner_id
    )
    if record is None:
        raise NotFound("knowledge_document")
    _lock_linked_records(session, record)
    if record.state != KNOWLEDGE_STORAGE_AVAILABLE:
        raise InvalidTransition(
            "The knowledge document is not available.",
            code="knowledge_storage_not_available",
        )
    attached = session.scalar(select(func.count()).select_from(
        AssignmentKnowledgeDocumentRecord
    ).where(
        AssignmentKnowledgeDocumentRecord.assignment_id == assignment_id,
        AssignmentKnowledgeDocumentRecord.document_id == document_id,
    ))
    if not attached:
        raise InvalidTransition(
            "The task knowledge attachment was not persisted.",
            code="knowledge_attachment_not_persisted",
        )
    if record.retention_policy == KNOWLEDGE_RETENTION_TASK_ONLY:
        record.unattached_expires_at = None
        record.updated_at = timestamp
    session.flush()
    return _entry(record)


def _document_is_frozen_by_active_run(
    session: Session, *, document_id: str, owner_id: str,
) -> bool:
    from backend.db.workflow_repository import GradingRunSetupRecord

    manifests = session.scalars(
        select(GradingRunSetupRecord.input_manifest)
        .join(
            GradingRunRecord,
            GradingRunRecord.id == GradingRunSetupRecord.grading_run_id,
        )
        .where(
            GradingRunSetupRecord.owner_id == owner_id,
            GradingRunRecord.status.in_(("queued", "running")),
        )
    ).all()
    return any(
        document_id in (manifest or {}).get("knowledge_document_ids", [])
        for manifest in manifests
    )


def _enqueue_cleanup(
    record: KnowledgeStorageRecord, *, reason: str, timestamp: float,
) -> KnowledgeCleanupRequest:
    if reason not in KNOWLEDGE_CLEANUP_REASONS:
        raise ValidationError(
            "Knowledge cleanup reason is invalid.",
            code="invalid_knowledge_cleanup_reason",
        )
    if record.state != KNOWLEDGE_STORAGE_CLEANUP_PENDING:
        record.state = KNOWLEDGE_STORAGE_CLEANUP_PENDING
        record.cleanup_operation_id = f"kcleanup_{uuid.uuid4().hex[:24]}"
        record.cleanup_requested_at = timestamp
        record.cleanup_attempt_count = 0
    record.cleanup_reason = reason
    record.error_code = None
    record.reservation_expires_at = None
    record.unattached_expires_at = None
    record.cleanup_retry_at = timestamp
    record.cleanup_claim_token = None
    record.cleanup_claimed_at = None
    record.updated_at = timestamp
    return KnowledgeCleanupRequest(
        status="cleanup_pending",
        entry_id=record.id,
        document_id=record.document_id,
        cleanup_operation_id=record.cleanup_operation_id,
        size_bytes=record.size_bytes,
    )


def request_document_cleanup_in_session(
    session: Session,
    *,
    document_id: str,
    owner_id: str,
    reason: str = KNOWLEDGE_CLEANUP_EXPLICIT_DELETE,
    now: float | None = None,
) -> KnowledgeCleanupRequest:
    timestamp = time.time() if now is None else now
    _lock_owner(session, owner_id)
    record = _lock_entry_by_document(
        session, document_id=document_id, owner_id=owner_id
    )
    if record is None:
        raise NotFound("knowledge_document")
    _lock_linked_records(session, record)
    if _document_is_frozen_by_active_run(
        session, document_id=document_id, owner_id=owner_id
    ):
        raise InvalidTransition(
            "Knowledge document is frozen by an active grading run.",
            code="knowledge_document_in_active_grading_run",
        )
    # An explicit knowledge deletion owns the canonical document, not merely
    # one presentation surface. Detach every assignment reference only after
    # the frozen-run guard succeeds; course metadata stays hidden by ledger
    # state and is cascade-removed after physical deletion is confirmed.
    session.execute(delete(AssignmentKnowledgeDocumentRecord).where(
        AssignmentKnowledgeDocumentRecord.document_id == document_id
    ))
    request = _enqueue_cleanup(record, reason=reason, timestamp=timestamp)
    session.flush()
    return request


def request_document_cleanup(
    document_id: str,
    owner_id: str,
    *,
    reason: str = KNOWLEDGE_CLEANUP_EXPLICIT_DELETE,
) -> KnowledgeCleanupRequest:
    with session_scope() as session:
        return request_document_cleanup_in_session(
            session,
            document_id=document_id,
            owner_id=owner_id,
            reason=reason,
        )


def request_task_only_cleanup_if_unreferenced_in_session(
    session: Session,
    *,
    document_id: str,
    owner_id: str,
    reason: str = KNOWLEDGE_CLEANUP_TASK_UNREFERENCED,
    now: float | None = None,
) -> KnowledgeCleanupRequest:
    timestamp = time.time() if now is None else now
    _lock_owner(session, owner_id)
    record = _lock_entry_by_document(
        session, document_id=document_id, owner_id=owner_id
    )
    if record is None:
        return KnowledgeCleanupRequest(
            status="not_found", entry_id="", document_id=document_id,
            cleanup_operation_id=None, size_bytes=0,
        )
    _lock_linked_records(session, record)
    if record.retention_policy == KNOWLEDGE_RETENTION_RETAINED:
        return KnowledgeCleanupRequest(
            status="retained", entry_id=record.id, document_id=document_id,
            cleanup_operation_id=record.cleanup_operation_id,
            size_bytes=record.size_bytes,
        )
    if record.state == KNOWLEDGE_STORAGE_CLEANUP_PENDING:
        return KnowledgeCleanupRequest(
            status="cleanup_pending", entry_id=record.id,
            document_id=document_id,
            cleanup_operation_id=record.cleanup_operation_id,
            size_bytes=record.size_bytes,
        )
    reference_count = int(session.scalar(
        select(func.count()).select_from(AssignmentKnowledgeDocumentRecord).where(
            AssignmentKnowledgeDocumentRecord.document_id == document_id
        )
    ) or 0)
    if reference_count:
        record.unattached_expires_at = None
        return KnowledgeCleanupRequest(
            status="still_referenced", entry_id=record.id,
            document_id=document_id, cleanup_operation_id=None,
            size_bytes=record.size_bytes,
        )
    if _document_is_frozen_by_active_run(
        session, document_id=document_id, owner_id=owner_id
    ):
        record.unattached_expires_at = timestamp + _retry_delay(1)
        record.updated_at = timestamp
        return KnowledgeCleanupRequest(
            status="deferred_active_grading", entry_id=record.id,
            document_id=document_id, cleanup_operation_id=None,
            size_bytes=record.size_bytes,
        )
    request = _enqueue_cleanup(record, reason=reason, timestamp=timestamp)
    session.flush()
    return request


def request_task_only_cleanup_if_unreferenced(
    document_id: str,
    owner_id: str,
    *,
    reason: str = KNOWLEDGE_CLEANUP_TASK_UNREFERENCED,
) -> KnowledgeCleanupRequest:
    with session_scope() as session:
        return request_task_only_cleanup_if_unreferenced_in_session(
            session,
            document_id=document_id,
            owner_id=owner_id,
            reason=reason,
        )


def reconcile_assignment_documents_in_session(
    session: Session,
    *,
    assignment_id: str,
    owner_id: str,
    previous_document_ids: Iterable[str],
    current_document_ids: Iterable[str],
    cleanup_reason: str = KNOWLEDGE_CLEANUP_TASK_UNREFERENCED,
    now: float | None = None,
) -> tuple[KnowledgeCleanupRequest, ...]:
    previous = set(previous_document_ids)
    current = set(current_document_ids)
    for document_id in sorted(current):
        mark_document_attached_in_session(
            session,
            assignment_id=assignment_id,
            owner_id=owner_id,
            document_id=document_id,
            now=now,
        )
    requests = [
        request_task_only_cleanup_if_unreferenced_in_session(
            session,
            document_id=document_id,
            owner_id=owner_id,
            reason=cleanup_reason,
            now=now,
        )
        for document_id in sorted(previous - current)
    ]
    return tuple(requests)


def detach_assignment_documents_in_session(
    session: Session,
    *,
    assignment_id: str,
    owner_id: str,
    reason: str = KNOWLEDGE_CLEANUP_TASK_DELETED,
    now: float | None = None,
) -> tuple[KnowledgeCleanupRequest, ...]:
    _lock_owner(session, owner_id)
    document_ids = tuple(str(value) for value in session.scalars(
        select(AssignmentKnowledgeDocumentRecord.document_id)
        .join(
            AssignmentRecord,
            AssignmentRecord.id
            == AssignmentKnowledgeDocumentRecord.assignment_id,
        )
        .where(
            AssignmentKnowledgeDocumentRecord.assignment_id == assignment_id,
            AssignmentRecord.teacher_id == owner_id,
        )
        .order_by(AssignmentKnowledgeDocumentRecord.document_id.asc())
    ))
    session.execute(delete(AssignmentKnowledgeDocumentRecord).where(
        AssignmentKnowledgeDocumentRecord.assignment_id == assignment_id
    ))
    session.flush()
    return tuple(
        request_task_only_cleanup_if_unreferenced_in_session(
            session,
            document_id=document_id,
            owner_id=owner_id,
            reason=reason,
            now=now,
        )
        for document_id in document_ids
    )


def mark_upload_cleanup_pending(
    *,
    reservation_id: str,
    owner_id: str,
    writer_claim_token: str,
    writer_completed: bool,
    reason: str,
    error_code: str,
    now: float | None = None,
) -> KnowledgeCleanupRequest:
    timestamp = time.time() if now is None else now
    with session_scope() as session:
        _lock_owner(session, owner_id)
        record = session.scalar(
            select(KnowledgeStorageRecord)
            .where(
                KnowledgeStorageRecord.id == reservation_id,
                KnowledgeStorageRecord.owner_id == owner_id,
            )
            .with_for_update()
        )
        if record is None:
            raise NotFound("knowledge_storage_reservation")
        _lock_linked_records(session, record)
        if (
            record.state != KNOWLEDGE_STORAGE_RESERVED
            or record.writer_claim_token != writer_claim_token
        ):
            raise KnowledgeStorageReservationConflict(
                "The knowledge upload writer no longer owns its reservation.",
                code="knowledge_storage_writer_claim_lost",
            )
        if writer_completed:
            # The SDK confirmed its PUT call returned successfully and no future
            # write remains for this token. Clear the writer fence so ordinary
            # compensation may retire the guard after exact-key deletion.
            record.writer_claim_token = None
            record.writer_claimed_at = None
            record.writer_heartbeat_at = None
            record.writer_lease_expires_at = None
        else:
            # A save exception is outcome-uncertain: the object store may still
            # complete the PUT after a compensating delete. Expire the lease now
            # but retain its token and permanent pre-I/O guard forever.
            record.writer_heartbeat_at = timestamp
            record.writer_lease_expires_at = timestamp
            guard = session.scalar(
                select(KnowledgeStorageOrphanGuardRecord)
                .where(
                    KnowledgeStorageOrphanGuardRecord.storage_key
                    == record.storage_key,
                    KnowledgeStorageOrphanGuardRecord.writer_claim_token
                    == writer_claim_token,
                )
                .with_for_update()
            )
            if guard is None:
                raise KnowledgeStorageReservationConflict(
                    "The uncertain writer lost its permanent orphan guard.",
                    code="knowledge_orphan_guard_missing",
                )
            guard.next_check_at = min(guard.next_check_at, timestamp)
            guard.updated_at = timestamp
        request = _enqueue_cleanup(record, reason=reason, timestamp=timestamp)
        record.error_code = error_code[:128]
        session.flush()
        return request


def abort_reservation_after_confirmed_delete(
    *,
    reservation_id: str,
    owner_id: str,
    writer_claim_token: str,
) -> bool:
    """Release a pre-publication charge only after exact-key deletion."""
    with session_scope() as session:
        _lock_owner(session, owner_id)
        record = session.scalar(
            select(KnowledgeStorageRecord)
            .where(
                KnowledgeStorageRecord.id == reservation_id,
                KnowledgeStorageRecord.owner_id == owner_id,
            )
            .with_for_update()
        )
        if record is None:
            return False
        document, stored = _lock_linked_records(session, record)
        if stored is not None or record.state == KNOWLEDGE_STORAGE_AVAILABLE:
            raise KnowledgeStorageReservationConflict(
                "A published knowledge object cannot use reservation abort.",
                code="knowledge_storage_already_published",
            )
        if record.writer_claim_token is not None:
            raise KnowledgeStorageReservationConflict(
                "A live or expired writer requires fenced stale-writer cleanup.",
                code="knowledge_storage_writer_claim_active",
            )
        guard = session.scalar(
            select(KnowledgeStorageOrphanGuardRecord)
            .where(KnowledgeStorageOrphanGuardRecord.storage_key == record.storage_key)
            .with_for_update()
        )
        if guard is None or guard.writer_claim_token != writer_claim_token:
            raise KnowledgeStorageReservationConflict(
                "The completed writer lost its exact orphan guard.",
                code="knowledge_orphan_guard_missing",
            )
        session.delete(record)
        session.flush()
        if document is not None:
            session.delete(document)
        if guard is not None:
            session.delete(guard)
        return True


def claim_next_cleanup(
    *,
    worker_id: str,
    lease_seconds: int | None = None,
    now: float | None = None,
) -> KnowledgeCleanupClaim | None:
    del worker_id  # claim tokens, not process names, fence completion
    timestamp = time.time() if now is None else now
    lease = max(1, int(
        settings.knowledge_cleanup_claim_seconds
        if lease_seconds is None else lease_seconds
    ))
    claim_expired_before = timestamp - lease
    writer_reapable = or_(
        KnowledgeStorageRecord.writer_claim_token.is_(None),
        and_(
            KnowledgeStorageRecord.writer_lease_expires_at.is_not(None),
            KnowledgeStorageRecord.writer_lease_expires_at <= timestamp,
        ),
    )
    candidate_condition = or_(
        and_(
            KnowledgeStorageRecord.state == KNOWLEDGE_STORAGE_CLEANUP_PENDING,
            writer_reapable,
            or_(
                KnowledgeStorageRecord.cleanup_retry_at.is_(None),
                KnowledgeStorageRecord.cleanup_retry_at <= timestamp,
            ),
            or_(
                KnowledgeStorageRecord.cleanup_claim_token.is_(None),
                KnowledgeStorageRecord.cleanup_claimed_at <= claim_expired_before,
            ),
        ),
        and_(
            KnowledgeStorageRecord.state == KNOWLEDGE_STORAGE_RESERVED,
            KnowledgeStorageRecord.reservation_expires_at.is_not(None),
            KnowledgeStorageRecord.reservation_expires_at <= timestamp,
            writer_reapable,
        ),
        and_(
            KnowledgeStorageRecord.state == KNOWLEDGE_STORAGE_AVAILABLE,
            KnowledgeStorageRecord.retention_policy
            == KNOWLEDGE_RETENTION_TASK_ONLY,
            KnowledgeStorageRecord.unattached_expires_at.is_not(None),
            KnowledgeStorageRecord.unattached_expires_at <= timestamp,
        ),
    )
    # Read candidates without row locks so the required owner gate remains the
    # first lock. Each candidate is revalidated after acquiring that gate.
    with session_scope() as session:
        candidates = list(session.execute(
            select(KnowledgeStorageRecord.id, KnowledgeStorageRecord.owner_id)
            .where(candidate_condition)
            .order_by(KnowledgeStorageRecord.updated_at.asc())
            .limit(max(8, int(settings.knowledge_cleanup_batch_size) * 4))
        ))

    for entry_id, owner_id in candidates:
        with session_scope() as session:
            _lock_owner(session, str(owner_id))
            record = session.scalar(
                select(KnowledgeStorageRecord)
                .where(KnowledgeStorageRecord.id == entry_id)
                .with_for_update()
            )
            if record is None or record.owner_id != owner_id:
                continue
            document, _stored = _lock_linked_records(session, record)
            if record.state == KNOWLEDGE_STORAGE_RESERVED:
                if (
                    record.reservation_expires_at is None
                    or record.reservation_expires_at > timestamp
                    or (
                        record.writer_claim_token is not None
                        and (
                            record.writer_lease_expires_at is None
                            or record.writer_lease_expires_at > timestamp
                        )
                    )
                ):
                    continue
                _enqueue_cleanup(
                    record,
                    reason=KNOWLEDGE_CLEANUP_UPLOAD_ABANDONED,
                    timestamp=timestamp,
                )
            elif record.state == KNOWLEDGE_STORAGE_AVAILABLE:
                if (
                    record.retention_policy != KNOWLEDGE_RETENTION_TASK_ONLY
                    or record.unattached_expires_at is None
                    or record.unattached_expires_at > timestamp
                ):
                    continue
                reference_count = int(session.scalar(
                    select(func.count()).select_from(
                        AssignmentKnowledgeDocumentRecord
                    ).where(
                        AssignmentKnowledgeDocumentRecord.document_id
                        == record.document_id
                    )
                ) or 0)
                if reference_count:
                    record.unattached_expires_at = None
                    continue
                if (
                    document is not None
                    and _document_is_frozen_by_active_run(
                        session,
                        document_id=document.id,
                        owner_id=record.owner_id,
                    )
                ):
                    record.unattached_expires_at = timestamp + _retry_delay(1)
                    continue
                _enqueue_cleanup(
                    record,
                    reason=KNOWLEDGE_CLEANUP_TASK_UNREFERENCED,
                    timestamp=timestamp,
                )
            elif record.state == KNOWLEDGE_STORAGE_CLEANUP_PENDING:
                if (
                    record.cleanup_retry_at is not None
                    and record.cleanup_retry_at > timestamp
                ):
                    continue
                if (
                    record.cleanup_claim_token is not None
                    and record.cleanup_claimed_at is not None
                    and record.cleanup_claimed_at > claim_expired_before
                ):
                    continue
                if (
                    record.writer_claim_token is not None
                    and (
                        record.writer_lease_expires_at is None
                        or record.writer_lease_expires_at > timestamp
                    )
                ):
                    continue
                if (
                    document is not None
                    and _document_is_frozen_by_active_run(
                        session,
                        document_id=document.id,
                        owner_id=record.owner_id,
                    )
                ):
                    record.cleanup_retry_at = timestamp + _retry_delay(
                        max(1, record.cleanup_attempt_count)
                    )
                    record.cleanup_claim_token = None
                    record.cleanup_claimed_at = None
                    continue
            else:
                continue

            token = uuid.uuid4().hex
            record.cleanup_attempt_count += 1
            record.cleanup_last_attempt_at = timestamp
            record.cleanup_claim_token = token
            record.cleanup_claimed_at = timestamp
            record.updated_at = timestamp
            session.flush()
            assert record.cleanup_operation_id is not None
            return KnowledgeCleanupClaim(
                entry_id=record.id,
                owner_id=record.owner_id,
                document_id=record.document_id,
                stored_file_id=record.stored_file_id,
                cleanup_operation_id=record.cleanup_operation_id,
                storage_backend=record.storage_backend,
                storage_key=record.storage_key,
                size_bytes=record.size_bytes,
                attempt=record.cleanup_attempt_count,
                claim_token=token,
            )
    return None


def finish_cleanup(
    claim: KnowledgeCleanupClaim,
    *,
    deleted: bool,
    error_code: str | None = None,
    now: float | None = None,
) -> KnowledgeCleanupResult:
    timestamp = time.time() if now is None else now
    with session_scope() as session:
        _lock_owner(session, claim.owner_id)
        record = session.scalar(
            select(KnowledgeStorageRecord)
            .where(
                KnowledgeStorageRecord.id == claim.entry_id,
                KnowledgeStorageRecord.owner_id == claim.owner_id,
            )
            .with_for_update()
        )
        if record is None:
            return KnowledgeCleanupResult(
                status="already_deleted", entry_id=claim.entry_id,
                size_bytes=claim.size_bytes,
            )
        document, stored = _lock_linked_records(session, record)
        if (
            record.state != KNOWLEDGE_STORAGE_CLEANUP_PENDING
            or record.cleanup_operation_id != claim.cleanup_operation_id
            or record.cleanup_claim_token != claim.claim_token
            or record.cleanup_attempt_count != claim.attempt
        ):
            raise KnowledgeStorageReservationConflict(
                "The knowledge cleanup claim is stale.",
                code="knowledge_cleanup_claim_lost",
            )
        if not deleted:
            retry_at = timestamp + _retry_delay(record.cleanup_attempt_count)
            record.cleanup_reason = KNOWLEDGE_CLEANUP_STORAGE_DELETE_FAILED
            record.error_code = (error_code or "knowledge_storage_delete_failed")[:128]
            record.cleanup_retry_at = retry_at
            record.cleanup_claim_token = None
            record.cleanup_claimed_at = None
            record.updated_at = timestamp
            return KnowledgeCleanupResult(
                status="retrying", entry_id=record.id,
                size_bytes=record.size_bytes, retry_at=retry_at,
            )

        size_bytes = record.size_bytes
        guarded = record.writer_claim_token is not None
        guard = session.scalar(
            select(KnowledgeStorageOrphanGuardRecord)
            .where(KnowledgeStorageOrphanGuardRecord.storage_key == record.storage_key)
            .with_for_update()
        )
        if guarded:
            # Physical deletion observed the exact key absent/deleted, so quota
            # can be released. The pre-I/O, zero-quota guard remains forever
            # because an expired writer may still complete a late PUT.
            if guard is None or guard.writer_claim_token != record.writer_claim_token:
                raise KnowledgeStorageReservationConflict(
                    "The expired writer lost its permanent orphan guard.",
                    code="knowledge_orphan_guard_missing",
                )
            guard.next_check_at = min(guard.next_check_at, timestamp)
            guard.updated_at = timestamp
        elif guard is not None:
            # The writer explicitly completed object I/O and ordinary cleanup
            # confirmed the key gone. No future late PUT exists for this token.
            session.delete(guard)
        if document is not None and stored is not None:
            if document.stored_file_id == stored.id:
                document.stored_file_id = None
            if stored.knowledge_document_id == document.id:
                stored.knowledge_document_id = None
            session.flush()
        session.delete(record)
        session.flush()
        if stored is not None:
            session.delete(stored)
        if document is not None:
            session.delete(document)
        return KnowledgeCleanupResult(
            status="deleted_guarded" if guarded else "deleted",
            entry_id=claim.entry_id,
            size_bytes=size_bytes,
        )


def claim_next_orphan_guard(
    *,
    worker_id: str,
    lease_seconds: int | None = None,
    now: float | None = None,
) -> KnowledgeOrphanGuardClaim | None:
    """Claim one permanent late-writer guard without acquiring a User lock.

    Guards deliberately outlive user rows. They never acquire ledger/document
    locks, which prevents a Guard -> User inversion with cleanup completion and
    stale-writer acknowledgement.
    """
    del worker_id
    timestamp = time.time() if now is None else now
    lease = max(1, int(
        settings.knowledge_cleanup_claim_seconds
        if lease_seconds is None else lease_seconds
    ))
    claim_expired_before = timestamp - lease
    with session_scope() as session:
        guard = session.scalar(
            select(KnowledgeStorageOrphanGuardRecord)
            .where(
                KnowledgeStorageOrphanGuardRecord.next_check_at <= timestamp,
                ~exists(
                    select(1).where(
                        KnowledgeStorageRecord.storage_key
                        == KnowledgeStorageOrphanGuardRecord.storage_key
                    )
                ),
                or_(
                    KnowledgeStorageOrphanGuardRecord.claim_token.is_(None),
                    KnowledgeStorageOrphanGuardRecord.claimed_at
                    <= claim_expired_before,
                ),
            )
            .order_by(
                KnowledgeStorageOrphanGuardRecord.next_check_at.asc(),
                KnowledgeStorageOrphanGuardRecord.id.asc(),
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if guard is None:
            return None
        token = uuid.uuid4().hex
        guard.attempt_count = min(2_000_000_000, guard.attempt_count + 1)
        guard.last_attempt_at = timestamp
        guard.claim_token = token
        guard.claimed_at = timestamp
        guard.updated_at = timestamp
        session.flush()
        return KnowledgeOrphanGuardClaim(
            guard_id=guard.id,
            owner_id=guard.owner_id,
            storage_backend=guard.storage_backend,
            storage_key=guard.storage_key,
            writer_claim_token=guard.writer_claim_token,
            attempt=guard.attempt_count,
            claim_token=token,
        )


def finish_orphan_guard_check(
    claim: KnowledgeOrphanGuardClaim,
    *,
    deleted: bool,
    error_code: str | None = None,
    now: float | None = None,
) -> float:
    """Finish a guard pass and schedule another until its writer acknowledges.

    A successful delete does *not* remove the guard: the expired writer may be
    paused before its PUT reaches the backend. Only that writer can acknowledge
    after it has itself confirmed exact-key deletion.
    """
    timestamp = time.time() if now is None else now
    with session_scope() as session:
        guard = session.scalar(
            select(KnowledgeStorageOrphanGuardRecord)
            .where(KnowledgeStorageOrphanGuardRecord.id == claim.guard_id)
            .with_for_update()
        )
        if guard is None:
            return timestamp
        if (
            guard.claim_token != claim.claim_token
            or guard.attempt_count != claim.attempt
            or guard.writer_claim_token != claim.writer_claim_token
        ):
            raise KnowledgeStorageReservationConflict(
                "The knowledge orphan-guard claim is stale.",
                code="knowledge_orphan_guard_claim_lost",
            )
        if deleted:
            next_check_at = timestamp + max(
                1, int(settings.knowledge_orphan_guard_recheck_seconds)
            )
            guard.error_code = None
        else:
            next_check_at = timestamp + _retry_delay(guard.attempt_count)
            guard.error_code = (
                error_code or "knowledge_orphan_guard_delete_failed"
            )[:128]
        guard.next_check_at = next_check_at
        guard.claim_token = None
        guard.claimed_at = None
        guard.updated_at = timestamp
        session.flush()
        return next_check_at


def acknowledge_stale_writer_cleanup(
    *,
    reservation_id: str,
    owner_id: str,
    storage_key: str,
    writer_claim_token: str,
) -> bool:
    """Remove stale-writer metadata after that writer deleted its exact key.

    The owner lock is conditional because a permanent guard intentionally
    survives account deletion. If a ledger row remains, its User FK guarantees
    the owner row exists and this function preserves User -> ledger -> guard.
    """
    with session_scope() as session:
        owner_result = session.execute(
            update(UserRecord)
            .where(UserRecord.id == owner_id)
            .values(updated_at=UserRecord.updated_at)
        )
        record = session.scalar(
            select(KnowledgeStorageRecord)
            .where(
                KnowledgeStorageRecord.id == reservation_id,
                KnowledgeStorageRecord.owner_id == owner_id,
            )
            .with_for_update()
        )
        if record is not None:
            if owner_result.rowcount != 1:
                raise KnowledgeStorageReservationConflict(
                    "Knowledge writer metadata lost its owner fence.",
                    code="knowledge_storage_owner_fence_lost",
                )
            if (
                record.writer_claim_token != writer_claim_token
                or record.storage_key != storage_key
                or record.state == KNOWLEDGE_STORAGE_AVAILABLE
                or record.stored_file_id is not None
            ):
                return False
            document, _stored = _lock_linked_records(session, record)
        else:
            document = None

        guard = session.scalar(
            select(KnowledgeStorageOrphanGuardRecord)
            .where(
                KnowledgeStorageOrphanGuardRecord.storage_key == storage_key,
                KnowledgeStorageOrphanGuardRecord.owner_id == owner_id,
            )
            .with_for_update()
        )
        if guard is not None and guard.writer_claim_token != writer_claim_token:
            return False

        if record is not None:
            session.delete(record)
            session.flush()
            if document is not None:
                session.delete(document)
        if guard is not None:
            session.delete(guard)
        return True


def orphan_guard_count(*, storage_key: str | None = None) -> int:
    """Diagnostic/test count; guards are zero-quota internal metadata."""
    statement = select(func.count()).select_from(KnowledgeStorageOrphanGuardRecord)
    if storage_key is not None:
        statement = statement.where(
            KnowledgeStorageOrphanGuardRecord.storage_key == storage_key
        )
    with session_scope() as session:
        return int(session.scalar(statement) or 0)


__all__ = [
    "abort_reservation_after_confirmed_delete",
    "acknowledge_stale_writer_cleanup",
    "claim_next_cleanup",
    "claim_next_orphan_guard",
    "detach_assignment_documents_in_session",
    "finish_cleanup",
    "finish_orphan_guard_check",
    "fence_grading_knowledge_in_session",
    "get_document_storage",
    "get_storage_entry",
    "knowledge_storage_usage",
    "lock_knowledge_owner_in_session",
    "mark_document_attached_in_session",
    "mark_upload_cleanup_pending",
    "orphan_guard_count",
    "promote_retained",
    "promote_retained_in_session",
    "publish_upload",
    "renew_upload_writer_claim",
    "reconcile_assignment_documents_in_session",
    "request_document_cleanup",
    "request_document_cleanup_in_session",
    "request_task_only_cleanup_if_unreferenced",
    "request_task_only_cleanup_if_unreferenced_in_session",
    "reserve_upload",
    "visible_document_ids",
]
