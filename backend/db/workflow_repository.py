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

import time
import uuid
from typing import Any, Iterable

from sqlalchemy import (
    Boolean,
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
)
from backend.db.session import session_scope
from backend.domain.errors import (
    InvalidTransition,
    NotFound,
    ResultNotReleasable,
    VersionConflict,
)


class AssignmentWorkflowRecord(Base):
    __tablename__ = "assignment_workflows"

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
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)


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
    unresolved_statuses = {"failed", "needs_review"}
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

        # The complete state is checked before the client revision so a retry
        # after a lost response remains genuinely idempotent.
        already_recorded = (
            run.released_at is not None
            and workflow.presentation_status == "finalized"
            and workflow.final_result_version > 0
            and workflow.final_result_updated_at is not None
        )
        if already_recorded:
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
) -> tuple[WorkflowOperationRecord, bool]:
    now = time.time()
    selector = (
        WorkflowOperationRecord.assignment_id == assignment_id,
        WorkflowOperationRecord.owner_id == owner_id,
        WorkflowOperationRecord.operation_type == operation_type,
        WorkflowOperationRecord.input_hash == input_hash,
    )

    def existing_result(session, existing: WorkflowOperationRecord):
        retryable = or_(
            WorkflowOperationRecord.status == "error",
            and_(
                WorkflowOperationRecord.status.in_(("pending", "running")),
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
                status="pending", progress=progress or {}, payload=payload or {},
                error_code=None, updated_at=now, completed_at=None,
                expires_at=expires_at,
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
                select(WorkflowOperationRecord).where(*selector)
            )
            if existing is not None:
                return existing_result(session, existing)
            row = WorkflowOperationRecord(
                id=_new_id("op"), assignment_id=assignment_id,
                owner_id=owner_id, operation_type=operation_type,
                input_hash=input_hash, attempt=1, status="pending",
                payload=payload or {}, progress=progress or {},
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
                select(WorkflowOperationRecord).where(*selector)
            )
            if existing is None:
                raise
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
    **changes: Any,
) -> WorkflowOperationRecord:
    allowed = {"status", "progress", "payload", "error_code", "completed_at", "expires_at"}
    values = {key: value for key, value in changes.items() if key in allowed}
    now = time.time()
    with session_scope() as session:
        result = session.execute(
            update(WorkflowOperationRecord).where(
                WorkflowOperationRecord.id == operation_id,
                WorkflowOperationRecord.owner_id == owner_id,
                WorkflowOperationRecord.attempt == expected_attempt,
            ).values(**values, updated_at=now)
        )
        if result.rowcount != 1:
            exists = session.scalar(select(WorkflowOperationRecord.id).where(
                WorkflowOperationRecord.id == operation_id,
                WorkflowOperationRecord.owner_id == owner_id,
            ))
            if exists is None:
                raise NotFound("workflow_operation")
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
        return _detach_operation(row)


def upsert_student_presentation(
    *, assignment_id: str, student_id: str, display_student_id: str,
    display_name: str, source_filename: str | None = None,
    identity_match_method: str | None = None, identity_status: str = "needs_review",
    is_active: bool = True,
) -> AssignmentStudentPresentationRecord:
    now = time.time()
    with session_scope() as session:
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
            select(AssignmentStudentPresentationRecord).where(
                AssignmentStudentPresentationRecord.assignment_id == assignment_id
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
