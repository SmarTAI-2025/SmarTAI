"""Durable, lease-fenced task deletion and physical object reconciliation.

A teacher delete is a logical tombstone first.  The assignment remains in the
database while this repository cancels producers, removes every task-owned
object, and only then lets the database cascade structured task data.  Keeping
the parent row until storage absence is confirmed prevents untracked orphans
when an object store is temporarily unavailable.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from backend.db.models import (
    AssignmentRecord,
    CourseEnrollmentRecord,
    GradingRunRecord,
    SourceStorageReservationRecord,
    StoredFileRecord,
    SubmissionRecord,
    SubmissionRevisionRecord,
    UserRecord,
)
from backend.db.session import session_scope
from backend.db import source_storage_repository, workflow_repository
from backend.domain import education
from backend.domain.errors import LeaseLost, NotFound
from backend.domain.source_storage import (
    SOURCE_FILE_AVAILABLE,
    SOURCE_FILE_CLEANUP_PENDING,
    SOURCE_FILE_UNAVAILABLE,
    SOURCE_REASON_STORAGE_DELETE_FAILED,
    SOURCE_REASON_TASK_DELETED,
    SOURCE_RESERVATION_CLEANUP_OPERATION,
    TASK_DELETE_OPERATION,
)


@dataclass(frozen=True)
class TaskDeletionPass:
    producer_blockers: int
    next_producer_retry_at: float | None
    pending_reservations: int
    next_reservation_retry_at: float | None
    pending_files: int


@dataclass(frozen=True)
class TaskFileDeleteClaim:
    status: str
    file_id: str
    storage_backend: str | None = None
    storage_key: str | None = None
    claim_token: str | None = None


def _task_revision_ids(assignment_id: str):
    return (
        select(SubmissionRevisionRecord.id)
        .join(
            SubmissionRecord,
            SubmissionRecord.id == SubmissionRevisionRecord.submission_id,
        )
        .where(SubmissionRecord.assignment_id == assignment_id)
    )


def _belongs_to_task(assignment_id: str):
    return or_(
        StoredFileRecord.assignment_id == assignment_id,
        StoredFileRecord.submission_revision_id.in_(
            _task_revision_ids(assignment_id)
        ),
    )


def _task_import_user_ids(session: Session, assignment_id: str) -> tuple[str, ...]:
    """Identify disabled synthetic students created by this task's import path."""

    return tuple(str(value) for value in session.scalars(
        select(UserRecord.id)
        .join(SubmissionRecord, SubmissionRecord.student_id == UserRecord.id)
        .where(
            SubmissionRecord.assignment_id == assignment_id,
            UserRecord.id.startswith("imported_"),
            UserRecord.username.startswith("imported-"),
            UserRecord.email.is_(None),
            UserRecord.role == "student",
            UserRecord.is_active.is_(False),
            UserRecord.password_hash == "!disabled-imported-account",
        )
        .distinct()
        .order_by(UserRecord.id.asc())
    ))


def _delete_unreferenced_task_import_users(
    session: Session,
    *,
    candidate_ids: tuple[str, ...],
    course_id: str,
) -> None:
    """Remove only task-exclusive synthetic users and their import enrollment.

    Imported students are disabled placeholder accounts, not real identities,
    but ``users`` and ``course_enrollments`` are outside the assignment cascade.
    Lock every candidate parent first, then retain any account referenced by a
    different course or by *any* non-enrollment user FK. The metadata-driven
    reference check is deliberately future-proof: adding another user-owned
    table cannot silently turn task deletion into a cross-scope cascade.
    """

    if not candidate_ids:
        return
    users = list(session.scalars(
        select(UserRecord)
        .where(
            UserRecord.id.in_(candidate_ids),
            UserRecord.id.startswith("imported_"),
            UserRecord.username.startswith("imported-"),
            UserRecord.email.is_(None),
            UserRecord.role == "student",
            UserRecord.is_active.is_(False),
            UserRecord.password_hash == "!disabled-imported-account",
        )
        .order_by(UserRecord.id.asc())
        .with_for_update()
    ))
    eligible_ids = {str(user.id) for user in users}
    if not eligible_ids:
        return

    protected_ids = set(str(value) for value in session.scalars(
        select(CourseEnrollmentRecord.student_id).where(
            CourseEnrollmentRecord.student_id.in_(eligible_ids),
            CourseEnrollmentRecord.course_id != course_id,
        )
    ))
    for table in sorted(UserRecord.metadata.tables.values(), key=lambda item: item.name):
        if table.name in {"users", "course_enrollments"}:
            continue
        for column in table.columns:
            if not any(
                foreign_key.column.table.name == "users"
                and foreign_key.column.name == "id"
                for foreign_key in column.foreign_keys
            ):
                continue
            protected_ids.update(
                str(value)
                for value in session.scalars(
                    select(column).where(column.in_(eligible_ids)).distinct()
                )
                if value is not None
            )

    deletable_ids = eligible_ids - protected_ids
    if not deletable_ids:
        return
    session.execute(delete(CourseEnrollmentRecord).where(
        CourseEnrollmentRecord.student_id.in_(deletable_ids),
        CourseEnrollmentRecord.course_id == course_id,
    ))
    session.execute(delete(UserRecord).where(UserRecord.id.in_(deletable_ids)))


def _lock_live_delete_operation(
    session: Session,
    *,
    operation_id: str,
    owner_id: str,
    assignment_id: str,
    attempt: int,
    worker_id: str,
    lease_token: str,
):
    return source_storage_repository._lock_live_source_operation(
        session,
        operation_id=operation_id,
        operation_type=TASK_DELETE_OPERATION,
        owner_id=owner_id,
        assignment_id=assignment_id,
        attempt=attempt,
        worker_id=worker_id,
        lease_token=lease_token,
    )


def _drain_producer_operations(
    *, owner_id: str, assignment_id: str, delete_operation_id: str
) -> tuple[int, float | None]:
    """Terminalize producers one-by-one in the canonical O -> W -> A order."""
    now = time.time()
    with session_scope() as session:
        ids = tuple(str(value) for value in session.scalars(
            select(workflow_repository.WorkflowOperationRecord.id)
            .where(
                workflow_repository.WorkflowOperationRecord.assignment_id
                == assignment_id,
                workflow_repository.WorkflowOperationRecord.owner_id == owner_id,
                workflow_repository.WorkflowOperationRecord.id
                != delete_operation_id,
                workflow_repository.WorkflowOperationRecord.operation_type
                != SOURCE_RESERVATION_CLEANUP_OPERATION,
                workflow_repository.WorkflowOperationRecord.status.in_(
                    ("preparing", "pending", "ready", "running")
                ),
            )
            .order_by(workflow_repository.WorkflowOperationRecord.id.asc())
        ))

    blockers = 0
    retry_at: float | None = None
    for operation_id in ids:
        with session_scope() as session:
            producer = session.scalar(
                select(workflow_repository.WorkflowOperationRecord)
                .where(
                    workflow_repository.WorkflowOperationRecord.id == operation_id,
                    workflow_repository.WorkflowOperationRecord.owner_id == owner_id,
                    workflow_repository.WorkflowOperationRecord.assignment_id
                    == assignment_id,
                )
                .with_for_update()
            )
            if producer is None or producer.status not in {
                "preparing", "pending", "ready", "running"
            }:
                continue
            live_until: float | None = None
            if producer.status == "preparing":
                live_until = producer.expires_at
            elif (
                producer.status == "running"
                and producer.lease_expires_at is not None
                and producer.lease_expires_at >= now
            ):
                live_until = producer.lease_expires_at
            if live_until is not None and live_until >= now:
                blockers += 1
                candidate = float(live_until) + 0.001
                retry_at = candidate if retry_at is None else max(retry_at, candidate)
                continue

            # Producer row is already held. Lock the workflow gate and parent
            # in the same order as producer publication/compensation.
            session.scalar(
                select(workflow_repository.AssignmentWorkflowRecord)
                .where(
                    workflow_repository.AssignmentWorkflowRecord.assignment_id
                    == assignment_id,
                    workflow_repository.AssignmentWorkflowRecord.owner_id
                    == owner_id,
                )
                .with_for_update()
            )
            assignment = session.scalar(
                select(AssignmentRecord)
                .where(
                    AssignmentRecord.id == assignment_id,
                    AssignmentRecord.teacher_id == owner_id,
                )
                .with_for_update()
            )
            if assignment is None or assignment.deletion_requested_at is None:
                raise LeaseLost("Task deletion is no longer authorized.")
            producer.status = "error"
            producer.error_code = "task_deleted"
            producer.completed_at = now
            producer.updated_at = now
            producer.lease_owner = None
            producer.lease_token = None
            producer.lease_expires_at = None
            producer.lease_heartbeat_at = None
    return blockers, retry_at


def _drain_grading_runs(
    *, owner_id: str, assignment_id: str
) -> tuple[int, float | None]:
    """Cancel grading rows in the established G -> W -> A lock order."""
    now = time.time()
    with session_scope() as session:
        ids = tuple(str(value) for value in session.scalars(
            select(GradingRunRecord.id)
            .where(
                GradingRunRecord.assignment_id == assignment_id,
                GradingRunRecord.teacher_id == owner_id,
                GradingRunRecord.status.in_(tuple(
                    education.ACTIVE_GRADING_RUN_STATUSES
                )),
            )
            .order_by(GradingRunRecord.id.asc())
        ))
    blockers = 0
    retry_at: float | None = None
    for run_id in ids:
        with session_scope() as session:
            run = session.scalar(
                select(GradingRunRecord)
                .where(
                    GradingRunRecord.id == run_id,
                    GradingRunRecord.assignment_id == assignment_id,
                    GradingRunRecord.teacher_id == owner_id,
                )
                .with_for_update()
            )
            if run is None or run.status not in education.ACTIVE_GRADING_RUN_STATUSES:
                continue
            if (
                run.status == education.GradingRunStatus.RUNNING.value
                and run.lease_expiry is not None
                and run.lease_expiry >= now
            ):
                blockers += 1
                candidate = float(run.lease_expiry) + 0.001
                retry_at = candidate if retry_at is None else max(retry_at, candidate)
                continue
            workflow = session.scalar(
                select(workflow_repository.AssignmentWorkflowRecord)
                .where(
                    workflow_repository.AssignmentWorkflowRecord.assignment_id
                    == assignment_id,
                    workflow_repository.AssignmentWorkflowRecord.owner_id
                    == owner_id,
                )
                .with_for_update()
            )
            assignment = session.scalar(
                select(AssignmentRecord)
                .where(
                    AssignmentRecord.id == assignment_id,
                    AssignmentRecord.teacher_id == owner_id,
                )
                .with_for_update()
            )
            if assignment is None or assignment.deletion_requested_at is None:
                raise LeaseLost("Task deletion is no longer authorized.")
            run.status = education.GradingRunStatus.CANCELLED.value
            run.completed_at = now
            run.lease_owner = None
            run.lease_expiry = None
            if workflow is not None and workflow.active_job_id == run.id:
                workflow.active_operation = None
                workflow.active_job_id = None
                workflow.grading_job_id = None
                workflow.error_code = None
                workflow.updated_at = now
    return blockers, retry_at


def prepare_task_deletion_pass(
    *,
    operation_id: str,
    owner_id: str,
    assignment_id: str,
    attempt: int,
    worker_id: str,
    lease_token: str,
) -> TaskDeletionPass:
    """Fence producers and assign every currently tracked task file to delete."""
    now = time.time()
    producer_blocker_count, producer_retry_at = _drain_producer_operations(
        owner_id=owner_id,
        assignment_id=assignment_id,
        delete_operation_id=operation_id,
    )
    grading_blocker_count, grading_retry_at = _drain_grading_runs(
        owner_id=owner_id, assignment_id=assignment_id
    )
    blocker_count = producer_blocker_count + grading_blocker_count
    if blocker_count:
        return TaskDeletionPass(
            producer_blockers=blocker_count,
            next_producer_retry_at=max(
                value for value in (producer_retry_at, grading_retry_at, now + 1.0)
                if value is not None
            ),
            pending_reservations=0,
            next_reservation_retry_at=None,
            pending_files=0,
        )

    # The task is already tombstoned, so no new task-only upload can publish.
    # Detach knowledge in its own User-first transaction before taking the
    # source-deletion O/W/Assignment locks below.  Retained documents survive;
    # each task-only document is enqueued only after its final reference.
    from backend.db.knowledge_storage_repository import (
        detach_assignment_documents_in_session,
    )
    from backend.domain.knowledge_storage import KNOWLEDGE_CLEANUP_TASK_DELETED

    with session_scope() as knowledge_session:
        detach_assignment_documents_in_session(
            knowledge_session,
            assignment_id=assignment_id,
            owner_id=owner_id,
            reason=KNOWLEDGE_CLEANUP_TASK_DELETED,
        )
    with session_scope() as session:
        _lock_live_delete_operation(
            session,
            operation_id=operation_id,
            owner_id=owner_id,
            assignment_id=assignment_id,
            attempt=attempt,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        workflow = session.scalar(
            select(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id
                == assignment_id,
                workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
            )
            .with_for_update()
        )
        assignment = session.scalar(
            select(AssignmentRecord)
            .where(
                AssignmentRecord.id == assignment_id,
                AssignmentRecord.teacher_id == owner_id,
            )
            .with_for_update()
        )
        if assignment is None:
            raise NotFound("assignment")
        if assignment.deletion_requested_at is None:
            raise LeaseLost("Task deletion is no longer authorized.")

        # No new producer can cross the workflow/assignment deletion gate.
        # Recheck after draining in case an older deployment inserted one just
        # before the tombstone transaction committed.
        remaining_producer = session.scalar(
            select(workflow_repository.WorkflowOperationRecord.id).where(
                workflow_repository.WorkflowOperationRecord.assignment_id
                == assignment_id,
                workflow_repository.WorkflowOperationRecord.owner_id == owner_id,
                workflow_repository.WorkflowOperationRecord.id != operation_id,
                workflow_repository.WorkflowOperationRecord.operation_type
                != SOURCE_RESERVATION_CLEANUP_OPERATION,
                workflow_repository.WorkflowOperationRecord.status.in_(
                    ("preparing", "pending", "ready", "running")
                ),
            ).limit(1)
        )
        if remaining_producer is not None:
            return TaskDeletionPass(
                producer_blockers=1,
                next_producer_retry_at=now + 1.0,
                pending_reservations=0,
                next_reservation_retry_at=None,
                pending_files=0,
            )
        remaining_run = session.scalar(
            select(GradingRunRecord.id).where(
                GradingRunRecord.assignment_id == assignment_id,
                GradingRunRecord.teacher_id == owner_id,
                GradingRunRecord.status.in_(tuple(
                    education.ACTIVE_GRADING_RUN_STATUSES
                )),
            ).limit(1)
        )
        if remaining_run is not None:
            return TaskDeletionPass(
                producer_blockers=1,
                next_producer_retry_at=now + 1.0,
                pending_reservations=0,
                next_reservation_retry_at=None,
                pending_files=0,
            )
        if workflow is not None:
            workflow.active_operation = None
            workflow.active_job_id = None
            workflow.error_code = None
            workflow.source_lifecycle_epoch += 1
            workflow.updated_at = now

        reservation_summary = session.execute(
            select(
                func.count(SourceStorageReservationRecord.id),
                func.min(SourceStorageReservationRecord.expires_at),
            ).where(
                SourceStorageReservationRecord.assignment_id == assignment_id
            )
        ).one()
        pending_reservations = int(reservation_summary[0] or 0)

        rows = list(session.scalars(
            select(StoredFileRecord)
            .where(
                StoredFileRecord.availability_status != SOURCE_FILE_UNAVAILABLE,
                _belongs_to_task(assignment_id),
            )
            .order_by(StoredFileRecord.id.asc())
            .with_for_update()
        ))
        for row in rows:
            row.availability_status = SOURCE_FILE_CLEANUP_PENDING
            if row.availability_reason != SOURCE_REASON_STORAGE_DELETE_FAILED:
                row.availability_reason = SOURCE_REASON_TASK_DELETED
            row.cleanup_operation_id = operation_id
            row.cleanup_final_result_version = None
            row.cleanup_requested_at = row.cleanup_requested_at or now
            row.cleanup_claim_token = None
            row.cleanup_claimed_at = None
            row.unavailable_at = None
            row.lifecycle_revision += 1

        return TaskDeletionPass(
            producer_blockers=0,
            next_producer_retry_at=None,
            pending_reservations=pending_reservations,
            next_reservation_retry_at=(
                float(reservation_summary[1])
                if reservation_summary[1] is not None
                else None
            ),
            pending_files=len(rows),
        )


def task_storage_prefixes(
    *,
    operation_id: str,
    owner_id: str,
    assignment_id: str,
    attempt: int,
    worker_id: str,
    lease_token: str,
) -> tuple[str, ...]:
    """Return the exact task-owned prefixes while the delete lease is live."""
    with session_scope() as session:
        _lock_live_delete_operation(
            session,
            operation_id=operation_id,
            owner_id=owner_id,
            assignment_id=assignment_id,
            attempt=attempt,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        assignment = session.scalar(select(AssignmentRecord.id).where(
            AssignmentRecord.id == assignment_id,
            AssignmentRecord.teacher_id == owner_id,
            AssignmentRecord.deletion_requested_at.is_not(None),
        ))
        if assignment is None:
            raise NotFound("assignment")
        revision_ids = tuple(str(value) for value in session.scalars(
            _task_revision_ids(assignment_id).order_by(
                SubmissionRevisionRecord.id.asc()
            )
        ))
        return (
            f"assignments/{assignment_id}/",
            *(f"revisions/{revision_id}/" for revision_id in revision_ids),
        )


def claim_next_task_file(
    *,
    operation_id: str,
    owner_id: str,
    assignment_id: str,
    attempt: int,
    worker_id: str,
    lease_token: str,
) -> TaskFileDeleteClaim:
    now = time.time()
    with session_scope() as session:
        _lock_live_delete_operation(
            session,
            operation_id=operation_id,
            owner_id=owner_id,
            assignment_id=assignment_id,
            attempt=attempt,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        row = session.scalar(
            select(StoredFileRecord)
            .where(
                StoredFileRecord.availability_status != SOURCE_FILE_UNAVAILABLE,
                _belongs_to_task(assignment_id),
            )
            .order_by(StoredFileRecord.id.asc())
            .limit(1)
            .with_for_update()
        )
        if row is None:
            return TaskFileDeleteClaim("empty", "")
        row.availability_status = SOURCE_FILE_CLEANUP_PENDING
        row.cleanup_operation_id = operation_id
        row.cleanup_final_result_version = None
        row.cleanup_requested_at = row.cleanup_requested_at or now
        row.cleanup_last_attempt_at = now
        row.cleanup_attempt_count += 1
        row.cleanup_claim_token = uuid.uuid4().hex
        row.cleanup_claimed_at = now
        row.lifecycle_revision += 1
        return TaskFileDeleteClaim(
            "ready",
            row.id,
            storage_backend=row.storage_backend,
            storage_key=row.storage_key,
            claim_token=row.cleanup_claim_token,
        )


def finish_task_file_delete(
    *,
    operation_id: str,
    owner_id: str,
    assignment_id: str,
    attempt: int,
    worker_id: str,
    lease_token: str,
    file_id: str,
    claim_token: str,
    deleted: bool,
) -> str:
    now = time.time()
    with session_scope() as session:
        _lock_live_delete_operation(
            session,
            operation_id=operation_id,
            owner_id=owner_id,
            assignment_id=assignment_id,
            attempt=attempt,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        row = session.scalar(
            select(StoredFileRecord)
            .where(
                StoredFileRecord.id == file_id,
                _belongs_to_task(assignment_id),
            )
            .with_for_update()
        )
        if row is None or row.availability_status == SOURCE_FILE_UNAVAILABLE:
            return "already_deleted"
        if (
            row.cleanup_operation_id != operation_id
            or row.cleanup_claim_token != claim_token
        ):
            return "superseded"
        row.cleanup_claim_token = None
        row.cleanup_claimed_at = None
        if not deleted:
            row.availability_status = SOURCE_FILE_CLEANUP_PENDING
            row.availability_reason = SOURCE_REASON_STORAGE_DELETE_FAILED
            row.lifecycle_revision += 1
            return "failed"
        if row.source_quota_owner_id is not None:
            source_storage_repository._lock_quota_owner(
                session, row.source_quota_owner_id
            )
        row.availability_status = SOURCE_FILE_UNAVAILABLE
        row.availability_reason = SOURCE_REASON_TASK_DELETED
        row.unavailable_at = now
        row.lifecycle_revision += 1
        return "deleted"


def finalize_task_deletion(
    *,
    operation_id: str,
    owner_id: str,
    assignment_id: str,
    attempt: int,
    worker_id: str,
    lease_token: str,
) -> bool:
    """Hard-delete only after every tracked physical object is absent."""
    with session_scope() as session:
        _lock_live_delete_operation(
            session,
            operation_id=operation_id,
            owner_id=owner_id,
            assignment_id=assignment_id,
            attempt=attempt,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        # A parent CASCADE will lock producer children. Acquire those rows
        # explicitly before W/A so we never hold a parent gate while waiting
        # on a producer that is itself following O/G -> W -> A.
        list(session.scalars(
            select(workflow_repository.WorkflowOperationRecord)
            .where(
                workflow_repository.WorkflowOperationRecord.assignment_id
                == assignment_id,
                workflow_repository.WorkflowOperationRecord.id != operation_id,
            )
            .order_by(workflow_repository.WorkflowOperationRecord.id.asc())
            .with_for_update()
        ))
        list(session.scalars(
            select(GradingRunRecord)
            .where(GradingRunRecord.assignment_id == assignment_id)
            .order_by(GradingRunRecord.id.asc())
            .with_for_update()
        ))
        session.scalar(
            select(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id
                == assignment_id,
                workflow_repository.AssignmentWorkflowRecord.owner_id
                == owner_id,
            )
            .with_for_update()
        )
        assignment = session.scalar(
            select(AssignmentRecord)
            .where(
                AssignmentRecord.id == assignment_id,
                AssignmentRecord.teacher_id == owner_id,
                AssignmentRecord.deletion_requested_at.is_not(None),
            )
            .with_for_update()
        )
        if assignment is None:
            return True
        reservation_id = session.scalar(
            select(SourceStorageReservationRecord.id)
            .where(SourceStorageReservationRecord.assignment_id == assignment_id)
            .limit(1)
            .with_for_update()
        )
        live_file_id = session.scalar(
            select(StoredFileRecord.id)
            .where(
                StoredFileRecord.availability_status != SOURCE_FILE_UNAVAILABLE,
                _belongs_to_task(assignment_id),
            )
            .limit(1)
            .with_for_update()
        )
        if reservation_id is not None or live_file_id is not None:
            return False
        imported_user_ids = _task_import_user_ids(session, assignment_id)
        assignment_course_id = assignment.course_id
        # PostgreSQL enforces these RESTRICT links immediately.  Delete the
        # task-owned source graph explicitly before the assignment's parallel
        # CASCADE removes both operations and StoredFile rows.
        from backend.db.source_outcome_repository import (
            WorkflowSourceItemRecord,
            WorkflowSourceOutcomeRecord,
        )

        source_ids = select(WorkflowSourceItemRecord.id).where(
            WorkflowSourceItemRecord.assignment_id == assignment_id
        )
        session.execute(
            delete(WorkflowSourceOutcomeRecord).where(
                WorkflowSourceOutcomeRecord.source_id.in_(source_ids)
            )
        )
        session.execute(
            delete(WorkflowSourceItemRecord).where(
                WorkflowSourceItemRecord.assignment_id == assignment_id
            )
        )
        session.flush()
        session.delete(assignment)
        session.flush()
        _delete_unreferenced_task_import_users(
            session,
            candidate_ids=imported_user_ids,
            course_id=assignment_course_id,
        )
        session.flush()
        return True
