"""Durable workflow sources and immutable per-file terminal outcomes."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base
from backend.db.models import AssignmentRecord, StoredFileRecord
from backend.db.session import session_scope
from backend.db.workflow_repository import WorkflowOperationRecord
from backend.domain.errors import NotFound, ValidationError, VersionConflict


OUTCOME_STATUSES = frozenset({
    "parsed",
    "parse_failed",
    "identity_conflict",
    "no_matching_answer",
})
MAX_UNKNOWN_QUESTION_IDS = 100
MAX_QUESTION_ID_LENGTH = 64
MAX_UNKNOWN_QUESTION_IDS_JSON_BYTES = 8192


class WorkflowSourceItemRecord(Base):
    __tablename__ = "workflow_source_items"
    __table_args__ = (
        UniqueConstraint(
            "operation_id",
            "attempt",
            "order_index",
            name="uq_workflow_source_items_operation_attempt_order",
        ),
        UniqueConstraint(
            "operation_id",
            "attempt",
            "stored_file_id",
            name="uq_workflow_source_items_operation_attempt_file",
        ),
        CheckConstraint("attempt > 0", name="ck_workflow_source_items_attempt_positive"),
        CheckConstraint(
            "order_index >= 0",
            name="ck_workflow_source_items_order_nonnegative",
        ),
        Index(
            "ix_workflow_source_items_assignment_operation_attempt_order",
            "assignment_id",
            "operation_id",
            "attempt",
            "order_index",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False
    )
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_operations.id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    stored_file_id: Mapped[str] = mapped_column(
        ForeignKey("stored_files.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    retry_of_source_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_source_items.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)


class WorkflowSourceOutcomeRecord(Base):
    __tablename__ = "workflow_source_outcomes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('parsed', 'parse_failed', 'identity_conflict', "
            "'no_matching_answer')",
            name="ck_workflow_source_outcomes_status",
        ),
        CheckConstraint(
            "matched_answer_count >= 0",
            name="ck_workflow_source_outcomes_matched_count_nonnegative",
        ),
    )

    source_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_source_items.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    student_candidate: Mapped[str | None] = mapped_column(String(255), nullable=True)
    matched_answer_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unknown_question_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    stable_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    artifact_file_id: Mapped[str | None] = mapped_column(
        ForeignKey("stored_files.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)


@dataclass(frozen=True)
class WorkflowSourceItem:
    id: str
    owner_id: str
    assignment_id: str
    operation_id: str
    attempt: int
    order_index: int
    stored_file_id: str
    retry_of_source_id: str | None
    created_at: float
    original_name: str
    storage_backend: str
    storage_key: str
    content_type: str | None
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class WorkflowSourceOutcome:
    source_id: str
    status: str
    student_candidate: str | None
    matched_answer_count: int
    unknown_question_ids: tuple[str, ...]
    stable_error_code: str | None
    retryable: bool
    artifact_file_id: str | None
    created_at: float


@dataclass(frozen=True)
class WorkflowSourceSummary:
    uploaded_count: int
    success_count: int
    failed_count: int
    conflict_count: int
    pending_count: int

    @property
    def is_complete(self) -> bool:
        return self.pending_count == 0


def _source_dto(
    source: WorkflowSourceItemRecord,
    stored_file: StoredFileRecord,
) -> WorkflowSourceItem:
    return WorkflowSourceItem(
        id=source.id,
        owner_id=source.owner_id,
        assignment_id=source.assignment_id,
        operation_id=source.operation_id,
        attempt=source.attempt,
        order_index=source.order_index,
        stored_file_id=source.stored_file_id,
        retry_of_source_id=source.retry_of_source_id,
        created_at=source.created_at,
        original_name=stored_file.original_name,
        storage_backend=stored_file.storage_backend,
        storage_key=stored_file.storage_key,
        content_type=stored_file.content_type,
        size_bytes=stored_file.size_bytes,
        sha256=stored_file.sha256,
    )


def _load_source_dto(session, source_id: str, owner_id: str) -> WorkflowSourceItem:
    row = session.execute(
        select(WorkflowSourceItemRecord, StoredFileRecord)
        .join(
            StoredFileRecord,
            StoredFileRecord.id == WorkflowSourceItemRecord.stored_file_id,
        )
        .where(
            WorkflowSourceItemRecord.id == source_id,
            WorkflowSourceItemRecord.owner_id == owner_id,
            StoredFileRecord.owner_id == owner_id,
        )
    ).one_or_none()
    if row is None:
        raise NotFound("workflow_source")
    return _source_dto(row[0], row[1])


def _same_registration(
    row: WorkflowSourceItemRecord,
    *,
    stored_file_id: str,
    retry_of_source_id: str | None,
) -> bool:
    return (
        row.stored_file_id == stored_file_id
        and row.retry_of_source_id == retry_of_source_id
    )


def _outcome_dto(row: WorkflowSourceOutcomeRecord) -> WorkflowSourceOutcome:
    return WorkflowSourceOutcome(
        source_id=row.source_id,
        status=row.status,
        student_candidate=row.student_candidate,
        matched_answer_count=row.matched_answer_count,
        unknown_question_ids=tuple(row.unknown_question_ids),
        stable_error_code=row.stable_error_code,
        retryable=row.retryable,
        artifact_file_id=row.artifact_file_id,
        created_at=row.created_at,
    )


def _same_outcome(
    row: WorkflowSourceOutcomeRecord,
    *,
    status: str,
    student_candidate: str | None,
    matched_answer_count: int,
    unknown_question_ids: list[str],
    stable_error_code: str | None,
    retryable: bool,
    artifact_file_id: str | None,
) -> bool:
    return (
        row.status == status
        and row.student_candidate == student_candidate
        and row.matched_answer_count == matched_answer_count
        and row.unknown_question_ids == unknown_question_ids
        and row.stable_error_code == stable_error_code
        and row.retryable is retryable
        and row.artifact_file_id == artifact_file_id
    )


def _validate_outcome_evidence(
    *,
    status: str,
    matched_answer_count: int,
    unknown_question_ids: list[str],
) -> None:
    if status not in OUTCOME_STATUSES:
        raise ValidationError("Invalid workflow source outcome status.")
    if matched_answer_count < 0:
        raise ValidationError("Matched answer count must be non-negative.")
    if not isinstance(unknown_question_ids, list):
        raise ValidationError("Unknown question IDs must be a list.")
    if len(unknown_question_ids) > MAX_UNKNOWN_QUESTION_IDS:
        raise ValidationError("Too many unknown question IDs.")
    if any(not isinstance(question_id, str) for question_id in unknown_question_ids):
        raise ValidationError("Unknown question IDs must be strings.")
    if any(len(question_id) > MAX_QUESTION_ID_LENGTH for question_id in unknown_question_ids):
        raise ValidationError("Unknown question ID is too long.")
    encoded = json.dumps(
        unknown_question_ids,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_UNKNOWN_QUESTION_IDS_JSON_BYTES:
        raise ValidationError("Unknown question IDs exceed the storage bound.")


def _owned_operation_for_update_statement(
    *,
    operation_id: str,
    assignment_id: str,
    owner_id: str,
):
    return (
        select(WorkflowOperationRecord)
        .where(
            WorkflowOperationRecord.id == operation_id,
            WorkflowOperationRecord.assignment_id == assignment_id,
            WorkflowOperationRecord.owner_id == owner_id,
        )
        .with_for_update()
    )


def register_source(
    *,
    owner_id: str,
    assignment_id: str,
    operation_id: str,
    expected_attempt: int,
    order_index: int,
    stored_file_id: str,
    retry_of_source_id: str | None = None,
) -> tuple[WorkflowSourceItem, bool]:
    if expected_attempt <= 0 or order_index < 0:
        raise ValidationError("Source attempt must be positive and order non-negative.")

    try:
        with session_scope() as session:
            assignment = session.scalar(select(AssignmentRecord.id).where(
                AssignmentRecord.id == assignment_id,
                AssignmentRecord.teacher_id == owner_id,
            ))
            if assignment is None:
                raise NotFound("workflow_source")

            operation = session.scalar(
                _owned_operation_for_update_statement(
                    operation_id=operation_id,
                    assignment_id=assignment_id,
                    owner_id=owner_id,
                )
            )
            if operation is None:
                raise NotFound("workflow_source")
            if operation.attempt != expected_attempt:
                raise VersionConflict(
                    "A newer workflow operation attempt is active.",
                    code="stale_operation_attempt",
                )

            stored_file = session.scalar(select(StoredFileRecord).where(
                StoredFileRecord.id == stored_file_id,
                StoredFileRecord.owner_id == owner_id,
                StoredFileRecord.assignment_id == assignment_id,
            ))
            if stored_file is None:
                raise NotFound("workflow_source")
            if not stored_file.content_type or not stored_file.content_type.strip():
                raise ValidationError("Workflow source requires a persisted MIME type.")

            if retry_of_source_id is not None:
                retry_source = session.scalar(select(WorkflowSourceItemRecord.id).where(
                    WorkflowSourceItemRecord.id == retry_of_source_id,
                    WorkflowSourceItemRecord.owner_id == owner_id,
                    WorkflowSourceItemRecord.operation_id == operation_id,
                    WorkflowSourceItemRecord.attempt < expected_attempt,
                ))
                if retry_source is None:
                    raise NotFound("workflow_source")

            existing = session.scalar(select(WorkflowSourceItemRecord).where(
                WorkflowSourceItemRecord.operation_id == operation_id,
                WorkflowSourceItemRecord.attempt == expected_attempt,
                WorkflowSourceItemRecord.order_index == order_index,
                WorkflowSourceItemRecord.owner_id == owner_id,
            ))
            if existing is not None:
                if not _same_registration(
                    existing,
                    stored_file_id=stored_file_id,
                    retry_of_source_id=retry_of_source_id,
                ):
                    raise VersionConflict("Workflow source position already exists.")
                return _source_dto(existing, stored_file), False

            row = WorkflowSourceItemRecord(
                id=f"src_{uuid.uuid4().hex}",
                owner_id=owner_id,
                assignment_id=assignment_id,
                operation_id=operation_id,
                attempt=expected_attempt,
                order_index=order_index,
                stored_file_id=stored_file_id,
                retry_of_source_id=retry_of_source_id,
                created_at=time.time(),
            )
            session.add(row)
            session.flush()
            return _source_dto(row, stored_file), True
    except IntegrityError:
        with session_scope() as session:
            existing = session.scalar(select(WorkflowSourceItemRecord).where(
                WorkflowSourceItemRecord.operation_id == operation_id,
                WorkflowSourceItemRecord.attempt == expected_attempt,
                WorkflowSourceItemRecord.order_index == order_index,
                WorkflowSourceItemRecord.owner_id == owner_id,
            ))
            if existing is None or not _same_registration(
                existing,
                stored_file_id=stored_file_id,
                retry_of_source_id=retry_of_source_id,
            ):
                raise VersionConflict("Workflow source position already exists.")
            stored_file = session.scalar(select(StoredFileRecord).where(
                StoredFileRecord.id == existing.stored_file_id,
                StoredFileRecord.owner_id == owner_id,
            ))
            assert stored_file is not None
            return _source_dto(existing, stored_file), False


def get_source(source_id: str, *, owner_id: str) -> WorkflowSourceItem:
    with session_scope() as session:
        return _load_source_dto(session, source_id, owner_id)


def _require_owned_operation(session, operation_id: str, owner_id: str) -> None:
    operation = session.scalar(select(WorkflowOperationRecord.id).where(
        WorkflowOperationRecord.id == operation_id,
        WorkflowOperationRecord.owner_id == owner_id,
    ))
    if operation is None:
        raise NotFound("workflow_source")


def list_sources(
    *,
    operation_id: str,
    owner_id: str,
    attempt: int,
) -> list[WorkflowSourceItem]:
    with session_scope() as session:
        _require_owned_operation(session, operation_id, owner_id)
        rows = session.execute(
            select(WorkflowSourceItemRecord, StoredFileRecord)
            .join(
                StoredFileRecord,
                StoredFileRecord.id == WorkflowSourceItemRecord.stored_file_id,
            )
            .where(
                WorkflowSourceItemRecord.operation_id == operation_id,
                WorkflowSourceItemRecord.owner_id == owner_id,
                WorkflowSourceItemRecord.attempt == attempt,
                StoredFileRecord.owner_id == owner_id,
            )
            .order_by(WorkflowSourceItemRecord.order_index)
        ).all()
        return [_source_dto(source, stored_file) for source, stored_file in rows]


def summarize_sources(
    *,
    operation_id: str,
    owner_id: str,
    attempt: int,
) -> WorkflowSourceSummary:
    with session_scope() as session:
        _require_owned_operation(session, operation_id, owner_id)
        statuses = session.scalars(
            select(WorkflowSourceOutcomeRecord.status)
            .select_from(WorkflowSourceItemRecord)
            .outerjoin(
                WorkflowSourceOutcomeRecord,
                WorkflowSourceOutcomeRecord.source_id == WorkflowSourceItemRecord.id,
            )
            .where(
                WorkflowSourceItemRecord.operation_id == operation_id,
                WorkflowSourceItemRecord.owner_id == owner_id,
                WorkflowSourceItemRecord.attempt == attempt,
            )
        ).all()

    return WorkflowSourceSummary(
        uploaded_count=len(statuses),
        success_count=sum(status == "parsed" for status in statuses),
        failed_count=sum(
            status in {"parse_failed", "no_matching_answer"}
            for status in statuses
        ),
        conflict_count=sum(status == "identity_conflict" for status in statuses),
        pending_count=sum(status is None for status in statuses),
    )


def get_outcome(
    source_id: str,
    *,
    owner_id: str,
) -> WorkflowSourceOutcome | None:
    with session_scope() as session:
        row = session.scalar(
            select(WorkflowSourceOutcomeRecord)
            .join(
                WorkflowSourceItemRecord,
                WorkflowSourceItemRecord.id == WorkflowSourceOutcomeRecord.source_id,
            )
            .where(
                WorkflowSourceOutcomeRecord.source_id == source_id,
                WorkflowSourceItemRecord.owner_id == owner_id,
            )
        )
        return _outcome_dto(row) if row is not None else None


def record_outcome(
    *,
    source_id: str,
    owner_id: str,
    status: str,
    student_candidate: str | None,
    matched_answer_count: int,
    unknown_question_ids: list[str],
    stable_error_code: str | None,
    retryable: bool,
    artifact_file_id: str | None = None,
) -> tuple[WorkflowSourceOutcome, bool]:
    with session_scope() as session:
        source = session.scalar(select(WorkflowSourceItemRecord).where(
            WorkflowSourceItemRecord.id == source_id,
            WorkflowSourceItemRecord.owner_id == owner_id,
        ))
        if source is None:
            raise NotFound("workflow_source")

        _validate_outcome_evidence(
            status=status,
            matched_answer_count=matched_answer_count,
            unknown_question_ids=unknown_question_ids,
        )

        if artifact_file_id is not None:
            artifact = session.scalar(select(StoredFileRecord.id).where(
                StoredFileRecord.id == artifact_file_id,
                StoredFileRecord.owner_id == owner_id,
                StoredFileRecord.assignment_id == source.assignment_id,
            ))
            if artifact is None:
                raise NotFound("workflow_source")

        existing = session.get(WorkflowSourceOutcomeRecord, source_id)
        if existing is not None:
            if not _same_outcome(
                existing,
                status=status,
                student_candidate=student_candidate,
                matched_answer_count=matched_answer_count,
                unknown_question_ids=unknown_question_ids,
                stable_error_code=stable_error_code,
                retryable=retryable,
                artifact_file_id=artifact_file_id,
            ):
                raise VersionConflict("Workflow source outcome already exists.")
            return _outcome_dto(existing), False

        row = WorkflowSourceOutcomeRecord(
            source_id=source_id,
            status=status,
            student_candidate=student_candidate,
            matched_answer_count=matched_answer_count,
            unknown_question_ids=list(unknown_question_ids),
            stable_error_code=stable_error_code,
            retryable=retryable,
            artifact_file_id=artifact_file_id,
            created_at=time.time(),
        )
        session.add(row)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            existing = session.scalar(
                select(WorkflowSourceOutcomeRecord)
                .join(
                    WorkflowSourceItemRecord,
                    WorkflowSourceItemRecord.id
                    == WorkflowSourceOutcomeRecord.source_id,
                )
                .where(
                    WorkflowSourceOutcomeRecord.source_id == source_id,
                    WorkflowSourceItemRecord.owner_id == owner_id,
                )
            )
            if existing is None:
                raise NotFound("workflow_source")
            if not _same_outcome(
                existing,
                status=status,
                student_candidate=student_candidate,
                matched_answer_count=matched_answer_count,
                unknown_question_ids=unknown_question_ids,
                stable_error_code=stable_error_code,
                retryable=retryable,
                artifact_file_id=artifact_file_id,
            ):
                raise VersionConflict("Workflow source outcome already exists.")
            return _outcome_dto(existing), False
        return _outcome_dto(row), True
