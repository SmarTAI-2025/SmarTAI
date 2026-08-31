"""Persistent presentation-workflow metadata over the normalized domain.

The Figma application speaks in terms of a teacher-facing ``Task``.  The
normalized backend deliberately has no Task aggregate: an assignment,
questions, submissions, grading runs, results, and reviews are their own rows.
This module stores only the small amount of presentation state that cannot be
derived from those rows (semester, active operation, review/display metadata,
and immutable grading-setup snapshots).

Core education data must never be copied into these JSON columns.  Temporary
operation payloads are bounded staging data and may be discarded after their
TTL; they are not a source of truth for confirmed questions or submissions.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterable

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    and_,
    delete,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base
# Import the normalized records before configuring string-based foreign keys.
from backend.db.models import (  # noqa: F401
    AssignmentRecord,
    GradeResultRecord,
    GradingRunRecord,
    StoredFileRecord,
)
from backend.db.session import session_scope
from backend.domain import education
from backend.domain.errors import (
    InvalidTransition,
    LeaseLost,
    NotFound,
    ResultNotReleasable,
    ValidationError,
    VersionConflict,
)
from backend.domain.source_storage import (
    DELAYED_SOURCE_OPERATION_TYPES,
    SOURCE_RESERVATION_CLEANUP_OPERATION,
    TASK_DELETE_OPERATION,
)


MAX_OPERATION_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_OPERATION_PROGRESS_BYTES = 64 * 1024
MAX_OPERATION_CHECKPOINT_BYTES = 64 * 1024
MAX_OPERATION_TERMINAL_SUMMARY_BYTES = 16 * 1024
MAX_OPERATION_ARTIFACT_REFS = 256
MAX_OPERATION_ARTIFACT_REF_LENGTH = 64
MAX_OPERATION_ARTIFACT_REFS_BYTES = 24 * 1024
MAX_OPERATION_CLAIM_BATCH = 100
MAX_LEASE_OWNER_LENGTH = 128
MAX_LEASE_TOKEN_LENGTH = 64


def _validate_json_object(
    value: Any,
    *,
    field: str,
    max_bytes: int,
) -> dict:
    if not isinstance(value, dict):
        raise ValidationError(
            f"Operation {field} must be a JSON object.",
            code=f"invalid_operation_{field}",
        )
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"Operation {field} must contain finite JSON values.",
            code=f"invalid_operation_{field}",
        ) from exc
    if len(encoded) > max_bytes:
        raise ValidationError(
            f"Operation {field} exceeds its storage limit.",
            code=f"operation_{field}_too_large",
        )
    return value


def _validate_checkpoint_stage(value: Any) -> str | None:
    if value is not None and (not isinstance(value, str) or len(value) > 64):
        raise ValidationError(
            "Checkpoint stage must be null or a string of at most 64 characters.",
            code="invalid_checkpoint_stage",
        )
    return value


def _validate_artifact_refs(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError(
            "Operation artifact references must be a list.",
            code="invalid_operation_artifact_refs",
        )
    if len(value) > MAX_OPERATION_ARTIFACT_REFS:
        raise ValidationError(
            "Operation artifact references exceed their storage limit.",
            code="operation_artifact_refs_too_large",
        )
    refs: list[str] = []
    seen: set[str] = set()
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > MAX_OPERATION_ARTIFACT_REF_LENGTH
        ):
            raise ValidationError(
                "Operation artifact references must contain stable file IDs.",
                code="invalid_operation_artifact_refs",
            )
        if item not in seen:
            refs.append(item)
            seen.add(item)
    encoded = json.dumps(refs, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_OPERATION_ARTIFACT_REFS_BYTES:
        raise ValidationError(
            "Operation artifact references exceed their storage limit.",
            code="operation_artifact_refs_too_large",
        )
    return refs


class AssignmentWorkflowRecord(Base):
    __tablename__ = "assignment_workflows"
    __table_args__ = (
        CheckConstraint(
            "source_lifecycle_epoch >= 0",
            name="ck_assignment_workflows_source_lifecycle_epoch_nonnegative",
        ),
    )

    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), primary_key=True
    )
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    semester_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    presentation_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="draft", index=True
    )
    workflow_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_operation: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    extract_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parse_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    grading_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_failed_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    problem_file_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    submission_file_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    pending_submission_file_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    submission_identity_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="filename"
    )
    submission_roster_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    question_recognition_provider_id: Mapped[str | None] = mapped_column(
        String(240), nullable=True
    )
    submission_recognition_provider_id: Mapped[str | None] = mapped_column(
        String(240), nullable=True
    )
    reference_file_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    test_cases_file_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    grading_setup: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    grading_setup_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    grading_setup_updated_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_result_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    final_result_updated_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Monotonic fence shared by raw-source reservations/publication and final
    # result cleanup. Finalization advances it before taking its source
    # snapshot, so a pre-finalization upload cannot publish late and escape the
    # cleanup manifest.
    source_lifecycle_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    analysis_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_generated"
    )
    analysis_result_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    analysis_generated_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    analysis_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)


class TaskCreateIdempotencyRecord(Base):
    """Maps a caller key to the normalized assignment created for it."""

    __tablename__ = "task_create_idempotency"
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "idempotency_key",
            name="uq_task_create_idempotency_owner_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)


class WorkflowOperationRecord(Base):
    """Durable, bounded state for extraction/import/generation operations."""

    __tablename__ = "workflow_operations"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id", "operation_type", "input_hash",
            name="uq_workflow_operations_assignment_type_hash",
        ),
        Index("ix_workflow_operations_assignment_status", "assignment_id", "status"),
        Index("ix_workflow_operations_claimable", "status", "lease_expires_at"),
        CheckConstraint(
            "checkpoint_revision >= 0",
            name="ck_workflow_operations_checkpoint_revision_nonnegative",
        ),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_token IS NULL"
            " AND lease_expires_at IS NULL AND lease_heartbeat_at IS NULL)"
            " OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL"
            " AND lease_expires_at IS NOT NULL AND lease_heartbeat_at IS NOT NULL)",
            name="ck_workflow_operations_lease_consistency",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    progress: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Temporary source descriptors/candidates only. Confirmed data is written
    # to assignment_questions/submission_* and then removed from this payload.
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    checkpoint_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    checkpoint_stage: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    checkpoint: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    artifact_refs: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    terminal_summary: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    # Operation lease fencing. Only the worker holding the current
    # (lease_owner, lease_token) with a live lease_expires_at may heartbeat,
    # checkpoint, write terminal state, or release. lease_token is random and
    # rotates on every claim/reclaim, so a fenced worker's old token is
    # rejected by predicate. owner/token are null together; an active lease
    # always has an expiry.
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    lease_heartbeat_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class AssignmentStudentPresentationRecord(Base):
    __tablename__ = "assignment_student_presentations"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id", "student_id",
            name="uq_assignment_student_presentations_assignment_student",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_source_items.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )
    display_student_id: Mapped[str] = mapped_column(String(160), nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    source_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    identity_match_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    identity_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="needs_review"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)


class SubmissionAnswerPresentationRecord(Base):
    __tablename__ = "submission_answer_presentations"

    answer_id: Mapped[str] = mapped_column(
        ForeignKey("submission_answers.id", ondelete="CASCADE"), primary_key=True
    )
    review_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )
    updated_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)


class GradingRunSetupRecord(Base):
    """Immutable teacher-approved setup frozen when a run is created."""

    __tablename__ = "grading_run_setups"

    grading_run_id: Mapped[str] = mapped_column(
        ForeignKey("grading_runs.id", ondelete="CASCADE"), primary_key=True
    )
    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    setup: Mapped[dict] = mapped_column(JSON, nullable=False)
    input_manifest: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)


class ResultArtifactManifestRecord(Base):
    """Metadata for deterministic artifacts; bytes are rebuilt from results."""

    __tablename__ = "result_artifact_manifests"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id", "result_version",
            name="uq_result_artifact_manifests_assignment_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    grading_run_id: Mapped[str] = mapped_column(
        ForeignKey("grading_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    result_version: Mapped[int] = mapped_column(Integer, nullable=False)
    result_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False)
    generated_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _new_lease_token() -> str:
    return uuid.uuid4().hex


def _validate_worker_id(worker_id: str) -> str:
    if (
        not isinstance(worker_id, str)
        or not worker_id
        or len(worker_id) > MAX_LEASE_OWNER_LENGTH
    ):
        raise ValidationError(
            "Worker ID must be a non-empty string of at most "
            f"{MAX_LEASE_OWNER_LENGTH} characters.",
            code="invalid_worker_id",
        )
    return worker_id


def _validate_lease_token(lease_token: str | None) -> str | None:
    if lease_token is None:
        return None
    if (
        not isinstance(lease_token, str)
        or not lease_token
        or len(lease_token) > MAX_LEASE_TOKEN_LENGTH
    ):
        raise ValidationError(
            "Lease token must be a non-empty string of at most "
            f"{MAX_LEASE_TOKEN_LENGTH} characters.",
            code="invalid_lease_token",
        )
    return lease_token


def _lease_write_predicate(expected_lease_token: str | None, now: float):
    if expected_lease_token is None:
        # Legacy non-worker writes are allowed only on rows with no active
        # lease; a live lease fences any writer that cannot present its token.
        return WorkflowOperationRecord.lease_owner.is_(None)
    return and_(
        WorkflowOperationRecord.lease_token == expected_lease_token,
        WorkflowOperationRecord.lease_expires_at >= now,
    )


def _lease_allows_write(
    record: WorkflowOperationRecord,
    expected_lease_token: str | None,
    now: float,
) -> bool:
    if expected_lease_token is None:
        return record.lease_owner is None
    return (
        record.lease_token == expected_lease_token
        and record.lease_expires_at is not None
        and record.lease_expires_at >= now
    )


def get_create_idempotency(
    *, owner_id: str, idempotency_key: str
) -> TaskCreateIdempotencyRecord | None:
    with session_scope() as session:
        row = session.scalar(
            select(TaskCreateIdempotencyRecord).where(
                TaskCreateIdempotencyRecord.owner_id == owner_id,
                TaskCreateIdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
        return _copy_record(row, TaskCreateIdempotencyRecord) if row is not None else None


def save_create_idempotency(
    *, owner_id: str, idempotency_key: str, request_hash: str,
    assignment_id: str,
) -> TaskCreateIdempotencyRecord:
    with session_scope() as session:
        existing = session.scalar(
            select(TaskCreateIdempotencyRecord).where(
                TaskCreateIdempotencyRecord.owner_id == owner_id,
                TaskCreateIdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise VersionConflict("idempotency_key_reused")
            return _copy_record(existing, TaskCreateIdempotencyRecord)
        row = TaskCreateIdempotencyRecord(
            id=_new_id("idem"), owner_id=owner_id,
            idempotency_key=idempotency_key, request_hash=request_hash,
            assignment_id=assignment_id, created_at=time.time(),
        )
        session.add(row)
        session.flush()
        return _copy_record(row, TaskCreateIdempotencyRecord)


def ensure_workflow(
    *, assignment_id: str, owner_id: str, semester_id: str | None = None
) -> AssignmentWorkflowRecord:
    now = time.time()
    with session_scope() as session:
        row = session.get(AssignmentWorkflowRecord, assignment_id)
        if row is None:
            row = AssignmentWorkflowRecord(
                assignment_id=assignment_id,
                owner_id=owner_id,
                semester_id=semester_id,
                presentation_status="draft",
                workflow_revision=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
        elif row.owner_id != owner_id:
            raise NotFound("workflow")
        return _detach_workflow(row)


def get_workflow(
    assignment_id: str, *, owner_id: str
) -> AssignmentWorkflowRecord:
    with session_scope() as session:
        row = session.scalar(
            select(AssignmentWorkflowRecord).where(
                AssignmentWorkflowRecord.assignment_id == assignment_id,
                AssignmentWorkflowRecord.owner_id == owner_id,
            )
        )
        if row is None:
            raise NotFound("workflow")
        return _detach_workflow(row)


def get_live_workflow(
    assignment_id: str, *, owner_id: str
) -> AssignmentWorkflowRecord:
    """Owner-scoped workflow read that hides a task deletion tombstone."""
    with session_scope() as session:
        row = session.scalar(
            select(AssignmentWorkflowRecord)
            .join(
                AssignmentRecord,
                AssignmentRecord.id == AssignmentWorkflowRecord.assignment_id,
            )
            .where(
                AssignmentWorkflowRecord.assignment_id == assignment_id,
                AssignmentWorkflowRecord.owner_id == owner_id,
                AssignmentRecord.teacher_id == owner_id,
                AssignmentRecord.deletion_requested_at.is_(None),
            )
        )
        if row is None:
            raise NotFound("workflow")
        return _detach_workflow(row)


def _lock_live_assignment(
    session, *, assignment_id: str, owner_id: str
) -> AssignmentRecord:
    row = session.scalar(
        select(AssignmentRecord)
        .where(
            AssignmentRecord.id == assignment_id,
            AssignmentRecord.teacher_id == owner_id,
            AssignmentRecord.deletion_requested_at.is_(None),
        )
        .with_for_update()
    )
    if row is None:
        raise NotFound("assignment")
    return row


def list_workflows(owner_id: str) -> list[AssignmentWorkflowRecord]:
    with session_scope() as session:
        rows = session.scalars(
            select(AssignmentWorkflowRecord)
            .where(AssignmentWorkflowRecord.owner_id == owner_id)
            .order_by(AssignmentWorkflowRecord.updated_at.desc())
        ).all()
        return [_detach_workflow(row) for row in rows]


def update_workflow(
    assignment_id: str,
    *,
    owner_id: str,
    expected_revision: int | None = None,
    bump_revision: bool = True,
    **changes: Any,
) -> AssignmentWorkflowRecord:
    allowed = {
        column.name
        for column in AssignmentWorkflowRecord.__table__.columns
        if column.name not in {"assignment_id", "owner_id", "created_at", "workflow_revision"}
    }
    values = {key: value for key, value in changes.items() if key in allowed}
    now = time.time()
    with session_scope() as session:
        if expected_revision is not None:
            atomic_values: dict[str, Any] = {**values, "updated_at": now}
            if bump_revision:
                atomic_values["workflow_revision"] = (
                    AssignmentWorkflowRecord.workflow_revision + 1
                )
            result = session.execute(
                update(AssignmentWorkflowRecord)
                .where(
                    AssignmentWorkflowRecord.assignment_id == assignment_id,
                    AssignmentWorkflowRecord.owner_id == owner_id,
                    AssignmentWorkflowRecord.workflow_revision == expected_revision,
                )
                .values(**atomic_values)
            )
            if result.rowcount != 1:
                exists = session.scalar(
                    select(AssignmentWorkflowRecord.assignment_id).where(
                        AssignmentWorkflowRecord.assignment_id == assignment_id,
                        AssignmentWorkflowRecord.owner_id == owner_id,
                    )
                )
                if exists is None:
                    raise NotFound("workflow")
                raise VersionConflict("workflow_revision_conflict")
            # The UPDATE above owns W. Take live A before commit so every
            # public workflow mutation serializes with task deletion as W -> A.
            _lock_live_assignment(
                session, assignment_id=assignment_id, owner_id=owner_id
            )
            row = session.get(AssignmentWorkflowRecord, assignment_id)
            assert row is not None
            return _detach_workflow(row)

        atomic_values = {**values, "updated_at": now}
        if bump_revision:
            atomic_values["workflow_revision"] = (
                AssignmentWorkflowRecord.workflow_revision + 1
            )
        result = session.execute(
            update(AssignmentWorkflowRecord)
            .where(
                AssignmentWorkflowRecord.assignment_id == assignment_id,
                AssignmentWorkflowRecord.owner_id == owner_id,
            )
            .values(**atomic_values)
        )
        if result.rowcount != 1:
            raise NotFound("workflow")
        _lock_live_assignment(
            session, assignment_id=assignment_id, owner_id=owner_id
        )
        row = session.get(AssignmentWorkflowRecord, assignment_id)
        assert row is not None
        return _detach_workflow(row)


def update_workflow_if_active_job(
    assignment_id: str,
    *,
    owner_id: str,
    active_job_id: str,
    **changes: Any,
) -> AssignmentWorkflowRecord | None:
    """Update a workflow only while it still points at ``active_job_id``.

    Recovery paths use this compare-and-set helper so a late terminal worker
    can never clear a newer operation that was claimed for the same task.
    Recovery metadata does not change the teacher-authored workflow revision.
    """
    allowed = {
        column.name
        for column in AssignmentWorkflowRecord.__table__.columns
        if column.name not in {
            "assignment_id", "owner_id", "created_at", "workflow_revision",
            "active_job_id",
        }
    }
    values = {key: value for key, value in changes.items() if key in allowed}
    now = time.time()
    with session_scope() as session:
        result = session.execute(
            update(AssignmentWorkflowRecord)
            .where(
                AssignmentWorkflowRecord.assignment_id == assignment_id,
                AssignmentWorkflowRecord.owner_id == owner_id,
                AssignmentWorkflowRecord.active_job_id == active_job_id,
            )
            .values(**values, active_job_id=None, updated_at=now)
        )
        if result.rowcount != 1:
            return None
        row = session.get(AssignmentWorkflowRecord, assignment_id)
        assert row is not None
        return _detach_workflow(row)


def bind_existing_active_grading_run(
    assignment_id: str,
    *,
    owner_id: str,
    run_id: str,
) -> AssignmentWorkflowRecord:
    """Repair a legacy active run that committed before its workflow pointer.

    New grading starts bind the run in the creation transaction.  This helper
    exists for rows produced by older code or a deployment interrupted in the
    former two-transaction window.  It never overwrites a different operation.
    """
    now = time.time()
    with session_scope() as session:
        # Grading mutations use G -> W -> A everywhere.  Acquiring W first
        # here could deadlock a grading heartbeat that already owns G.
        run = session.scalar(
            select(GradingRunRecord)
            .where(
                GradingRunRecord.id == run_id,
                GradingRunRecord.assignment_id == assignment_id,
                GradingRunRecord.teacher_id == owner_id,
                GradingRunRecord.status.in_(
                    tuple(education.ACTIVE_GRADING_RUN_STATUSES)
                ),
            )
            .with_for_update()
        )
        if run is None:
            raise NotFound("grading_run")
        workflow = session.scalar(
            select(AssignmentWorkflowRecord)
            .where(
                AssignmentWorkflowRecord.assignment_id == assignment_id,
                AssignmentWorkflowRecord.owner_id == owner_id,
            )
            .with_for_update()
        )
        if workflow is None:
            raise NotFound("workflow")
        _lock_live_assignment(
            session, assignment_id=assignment_id, owner_id=owner_id
        )
        if workflow.grading_job_id not in {None, run_id}:
            raise InvalidTransition(
                "The grading run is not the task's current result generation.",
                code="grading_run_not_current",
            )
        if (
            workflow.active_operation == "grading"
            and workflow.active_job_id == run_id
            and workflow.grading_job_id == run_id
        ):
            return _detach_workflow(workflow)
        claim_id = workflow.active_job_id or ""
        repairable_claim = claim_id.startswith("grading_claim_")
        if (
            workflow.active_operation not in {None, "grading"}
            or (
                workflow.active_job_id not in {None, run_id}
                and not repairable_claim
            )
            or workflow.grading_job_id not in {None, run_id}
        ):
            raise InvalidTransition("workflow_busy", code="workflow_busy")
        workflow.presentation_status = "grading"
        workflow.grading_job_id = run_id
        workflow.active_operation = "grading"
        workflow.active_job_id = run_id
        workflow.last_failed_job_id = None
        workflow.error_code = None
        workflow.updated_at = now
        session.flush()
        return _detach_workflow(workflow)


def supersede_workflow_operation(
    assignment_id: str,
    *,
    owner_id: str,
    operation_id: str,
    expected_attempt: int,
) -> AssignmentWorkflowRecord | None:
    """Stop one active OCR/import operation and release its workflow claim.

    The operation transition and marker cleanup share a transaction.  A late
    worker still holding the old attempt can no longer commit its staged data.
    """
    now = time.time()
    with session_scope() as session:
        stopped = session.execute(
            update(WorkflowOperationRecord)
            .where(
                WorkflowOperationRecord.id == operation_id,
                WorkflowOperationRecord.assignment_id == assignment_id,
                WorkflowOperationRecord.owner_id == owner_id,
                WorkflowOperationRecord.attempt == expected_attempt,
                WorkflowOperationRecord.status.in_(("pending", "running")),
            )
            .values(
                status="error",
                error_code="superseded",
                completed_at=now,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                lease_heartbeat_at=None,
                updated_at=now,
            )
        )
        if stopped.rowcount != 1:
            return None
        released = session.execute(
            update(AssignmentWorkflowRecord)
            .where(
                AssignmentWorkflowRecord.assignment_id == assignment_id,
                AssignmentWorkflowRecord.owner_id == owner_id,
                AssignmentWorkflowRecord.active_job_id == operation_id,
            )
            .values(
                active_operation=None,
                active_job_id=None,
                error_code=None,
                updated_at=now,
            )
        )
        if released.rowcount != 1:
            return None
        row = session.get(AssignmentWorkflowRecord, assignment_id)
        assert row is not None
        return _detach_workflow(row)


def confirm_final_result_atomic(
    *,
    assignment_id: str,
    owner_id: str,
    grading_run_id: str,
    expected_revision: int,
) -> tuple[AssignmentWorkflowRecord, float, bool]:
    """Release a grading run and advance its formal-result version atomically.

    The workflow row is the optimistic concurrency gate and the grading-run
    row serializes this operation against teacher review.  Returning ``False``
    is an idempotent replay: a lost HTTP response can be retried with the old
    workflow revision without publishing or incrementing the version twice.
    """
    now = time.time()
    terminal_statuses = {"completed", "partial_failed"}
    unresolved_statuses = {"failed"}
    with session_scope() as session:
        # Canonical grading lock order: G -> W -> A.
        run = session.scalar(
            select(GradingRunRecord)
            .where(
                GradingRunRecord.id == grading_run_id,
                GradingRunRecord.assignment_id == assignment_id,
                GradingRunRecord.teacher_id == owner_id,
            )
            .with_for_update()
        )
        if run is None:
            raise NotFound("grading_run")
        workflow = session.scalar(
            select(AssignmentWorkflowRecord)
            .where(
                AssignmentWorkflowRecord.assignment_id == assignment_id,
                AssignmentWorkflowRecord.owner_id == owner_id,
            )
            .with_for_update()
        )
        if workflow is None:
            raise NotFound("workflow")
        _lock_live_assignment(
            session, assignment_id=assignment_id, owner_id=owner_id
        )
        if workflow.grading_job_id not in {None, grading_run_id}:
            raise InvalidTransition(
                "The grading run is not the workflow's current run.",
                code="grading_run_not_current",
            )

        # The complete state is checked before the client revision so a retry
        # after a lost response remains genuinely idempotent.
        already_recorded = (
            run.released_at is not None
            and workflow.presentation_status == "finalized"
            and workflow.final_result_version > 0
            and workflow.final_result_updated_at is not None
            and workflow.final_result_updated_at == run.released_at
        )
        if already_recorded:
            # Compatibility callers created before atomic run/workflow binding
            # can still have a null pointer.  Bind only this exact released run
            # while both rows are locked; never overwrite another generation.
            if workflow.grading_job_id is None:
                workflow.grading_job_id = grading_run_id
                workflow.updated_at = now
            # F-B was added after some formal results already existed. An exact
            # replay repairs the missing cleanup enqueue inside this same row
            # lock without publishing a new result version.
            from backend.db.source_storage_repository import (
                enqueue_finalized_source_cleanup_in_session,
                grading_run_source_file_ids_in_session,
            )

            enqueue_finalized_source_cleanup_in_session(
                session,
                assignment_id=assignment_id,
                owner_id=owner_id,
                grading_run_id=grading_run_id,
                final_result_version=workflow.final_result_version,
                finalized_at=float(run.released_at),
                source_file_ids=grading_run_source_file_ids_in_session(
                    session, grading_run_id
                ),
            )
            return _detach_workflow(workflow), float(run.released_at), False

        if workflow.workflow_revision != expected_revision:
            raise VersionConflict("workflow_revision_conflict")
        if run.status not in terminal_statuses:
            raise InvalidTransition("run_not_complete")
        unresolved = session.scalar(
            select(GradeResultRecord.id)
            .where(
                GradeResultRecord.grading_run_id == grading_run_id,
                GradeResultRecord.result_status.in_(unresolved_statuses),
            )
            .limit(1)
        )
        if unresolved is not None:
            raise ResultNotReleasable("unresolved_failures")
        # Match the normalized release gate: the run cannot become formal
        # until every frozen revision has exactly one result for every frozen
        # question. This check stays inside the same run-row transaction.
        from backend.db.grading_repository import _result_matrix_axes

        revision_ids, question_ids = _result_matrix_axes(session, run)
        actual_pairs = set(session.execute(
            select(
                GradeResultRecord.submission_revision_id,
                GradeResultRecord.question_id,
            ).where(GradeResultRecord.grading_run_id == grading_run_id)
        ).all())
        expected_pairs = {
            (revision_id, question_id)
            for revision_id in revision_ids
            for question_id in question_ids
        }
        if not question_ids or actual_pairs != expected_pairs:
            raise ResultNotReleasable(
                "The grading result matrix is incomplete.",
                code="incomplete_result_matrix",
            )

        # A legacy interrupted release may have stamped the run but not the
        # workflow. Repair that state in-place; otherwise this is a new formal
        # version and receives one release timestamp/version increment.
        released_at = float(run.released_at) if run.released_at is not None else now
        if run.released_at is None:
            run.released_at = released_at
        workflow.presentation_status = "finalized"
        workflow.grading_job_id = grading_run_id
        workflow.final_result_version = max(1, workflow.final_result_version + 1)
        workflow.final_result_updated_at = released_at
        workflow.analysis_status = "not_generated"
        workflow.analysis_result_version = None
        workflow.analysis_generated_at = None
        workflow.analysis_error_code = None
        workflow.active_operation = None
        workflow.active_job_id = None
        workflow.error_code = None
        workflow.workflow_revision += 1
        workflow.updated_at = now
        # Grading completion normally enqueues the run's immutable source
        # manifest as soon as the visual result becomes available. Formal
        # confirmation repeats that exact manifest transactionally as an
        # idempotent repair. Legacy runs without a manifest retain the broader
        # compatibility snapshot below. Optional CSV/Markdown/LaTeX/ZIP
        # generation is never a prerequisite.
        from backend.db.source_storage_repository import (
            enqueue_finalized_source_cleanup_in_session,
            grading_run_source_file_ids_in_session,
        )

        frozen_source_file_ids = grading_run_source_file_ids_in_session(
            session, grading_run_id
        )
        if frozen_source_file_ids is None:
            # Legacy runs have no immutable source manifest, so their fallback
            # cleanup snapshots every current task original.  Only that broad
            # compatibility path advances the global epoch to fence an object
            # that was reserved before the snapshot but publishes afterwards.
            workflow.source_lifecycle_epoch += 1

        enqueue_finalized_source_cleanup_in_session(
            session,
            assignment_id=assignment_id,
            owner_id=owner_id,
            grading_run_id=grading_run_id,
            final_result_version=workflow.final_result_version,
            finalized_at=released_at,
            source_file_ids=frozen_source_file_ids,
        )
        session.flush()
        return _detach_workflow(workflow), released_at, True


def delete_workflow(assignment_id: str, *, owner_id: str) -> None:
    with session_scope() as session:
        result = session.execute(
            delete(AssignmentWorkflowRecord).where(
                AssignmentWorkflowRecord.assignment_id == assignment_id,
                AssignmentWorkflowRecord.owner_id == owner_id,
            )
        )
        if result.rowcount != 1:
            raise NotFound("workflow")


def create_operation(
    *,
    assignment_id: str,
    owner_id: str,
    operation_type: str,
    input_hash: str,
    payload: dict | None = None,
    progress: dict | None = None,
    expires_at: float | None = None,
    initial_status: str = "pending",
) -> tuple[WorkflowOperationRecord, bool]:
    if initial_status not in {"pending", "preparing"}:
        raise ValidationError(
            "Invalid initial workflow operation status.",
            code="invalid_operation_status",
        )
    payload = _validate_json_object(
        payload if payload is not None else {},
        field="payload",
        max_bytes=MAX_OPERATION_PAYLOAD_BYTES,
    )
    progress = _validate_json_object(
        progress if progress is not None else {},
        field="progress",
        max_bytes=MAX_OPERATION_PROGRESS_BYTES,
    )
    now = time.time()
    selector = (
        WorkflowOperationRecord.assignment_id == assignment_id,
        WorkflowOperationRecord.owner_id == owner_id,
        WorkflowOperationRecord.operation_type == operation_type,
        WorkflowOperationRecord.input_hash == input_hash,
    )

    def lock_task_write_gate(session) -> None:
        # Callers lock an existing operation first, then use the shared
        # operation -> workflow -> assignment order. For a genuinely new row
        # there is no operation to contend with yet, so workflow -> assignment
        # remains the safe insertion gate.
        session.scalar(
            select(AssignmentWorkflowRecord)
            .where(
                AssignmentWorkflowRecord.assignment_id == assignment_id,
                AssignmentWorkflowRecord.owner_id == owner_id,
            )
            .with_for_update()
        )
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
            raise NotFound("assignment")

    def existing_result(session, existing: WorkflowOperationRecord):
        retryable = or_(
            WorkflowOperationRecord.status == "error",
            and_(
                WorkflowOperationRecord.status.in_(("preparing", "pending", "running")),
                WorkflowOperationRecord.expires_at.is_not(None),
                WorkflowOperationRecord.expires_at <= now,
            ),
        )
        claimed = session.execute(
            update(WorkflowOperationRecord)
            .where(
                WorkflowOperationRecord.id == existing.id,
                WorkflowOperationRecord.owner_id == owner_id,
                WorkflowOperationRecord.attempt == existing.attempt,
                retryable,
            )
            .values(
                attempt=WorkflowOperationRecord.attempt + 1,
                status=initial_status, progress=progress, payload=payload,
                checkpoint_revision=0, checkpoint_stage=None, checkpoint={},
                artifact_refs=[], terminal_summary=None,
                error_code=None, updated_at=now, completed_at=None,
                expires_at=expires_at,
                # A new retry generation is unleased; the worker that claims
                # it receives a fresh token.
                lease_owner=None, lease_token=None,
                lease_expires_at=None, lease_heartbeat_at=None,
            )
        )
        if claimed.rowcount == 1:
            refreshed = session.scalar(
                select(WorkflowOperationRecord).where(
                    WorkflowOperationRecord.id == existing.id
                )
            )
            assert refreshed is not None
            return _detach_operation(refreshed), True
        session.expire_all()
        current = session.scalar(select(WorkflowOperationRecord).where(*selector))
        assert current is not None
        return _detach_operation(current), False

    try:
        with session_scope() as session:
            existing = session.scalar(
                select(WorkflowOperationRecord)
                .where(*selector)
                .with_for_update()
            )
            if existing is not None:
                lock_task_write_gate(session)
                return existing_result(session, existing)
            lock_task_write_gate(session)
            row = WorkflowOperationRecord(
                id=_new_id("op"), assignment_id=assignment_id,
                owner_id=owner_id, operation_type=operation_type,
                input_hash=input_hash, attempt=1, status=initial_status,
                payload=payload, progress=progress,
                created_at=now, updated_at=now, expires_at=expires_at,
            )
            session.add(row)
            session.flush()
            return _detach_operation(row), True
    except IntegrityError:
        # A concurrent first request may win the unique-key insert after our
        # initial SELECT.  Re-read its committed row and replay it instead of
        # leaking a raw database exception to the API.
        with session_scope() as session:
            existing = session.scalar(
                select(WorkflowOperationRecord)
                .where(*selector)
                .with_for_update()
            )
            if existing is None:
                raise
            lock_task_write_gate(session)
            return existing_result(session, existing)


def get_operation(operation_id: str, *, owner_id: str) -> WorkflowOperationRecord:
    with session_scope() as session:
        row = session.scalar(
            select(WorkflowOperationRecord).where(
                WorkflowOperationRecord.id == operation_id,
                WorkflowOperationRecord.owner_id == owner_id,
            )
        )
        if row is None:
            raise NotFound("workflow_operation")
        return _detach_operation(row)


def update_operation(
    operation_id: str, *, owner_id: str, expected_attempt: int,
    expected_lease_token: str | None = None,
    **changes: Any,
) -> WorkflowOperationRecord:
    allowed = {"status", "progress", "payload", "error_code", "completed_at", "expires_at"}
    values = {key: value for key, value in changes.items() if key in allowed}
    if "payload" in values:
        values["payload"] = _validate_json_object(
            values["payload"],
            field="payload",
            max_bytes=MAX_OPERATION_PAYLOAD_BYTES,
        )
    if "progress" in values:
        values["progress"] = _validate_json_object(
            values["progress"],
            field="progress",
            max_bytes=MAX_OPERATION_PROGRESS_BYTES,
        )
    _validate_lease_token(expected_lease_token)
    now = time.time()
    with session_scope() as session:
        result = session.execute(
            update(WorkflowOperationRecord).where(
                WorkflowOperationRecord.id == operation_id,
                WorkflowOperationRecord.owner_id == owner_id,
                WorkflowOperationRecord.attempt == expected_attempt,
                WorkflowOperationRecord.terminal_summary.is_(None),
                _lease_write_predicate(expected_lease_token, now),
            ).values(**values, updated_at=now)
        )
        if result.rowcount != 1:
            current = session.scalar(select(WorkflowOperationRecord).where(
                WorkflowOperationRecord.id == operation_id,
                WorkflowOperationRecord.owner_id == owner_id,
            ))
            if current is None:
                raise NotFound("workflow_operation")
            if current.attempt == expected_attempt and current.terminal_summary is not None:
                raise InvalidTransition(
                    "The workflow operation already has a terminal summary.",
                    code="operation_already_terminal",
                )
            if not _lease_allows_write(current, expected_lease_token, now):
                raise LeaseLost(
                    "The operation lease is held by another worker or expired.",
                    code="lease_lost",
                )
            raise VersionConflict(
                "A newer workflow operation attempt is active.",
                code="stale_operation_attempt",
            )
        row = session.scalar(select(WorkflowOperationRecord).where(
            WorkflowOperationRecord.id == operation_id,
            WorkflowOperationRecord.owner_id == owner_id,
            WorkflowOperationRecord.attempt == expected_attempt,
        ))
        assert row is not None
        if (
            row.operation_type not in {
                TASK_DELETE_OPERATION,
                SOURCE_RESERVATION_CLEANUP_OPERATION,
            }
            and values.get("status") != "error"
        ):
            session.scalar(
                select(AssignmentWorkflowRecord)
                .where(
                    AssignmentWorkflowRecord.assignment_id == row.assignment_id,
                    AssignmentWorkflowRecord.owner_id == owner_id,
                )
                .with_for_update()
            )
            _lock_live_assignment(
                session,
                assignment_id=row.assignment_id,
                owner_id=owner_id,
            )
        return _detach_operation(row)


def save_operation_checkpoint(
    operation_id: str,
    *,
    owner_id: str,
    expected_attempt: int,
    expected_checkpoint_revision: int,
    stage: str | None,
    checkpoint: dict,
    artifact_refs: list[str] | None = None,
    terminal_summary: dict | None = None,
    terminal_status: str | None = None,
    expected_lease_token: str | None = None,
) -> WorkflowOperationRecord:
    stage = _validate_checkpoint_stage(stage)
    checkpoint = _validate_json_object(
        checkpoint,
        field="checkpoint",
        max_bytes=MAX_OPERATION_CHECKPOINT_BYTES,
    )
    if terminal_summary is not None:
        terminal_summary = _validate_json_object(
            terminal_summary,
            field="terminal_summary",
            max_bytes=MAX_OPERATION_TERMINAL_SUMMARY_BYTES,
        )
    if (terminal_summary is None) != (terminal_status is None):
        raise ValidationError(
            "Terminal summary and status must be supplied together.",
            code="invalid_operation_terminal_state",
        )
    if terminal_status is not None and (
        not isinstance(terminal_status, str)
        or not terminal_status
        or len(terminal_status) > 32
        or terminal_status in {"pending", "running"}
    ):
        raise ValidationError(
            "Invalid terminal operation status.",
            code="invalid_operation_terminal_status",
        )
    refs = _validate_artifact_refs(
        artifact_refs if artifact_refs is not None else []
    )
    _validate_lease_token(expected_lease_token)
    with session_scope() as session:
        # Acquire the producer lock before any artifact row. The exact no-op
        # UPDATE is both a PostgreSQL row lock and SQLite's write/CAS gate;
        # SELECT FOR UPDATE alone would not serialize SQLite writers.
        now = time.time()
        fence_conditions = [
            WorkflowOperationRecord.id == operation_id,
            WorkflowOperationRecord.owner_id == owner_id,
            WorkflowOperationRecord.attempt == expected_attempt,
            WorkflowOperationRecord.checkpoint_revision
            == expected_checkpoint_revision,
            WorkflowOperationRecord.terminal_summary.is_(None),
            WorkflowOperationRecord.status.in_((
                "preparing", "pending", "ready", "running",
            )),
            _lease_write_predicate(expected_lease_token, now),
        ]
        if expected_lease_token is not None:
            fence_conditions.append(WorkflowOperationRecord.status == "running")
        fenced = session.execute(
            update(WorkflowOperationRecord)
            .where(*fence_conditions)
            .values(updated_at=WorkflowOperationRecord.updated_at)
        )
        if fenced.rowcount != 1:
            session.expire_all()
            current = session.scalar(select(WorkflowOperationRecord).where(
                WorkflowOperationRecord.id == operation_id,
                WorkflowOperationRecord.owner_id == owner_id,
            ))
            if current is None:
                raise NotFound("workflow_operation")
            if current.attempt != expected_attempt:
                raise VersionConflict(
                    "A newer workflow operation attempt is active.",
                    code="stale_operation_attempt",
                )
            if current.checkpoint_revision != expected_checkpoint_revision:
                raise VersionConflict(
                    "The workflow operation checkpoint changed.",
                    code="stale_checkpoint_revision",
                )
            if current.terminal_summary is not None:
                raise InvalidTransition(
                    "The workflow operation already has a terminal summary.",
                    code="operation_already_terminal",
                )
            if current.status not in {
                "preparing", "pending", "ready", "running",
            } or (
                expected_lease_token is not None
                and current.status != "running"
            ):
                raise LeaseLost(
                    "The workflow operation no longer accepts checkpoint writes.",
                    code="lease_lost",
                )
            if not _lease_allows_write(current, expected_lease_token, time.time()):
                raise LeaseLost(
                    "The operation lease is held by another worker or expired.",
                    code="lease_lost",
                )
            raise VersionConflict(
                "The workflow operation checkpoint changed.",
                code="stale_checkpoint_revision",
            )
        current = session.scalar(select(WorkflowOperationRecord).where(
            WorkflowOperationRecord.id == operation_id,
            WorkflowOperationRecord.owner_id == owner_id,
        ))
        assert current is not None
        now = time.time()
        if not _lease_allows_write(current, expected_lease_token, now):
            raise LeaseLost(
                "The operation lease is held by another worker or expired.",
                code="lease_lost",
            )
        if refs:
            matched_refs = set(session.scalars(
                select(StoredFileRecord.id).where(
                    StoredFileRecord.id.in_(refs),
                    StoredFileRecord.owner_id == owner_id,
                    StoredFileRecord.assignment_id == current.assignment_id,
                ).with_for_update()
            ))
            if matched_refs != set(refs):
                raise NotFound("stored_file")

        checkpoint_values = {
            "checkpoint_revision": current.checkpoint_revision + 1,
            "checkpoint_stage": stage,
            "checkpoint": checkpoint,
            "artifact_refs": refs,
            "terminal_summary": terminal_summary,
            "updated_at": now,
        }
        if terminal_status is not None:
            checkpoint_values.update(
                status=terminal_status,
                completed_at=now,
                # A terminal transition releases only its own matching lease;
                # a fenced or stale worker never reaches this branch.
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                lease_heartbeat_at=None,
            )
        for key, value in checkpoint_values.items():
            setattr(current, key, value)
        session.flush()
        return _detach_operation(current)


def claim_operation(
    operation_id: str,
    *,
    owner_id: str,
    worker_id: str,
    lease_seconds: int,
) -> WorkflowOperationRecord:
    """Atomically claim or reclaim an operation's lease with a fresh token.

    The conditional UPDATE matches a row that is pending or running and is
    either never leased or whose lease has expired. Any live lease — including
    one held by this same ``worker_id`` — rejects a second claim, so two
    concurrent coroutines in one process cannot both win. rowcount 0 means a
    live worker holds the lease → ``LeaseLost``. The token rotates on every
    claim, so a worker that lost the race can no longer write with its old
    token.
    """
    _validate_worker_id(worker_id)
    if lease_seconds <= 0:
        raise ValidationError(
            "Lease duration must be positive.",
            code="invalid_lease_duration",
        )
    now = time.time()
    with session_scope() as session:
        result = session.execute(
            update(WorkflowOperationRecord)
            .where(
                WorkflowOperationRecord.id == operation_id,
                WorkflowOperationRecord.owner_id == owner_id,
                WorkflowOperationRecord.status.in_(("pending", "running")),
                or_(
                    WorkflowOperationRecord.operation_type.not_in(
                        tuple(DELAYED_SOURCE_OPERATION_TYPES)
                    ),
                    WorkflowOperationRecord.expires_at.is_(None),
                    WorkflowOperationRecord.expires_at <= now,
                ),
                or_(
                    WorkflowOperationRecord.lease_owner.is_(None),
                    WorkflowOperationRecord.lease_expires_at < now,
                ),
            )
            .values(
                status="running",
                lease_owner=worker_id,
                lease_token=_new_lease_token(),
                lease_expires_at=now + lease_seconds,
                lease_heartbeat_at=now,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            session.expire_all()
            current = session.scalar(
                select(WorkflowOperationRecord).where(
                    WorkflowOperationRecord.id == operation_id,
                    WorkflowOperationRecord.owner_id == owner_id,
                )
            )
            if current is None:
                raise NotFound("workflow_operation")
            raise LeaseLost(
                "The workflow operation is not claimable.",
                code="operation_not_claimable",
            )
        row = session.scalar(
            select(WorkflowOperationRecord).where(
                WorkflowOperationRecord.id == operation_id,
                WorkflowOperationRecord.owner_id == owner_id,
            )
        )
        assert row is not None
        if row.operation_type not in {
            TASK_DELETE_OPERATION,
            SOURCE_RESERVATION_CLEANUP_OPERATION,
        }:
            session.scalar(
                select(AssignmentWorkflowRecord)
                .where(
                    AssignmentWorkflowRecord.assignment_id == row.assignment_id,
                    AssignmentWorkflowRecord.owner_id == row.owner_id,
                )
                .with_for_update()
            )
            assignment = session.scalar(
                select(AssignmentRecord)
                .where(
                    AssignmentRecord.id == row.assignment_id,
                    AssignmentRecord.teacher_id == row.owner_id,
                )
                .with_for_update()
            )
            if assignment is None or assignment.deletion_requested_at is not None:
                raise LeaseLost(
                    "The task is being deleted.", code="task_deleted"
                )
        return _detach_operation(row)


def heartbeat_operation(
    operation_id: str,
    *,
    owner_id: str,
    worker_id: str,
    lease_token: str,
    lease_seconds: int,
) -> bool:
    """Extend the lease. Only the current owner with the live matching token
    may heartbeat; a fenced or expired lease raises ``LeaseLost`` so the worker
    loop stops rather than silently extending a lease it no longer holds."""
    _validate_worker_id(worker_id)
    _validate_lease_token(lease_token)
    if lease_seconds <= 0:
        raise ValidationError(
            "Lease duration must be positive.",
            code="invalid_lease_duration",
        )
    now = time.time()
    with session_scope() as session:
        result = session.execute(
            update(WorkflowOperationRecord)
            .where(
                WorkflowOperationRecord.id == operation_id,
                WorkflowOperationRecord.owner_id == owner_id,
                WorkflowOperationRecord.lease_owner == worker_id,
                WorkflowOperationRecord.lease_token == lease_token,
                WorkflowOperationRecord.lease_expires_at >= now,
                WorkflowOperationRecord.status == "running",
            )
            .values(
                lease_expires_at=now + lease_seconds,
                lease_heartbeat_at=now,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            session.expire_all()
            current = session.scalar(
                select(WorkflowOperationRecord).where(
                    WorkflowOperationRecord.id == operation_id,
                    WorkflowOperationRecord.owner_id == owner_id,
                )
            )
            if current is None:
                raise NotFound("workflow_operation")
            raise LeaseLost("lease_lost", code="lease_lost")
        operation = session.scalar(select(WorkflowOperationRecord).where(
            WorkflowOperationRecord.id == operation_id,
            WorkflowOperationRecord.owner_id == owner_id,
        ))
        assert operation is not None
        if operation.operation_type not in {
            TASK_DELETE_OPERATION,
            SOURCE_RESERVATION_CLEANUP_OPERATION,
        }:
            session.scalar(
                select(AssignmentWorkflowRecord)
                .where(
                    AssignmentWorkflowRecord.assignment_id
                    == operation.assignment_id,
                    AssignmentWorkflowRecord.owner_id == owner_id,
                )
                .with_for_update()
            )
            assignment = session.scalar(
                select(AssignmentRecord)
                .where(
                    AssignmentRecord.id == operation.assignment_id,
                    AssignmentRecord.teacher_id == owner_id,
                )
                .with_for_update()
            )
            if assignment is None or assignment.deletion_requested_at is not None:
                raise LeaseLost("lease_lost", code="task_deleted")
        return True


def release_operation(
    operation_id: str,
    *,
    owner_id: str,
    worker_id: str,
    lease_token: str,
) -> WorkflowOperationRecord:
    """Clear the lease only when this worker's live token still matches.

    A fenced or expired worker cannot release (and thereby hand the row back)
    after its token was rotated or its lease lapsed.
    """
    _validate_worker_id(worker_id)
    _validate_lease_token(lease_token)
    now = time.time()
    with session_scope() as session:
        result = session.execute(
            update(WorkflowOperationRecord)
            .where(
                WorkflowOperationRecord.id == operation_id,
                WorkflowOperationRecord.owner_id == owner_id,
                WorkflowOperationRecord.lease_owner == worker_id,
                WorkflowOperationRecord.lease_token == lease_token,
                WorkflowOperationRecord.lease_expires_at >= now,
            )
            .values(
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                lease_heartbeat_at=None,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            session.expire_all()
            current = session.scalar(
                select(WorkflowOperationRecord).where(
                    WorkflowOperationRecord.id == operation_id,
                    WorkflowOperationRecord.owner_id == owner_id,
                )
            )
            if current is None:
                raise NotFound("workflow_operation")
            raise LeaseLost("lease_lost", code="lease_lost")
        row = session.scalar(
            select(WorkflowOperationRecord).where(
                WorkflowOperationRecord.id == operation_id,
                WorkflowOperationRecord.owner_id == owner_id,
            )
        )
        assert row is not None
        return _detach_operation(row)


def reschedule_operation(
    operation_id: str,
    *,
    owner_id: str,
    worker_id: str,
    lease_token: str,
    expected_attempt: int,
    retry_at: float,
    progress: dict,
    error_code: str,
) -> WorkflowOperationRecord:
    """Persist a delayed automatic retry and release the current lease."""
    _validate_worker_id(worker_id)
    _validate_lease_token(lease_token)
    progress = _validate_json_object(
        progress, field="progress", max_bytes=MAX_OPERATION_PROGRESS_BYTES
    )
    if not isinstance(retry_at, (int, float)) or retry_at <= time.time():
        raise ValidationError(
            "The retry time must be in the future.",
            code="invalid_operation_retry_time",
        )
    now = time.time()
    with session_scope() as session:
        result = session.execute(
            update(WorkflowOperationRecord)
            .where(
                WorkflowOperationRecord.id == operation_id,
                WorkflowOperationRecord.owner_id == owner_id,
                WorkflowOperationRecord.operation_type.in_(
                    tuple(DELAYED_SOURCE_OPERATION_TYPES)
                ),
                WorkflowOperationRecord.attempt == expected_attempt,
                WorkflowOperationRecord.status == "running",
                WorkflowOperationRecord.terminal_summary.is_(None),
                WorkflowOperationRecord.lease_owner == worker_id,
                WorkflowOperationRecord.lease_token == lease_token,
                WorkflowOperationRecord.lease_expires_at >= now,
            )
            .values(
                status="pending",
                progress=progress,
                error_code=error_code,
                expires_at=float(retry_at),
                updated_at=now,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                lease_heartbeat_at=None,
            )
        )
        if result.rowcount != 1:
            raise LeaseLost("The operation lease was lost before retry scheduling.")
        row = session.scalar(select(WorkflowOperationRecord).where(
            WorkflowOperationRecord.id == operation_id,
            WorkflowOperationRecord.owner_id == owner_id,
        ))
        assert row is not None
        return _detach_operation(row)


def list_claimable_operations(
    operation_types: Iterable[str],
    *,
    limit: int = 10,
) -> list[WorkflowOperationRecord]:
    """Return a bounded set of rows a worker may claim.

    Only rows for the supported operation types that are pending, or running
    with no live lease (never leased or expired), are returned. Rows already
    held by a live worker are excluded; each returned row is then claimed with
    the polling worker's identity, so the conditional claim does the real
    fencing and owner predicates are preserved.
    """
    types = list(operation_types)
    if not types or limit <= 0:
        return []
    if limit > MAX_OPERATION_CLAIM_BATCH:
        raise ValidationError(
            "Claim batch exceeds its storage limit.",
            code="operation_claim_batch_too_large",
        )
    now = time.time()
    with session_scope() as session:
        rows = session.scalars(
            select(WorkflowOperationRecord)
            .where(
                WorkflowOperationRecord.operation_type.in_(types),
                WorkflowOperationRecord.status.in_(("pending", "running")),
                or_(
                    WorkflowOperationRecord.operation_type.not_in(
                        tuple(DELAYED_SOURCE_OPERATION_TYPES)
                    ),
                    WorkflowOperationRecord.expires_at.is_(None),
                    WorkflowOperationRecord.expires_at <= now,
                ),
                or_(
                    WorkflowOperationRecord.lease_owner.is_(None),
                    WorkflowOperationRecord.lease_expires_at < now,
                ),
            )
            .order_by(WorkflowOperationRecord.updated_at.asc())
            .limit(limit)
        ).all()
        return [_detach_operation(row) for row in rows]


def upsert_student_presentation(
    *, assignment_id: str, student_id: str, display_student_id: str,
    display_name: str, source_filename: str | None = None,
    identity_match_method: str | None = None, identity_status: str = "needs_review",
    is_active: bool = True,
) -> AssignmentStudentPresentationRecord:
    now = time.time()
    with session_scope() as session:
        assignment = session.scalar(
            select(AssignmentRecord)
            .where(
                AssignmentRecord.id == assignment_id,
                AssignmentRecord.deletion_requested_at.is_(None),
            )
            .with_for_update()
        )
        if assignment is None:
            raise NotFound("assignment")
        row = session.scalar(
            select(AssignmentStudentPresentationRecord).where(
                AssignmentStudentPresentationRecord.assignment_id == assignment_id,
                AssignmentStudentPresentationRecord.student_id == student_id,
            )
        )
        if row is None:
            row = AssignmentStudentPresentationRecord(
                id=_new_id("sp"), assignment_id=assignment_id, student_id=student_id,
                display_student_id=display_student_id, display_name=display_name,
                source_filename=source_filename,
                identity_match_method=identity_match_method,
                identity_status=identity_status, is_active=is_active,
                created_at=now, updated_at=now,
            )
            session.add(row)
        else:
            row.display_student_id = display_student_id
            row.display_name = display_name
            row.source_filename = source_filename
            row.identity_match_method = identity_match_method
            row.identity_status = identity_status
            row.is_active = is_active
            row.updated_at = now
        session.flush()
        return _detach_student(row)


def list_student_presentations(
    assignment_id: str,
) -> dict[str, AssignmentStudentPresentationRecord]:
    with session_scope() as session:
        rows = session.scalars(
            select(AssignmentStudentPresentationRecord)
            .join(
                AssignmentRecord,
                AssignmentRecord.id
                == AssignmentStudentPresentationRecord.assignment_id,
            )
            .where(
                AssignmentStudentPresentationRecord.assignment_id == assignment_id,
                AssignmentRecord.deletion_requested_at.is_(None),
            )
        ).all()
        return {row.student_id: _detach_student(row) for row in rows}


def set_answer_review_status(answer_id: str, review_status: str) -> None:
    now = time.time()
    with session_scope() as session:
        row = session.get(SubmissionAnswerPresentationRecord, answer_id)
        if row is None:
            session.add(SubmissionAnswerPresentationRecord(
                answer_id=answer_id, review_status=review_status, updated_at=now
            ))
        else:
            row.review_status = review_status
            row.updated_at = now


def answer_review_statuses(answer_ids: Iterable[str]) -> dict[str, str]:
    ids = list(answer_ids)
    if not ids:
        return {}
    with session_scope() as session:
        rows = session.scalars(
            select(SubmissionAnswerPresentationRecord).where(
                SubmissionAnswerPresentationRecord.answer_id.in_(ids)
            )
        ).all()
        return {row.answer_id: row.review_status for row in rows}


def save_run_setup(
    *, grading_run_id: str, assignment_id: str, owner_id: str,
    setup: dict, fingerprint: str, input_manifest: dict | None = None,
) -> GradingRunSetupRecord:
    with session_scope() as session:
        row = session.get(GradingRunSetupRecord, grading_run_id)
        if row is None:
            row = GradingRunSetupRecord(
                grading_run_id=grading_run_id, assignment_id=assignment_id,
                owner_id=owner_id, setup=setup,
                input_manifest=input_manifest or {}, fingerprint=fingerprint,
                created_at=time.time(),
            )
            session.add(row)
            session.flush()
        return _detach_run_setup(row)


def get_run_setup(grading_run_id: str) -> GradingRunSetupRecord | None:
    with session_scope() as session:
        row = session.get(GradingRunSetupRecord, grading_run_id)
        return _detach_run_setup(row) if row is not None else None


def save_artifact_manifest(
    *, assignment_id: str, grading_run_id: str, owner_id: str,
    result_version: int, result_fingerprint: str, manifest: dict,
) -> tuple[ResultArtifactManifestRecord, bool]:
    declared_generated_at = manifest.get("generated_at")
    now = (
        float(declared_generated_at)
        if isinstance(declared_generated_at, (int, float))
        and not isinstance(declared_generated_at, bool)
        else time.time()
    )
    with session_scope() as session:
        _lock_live_assignment(
            session, assignment_id=assignment_id, owner_id=owner_id
        )
        row = session.scalar(
            select(ResultArtifactManifestRecord).where(
                ResultArtifactManifestRecord.assignment_id == assignment_id,
                ResultArtifactManifestRecord.result_version == result_version,
            )
        )
        created = row is None
        if row is None:
            row = ResultArtifactManifestRecord(
                id=_new_id("artifact"), assignment_id=assignment_id,
                grading_run_id=grading_run_id, owner_id=owner_id,
                result_version=result_version,
                result_fingerprint=result_fingerprint,
                manifest=manifest, generated_at=now,
            )
            session.add(row)
        else:
            if row.owner_id != owner_id:
                raise NotFound("artifact_manifest")
            # A formal result version is append-only.  Repeating generation
            # for the exact same frozen run is idempotent; trying to bind the
            # version number to different source facts is a version conflict,
            # never an in-place rewrite of published history.
            if (
                row.grading_run_id != grading_run_id
                or row.result_fingerprint != result_fingerprint
            ):
                raise VersionConflict("artifact_result_version_conflict")
        session.flush()
        return _detach_artifact(row), created


def save_artifact_manifest_atomic(
    *,
    assignment_id: str,
    grading_run_id: str,
    owner_id: str,
    result_version: int,
    result_fingerprint: str,
    manifest: dict,
    expected_revision: int,
) -> tuple[ResultArtifactManifestRecord, bool, AssignmentWorkflowRecord]:
    """Persist one artifact set and its ready-state in one transaction.

    Exact replays are returned before checking the stale client revision, so a
    caller that lost the original HTTP response gets ``already_done`` rather
    than a false conflict. A version can never be rebound to another run or
    result fingerprint.
    """
    declared_generated_at = manifest.get("generated_at")
    generated_at = (
        float(declared_generated_at)
        if isinstance(declared_generated_at, (int, float))
        and not isinstance(declared_generated_at, bool)
        else time.time()
    )
    now = time.time()
    with session_scope() as session:
        workflow = session.scalar(
            select(AssignmentWorkflowRecord)
            .where(
                AssignmentWorkflowRecord.assignment_id == assignment_id,
                AssignmentWorkflowRecord.owner_id == owner_id,
            )
            .with_for_update()
        )
        if workflow is None:
            raise NotFound("workflow")
        _lock_live_assignment(
            session, assignment_id=assignment_id, owner_id=owner_id
        )
        if workflow.final_result_version != result_version:
            raise VersionConflict("artifact_result_version_conflict")
        row = session.scalar(
            select(ResultArtifactManifestRecord).where(
                ResultArtifactManifestRecord.assignment_id == assignment_id,
                ResultArtifactManifestRecord.result_version == result_version,
            )
        )
        if row is not None:
            if row.owner_id != owner_id:
                raise NotFound("artifact_manifest")
            if (
                row.grading_run_id != grading_run_id
                or row.result_fingerprint != result_fingerprint
            ):
                raise VersionConflict("artifact_result_version_conflict")
            # Repair metadata left by the old multi-transaction path, but do
            # not increment the revision for a fully committed replay.
            if not (
                workflow.analysis_status == "ready"
                and workflow.analysis_result_version == result_version
                and workflow.analysis_generated_at == row.generated_at
            ):
                workflow.analysis_status = "ready"
                workflow.analysis_result_version = result_version
                workflow.analysis_generated_at = row.generated_at
                workflow.analysis_error_code = None
                workflow.workflow_revision += 1
                workflow.updated_at = now
                session.flush()
            return _detach_artifact(row), False, _detach_workflow(workflow)

        if workflow.workflow_revision != expected_revision:
            raise VersionConflict("workflow_revision_conflict")
        row = ResultArtifactManifestRecord(
            id=_new_id("artifact"),
            assignment_id=assignment_id,
            grading_run_id=grading_run_id,
            owner_id=owner_id,
            result_version=result_version,
            result_fingerprint=result_fingerprint,
            manifest=manifest,
            generated_at=generated_at,
        )
        session.add(row)
        workflow.analysis_status = "ready"
        workflow.analysis_result_version = result_version
        workflow.analysis_generated_at = generated_at
        workflow.analysis_error_code = None
        workflow.workflow_revision += 1
        workflow.updated_at = now
        session.flush()
        return _detach_artifact(row), True, _detach_workflow(workflow)


def list_artifact_manifests(
    assignment_id: str, *, owner_id: str
) -> list[ResultArtifactManifestRecord]:
    with session_scope() as session:
        rows = session.scalars(
            select(ResultArtifactManifestRecord)
            .where(
                ResultArtifactManifestRecord.assignment_id == assignment_id,
                ResultArtifactManifestRecord.owner_id == owner_id,
            )
            .order_by(ResultArtifactManifestRecord.result_version.desc())
        ).all()
        return [_detach_artifact(row) for row in rows]


def get_artifact_manifest(
    assignment_id: str, result_version: int, *, owner_id: str
) -> ResultArtifactManifestRecord:
    with session_scope() as session:
        row = session.scalar(
            select(ResultArtifactManifestRecord).where(
                ResultArtifactManifestRecord.assignment_id == assignment_id,
                ResultArtifactManifestRecord.result_version == result_version,
                ResultArtifactManifestRecord.owner_id == owner_id,
            )
        )
        if row is None:
            raise NotFound("artifact_manifest")
        return _detach_artifact(row)


def _copy_record(row, cls):
    return cls(**{column.name: getattr(row, column.name) for column in cls.__table__.columns})


def _detach_workflow(row: AssignmentWorkflowRecord) -> AssignmentWorkflowRecord:
    return _copy_record(row, AssignmentWorkflowRecord)


def _detach_operation(row: WorkflowOperationRecord) -> WorkflowOperationRecord:
    return _copy_record(row, WorkflowOperationRecord)


def _detach_student(row: AssignmentStudentPresentationRecord) -> AssignmentStudentPresentationRecord:
    return _copy_record(row, AssignmentStudentPresentationRecord)


def _detach_run_setup(row: GradingRunSetupRecord) -> GradingRunSetupRecord:
    return _copy_record(row, GradingRunSetupRecord)


def _detach_artifact(row: ResultArtifactManifestRecord) -> ResultArtifactManifestRecord:
    return _copy_record(row, ResultArtifactManifestRecord)
