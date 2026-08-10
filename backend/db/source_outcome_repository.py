"""Owner-scoped workflow sources and immutable per-file terminal outcomes."""
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
            "operation_id", "attempt", "order_index",
            name="uq_workflow_source_items_operation_attempt_order",
        ),
        UniqueConstraint(
            "operation_id", "attempt", "stored_file_id",
            name="uq_workflow_source_items_operation_attempt_file",
        ),
        CheckConstraint("attempt > 0", name="ck_workflow_source_items_attempt_positive"),
        CheckConstraint("order_index >= 0", name="ck_workflow_source_items_order_nonnegative"),
        Index(
            "ix_workflow_source_items_assignment_operation_attempt_order",
            "assignment_id", "operation_id", "attempt", "order_index",
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
        ForeignKey("workflow_source_items.id", ondelete="SET NULL"), nullable=True, index=True
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
    failure_phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    content_type: str
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
    failure_phase: str | None
    retryable: bool
    artifact_file_id: str | None
    created_at: float


@dataclass(frozen=True)
class WorkflowSourceResult:
    source: WorkflowSourceItem
    outcome: WorkflowSourceOutcome | None


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


def _source_dto(source: WorkflowSourceItemRecord, stored: StoredFileRecord) -> WorkflowSourceItem:
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
        original_name=stored.original_name,
        content_type=stored.content_type or "application/octet-stream",
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
    )


def _outcome_dto(row: WorkflowSourceOutcomeRecord) -> WorkflowSourceOutcome:
    return WorkflowSourceOutcome(
        source_id=row.source_id,
        status=row.status,
        student_candidate=row.student_candidate,
        matched_answer_count=row.matched_answer_count,
        unknown_question_ids=tuple(row.unknown_question_ids or []),
        stable_error_code=row.stable_error_code,
        failure_phase=row.failure_phase,
        retryable=row.retryable,
        artifact_file_id=row.artifact_file_id,
        created_at=row.created_at,
    )


def _require_owned_operation(session, operation_id: str, owner_id: str) -> WorkflowOperationRecord:
    operation = session.scalar(
        select(WorkflowOperationRecord).where(
            WorkflowOperationRecord.id == operation_id,
            WorkflowOperationRecord.owner_id == owner_id,
        )
    )
    if operation is None:
        raise NotFound("workflow_source")
    return operation


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
        raise ValidationError("Source attempt and order are invalid.")

    try:
        with session_scope() as session:
            assignment = session.scalar(
                select(AssignmentRecord.id).where(
                    AssignmentRecord.id == assignment_id,
                    AssignmentRecord.teacher_id == owner_id,
                )
            )
            if assignment is None:
                raise NotFound("workflow_source")
            operation = _require_owned_operation(session, operation_id, owner_id)
            if operation.assignment_id != assignment_id:
                raise NotFound("workflow_source")
            if operation.attempt != expected_attempt:
                raise VersionConflict(
                    "A newer workflow operation attempt is active.",
                    code="stale_operation_attempt",
                )
            stored = session.scalar(
                select(StoredFileRecord).where(
                    StoredFileRecord.id == stored_file_id,
                    StoredFileRecord.owner_id == owner_id,
                    StoredFileRecord.assignment_id == assignment_id,
                )
            )
            if stored is None:
                raise NotFound("workflow_source")
            if not stored.content_type or not stored.content_type.strip():
                raise ValidationError("Workflow source requires a persisted MIME type.")

            if retry_of_source_id is not None:
                previous = session.scalar(
                    select(WorkflowSourceItemRecord.id).where(
                        WorkflowSourceItemRecord.id == retry_of_source_id,
                        WorkflowSourceItemRecord.owner_id == owner_id,
                        WorkflowSourceItemRecord.operation_id == operation_id,
                        WorkflowSourceItemRecord.attempt < expected_attempt,
                    )
                )
                if previous is None:
                    raise NotFound("workflow_source")

            existing = session.scalar(
                select(WorkflowSourceItemRecord).where(
                    WorkflowSourceItemRecord.operation_id == operation_id,
                    WorkflowSourceItemRecord.attempt == expected_attempt,
                    WorkflowSourceItemRecord.order_index == order_index,
                    WorkflowSourceItemRecord.owner_id == owner_id,
                )
            )
            if existing is not None:
                if (
                    existing.stored_file_id != stored_file_id
                    or existing.retry_of_source_id != retry_of_source_id
                ):
                    raise VersionConflict("Workflow source position already exists.")
                return _source_dto(existing, stored), False

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
            return _source_dto(row, stored), True
    except IntegrityError as exc:
        raise VersionConflict("Workflow source position already exists.") from exc


def list_source_results(
    *, operation_id: str, owner_id: str, attempt: int,
) -> list[WorkflowSourceResult]:
    with session_scope() as session:
        _require_owned_operation(session, operation_id, owner_id)
        rows = session.execute(
            select(
                WorkflowSourceItemRecord,
                StoredFileRecord,
                WorkflowSourceOutcomeRecord,
            )
            .join(StoredFileRecord, StoredFileRecord.id == WorkflowSourceItemRecord.stored_file_id)
            .outerjoin(
                WorkflowSourceOutcomeRecord,
                WorkflowSourceOutcomeRecord.source_id == WorkflowSourceItemRecord.id,
            )
            .where(
                WorkflowSourceItemRecord.operation_id == operation_id,
                WorkflowSourceItemRecord.owner_id == owner_id,
                WorkflowSourceItemRecord.attempt == attempt,
                StoredFileRecord.owner_id == owner_id,
            )
            .order_by(WorkflowSourceItemRecord.order_index)
        ).all()
        return [
            WorkflowSourceResult(
                source=_source_dto(source, stored),
                outcome=_outcome_dto(outcome) if outcome is not None else None,
            )
            for source, stored, outcome in rows
        ]


def summarize_sources(
    *, operation_id: str, owner_id: str, attempt: int,
) -> WorkflowSourceSummary:
    results = list_source_results(
        operation_id=operation_id, owner_id=owner_id, attempt=attempt
    )
    statuses = [item.outcome.status if item.outcome else None for item in results]
    return WorkflowSourceSummary(
        uploaded_count=len(statuses),
        success_count=sum(status == "parsed" for status in statuses),
        failed_count=sum(status in {"parse_failed", "no_matching_answer"} for status in statuses),
        conflict_count=sum(status == "identity_conflict" for status in statuses),
        pending_count=sum(status is None for status in statuses),
    )


def _validate_evidence(
    status: str, matched_answer_count: int, unknown_question_ids: list[str]
) -> None:
    if status not in OUTCOME_STATUSES:
        raise ValidationError("Invalid workflow source outcome status.")
    if matched_answer_count < 0:
        raise ValidationError("Matched answer count must be non-negative.")
    if not isinstance(unknown_question_ids, list):
        raise ValidationError("Unknown question IDs must be a list.")
    if len(unknown_question_ids) > MAX_UNKNOWN_QUESTION_IDS:
        raise ValidationError("Too many unknown question IDs.")
    if any(not isinstance(value, str) for value in unknown_question_ids):
        raise ValidationError("Unknown question IDs must be strings.")
    if any(len(value) > MAX_QUESTION_ID_LENGTH for value in unknown_question_ids):
        raise ValidationError("Unknown question ID is too long.")
    encoded = json.dumps(unknown_question_ids, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_UNKNOWN_QUESTION_IDS_JSON_BYTES:
        raise ValidationError("Unknown question IDs exceed the storage bound.")


def record_outcome(
    *,
    source_id: str,
    owner_id: str,
    status: str,
    student_candidate: str | None,
    matched_answer_count: int,
    unknown_question_ids: list[str],
    stable_error_code: str | None,
    failure_phase: str | None,
    retryable: bool,
    artifact_file_id: str | None = None,
) -> tuple[WorkflowSourceOutcome, bool]:
    _validate_evidence(status, matched_answer_count, unknown_question_ids)
    with session_scope() as session:
        source = session.scalar(
            select(WorkflowSourceItemRecord).where(
                WorkflowSourceItemRecord.id == source_id,
                WorkflowSourceItemRecord.owner_id == owner_id,
            )
        )
        if source is None:
            raise NotFound("workflow_source")
        if artifact_file_id is not None:
            artifact = session.scalar(
                select(StoredFileRecord.id).where(
                    StoredFileRecord.id == artifact_file_id,
                    StoredFileRecord.owner_id == owner_id,
                    StoredFileRecord.assignment_id == source.assignment_id,
                )
            )
            if artifact is None:
                raise NotFound("workflow_source")

        expected = {
            "status": status,
            "student_candidate": student_candidate,
            "matched_answer_count": matched_answer_count,
            "unknown_question_ids": list(unknown_question_ids),
            "stable_error_code": stable_error_code,
            "failure_phase": failure_phase,
            "retryable": retryable,
            "artifact_file_id": artifact_file_id,
        }
        existing = session.get(WorkflowSourceOutcomeRecord, source_id)
        if existing is not None:
            if any(getattr(existing, key) != value for key, value in expected.items()):
                raise VersionConflict("Workflow source outcome already exists.")
            return _outcome_dto(existing), False

        row = WorkflowSourceOutcomeRecord(
            source_id=source_id,
            **expected,
            created_at=time.time(),
        )
        session.add(row)
        try:
            session.flush()
        except IntegrityError as exc:
            raise VersionConflict("Workflow source outcome already exists.") from exc
        return _outcome_dto(row), True
