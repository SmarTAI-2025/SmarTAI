"""Retain source files when completion would prevent a safe retry.

Replacement and explicit task deletion use separate authorized lifecycles.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db.models import (
    AssignmentQuestionRecord, GradingRunRecord, GradingRunSubmissionRecord,
    StoredFileRecord, SubmissionAnswerRecord, SubmissionRecord,
    SubmissionRevisionRecord,
)
from backend.db.source_outcome_repository import (
    WorkflowSourceItemRecord, WorkflowSourceOutcomeRecord,
)
from backend.db.workflow_repository import GradingRunSetupRecord, WorkflowOperationRecord
from backend.domain.source_storage import TASK_SOURCE_CLEANUP_KINDS


def completion_inputs_are_recoverable(
    session: Session, *, assignment_id: str, owner_id: str,
    grading_run_id: str, source_file_ids: tuple[str, ...] | None,
) -> bool:
    """Check persisted input facts while the caller holds the workflow lock.

    Partial grading can retry from saved answers. Recognition failure still
    needs the original file, including the archive containing a failed member.
    """
    run = session.scalar(select(GradingRunRecord).where(
        GradingRunRecord.id == grading_run_id,
        GradingRunRecord.assignment_id == assignment_id,
        GradingRunRecord.teacher_id == owner_id,
    ))
    if run is None:
        return False
    setup = session.get(GradingRunSetupRecord, grading_run_id)
    if setup is not None:
        if setup.owner_id != owner_id or setup.assignment_id != assignment_id:
            return False
        if not isinstance(setup.input_manifest, dict):
            return False
        questions = setup.input_manifest.get("questions")
        if not isinstance(questions, list) or not questions or any(
            not isinstance(q, dict) or not isinstance(q.get("q_id"), str)
            or not q["q_id"] for q in questions
        ):
            return False
        question_ids = {q["q_id"] for q in questions}
        if len(question_ids) != len(questions):
            return False
    else:
        question_ids = set(session.scalars(select(AssignmentQuestionRecord.q_id).where(
            AssignmentQuestionRecord.assignment_id == assignment_id,
        )))
    if not question_ids:
        return False
    revision_ids = set(session.scalars(select(GradingRunSubmissionRecord.submission_revision_id).where(
        GradingRunSubmissionRecord.grading_run_id == grading_run_id,
    )))
    if not revision_ids:
        return False
    if setup is not None:
        expected_revisions = setup.input_manifest.get("submission_revision_ids")
        if (
            not isinstance(expected_revisions, list)
            or any(not isinstance(value, str) or not value for value in expected_revisions)
            or set(expected_revisions) != revision_ids
        ):
            return False
    elif run.total_submissions > 0 and len(revision_ids) != run.total_submissions:
        return False
    owned_revisions = set(session.scalars(select(SubmissionRevisionRecord.id)
        .join(SubmissionRecord, SubmissionRecord.id == SubmissionRevisionRecord.submission_id)
        .where(SubmissionRevisionRecord.id.in_(revision_ids),
               SubmissionRecord.assignment_id == assignment_id)))
    if owned_revisions != revision_ids:
        return False
    answer_ids: dict[str, set[str]] = {revision_id: set() for revision_id in revision_ids}
    for revision_id, q_id in session.execute(select(
        SubmissionAnswerRecord.revision_id, SubmissionAnswerRecord.q_id,
    ).where(SubmissionAnswerRecord.revision_id.in_(revision_ids))):
        answer_ids[revision_id].add(q_id)
    if any(not question_ids.issubset(ids) for ids in answer_ids.values()):
        return False

    if source_file_ids is None:
        candidate_ids = set(session.scalars(select(StoredFileRecord.id).where(
            StoredFileRecord.assignment_id == assignment_id,
            StoredFileRecord.source_quota_owner_id == owner_id,
            StoredFileRecord.kind.in_(tuple(TASK_SOURCE_CLEANUP_KINDS)),
            StoredFileRecord.availability_status != "unavailable",
        )))
    else:
        candidate_ids = set(source_file_ids)
    if not candidate_ids:
        return True

    # Follow persisted archive relationships, never infer them from filenames.
    container_operations: set[str] = set()
    for operation in session.scalars(select(WorkflowOperationRecord).where(
        WorkflowOperationRecord.assignment_id == assignment_id,
        WorkflowOperationRecord.owner_id == owner_id,
        WorkflowOperationRecord.operation_type == "submission_recognition",
    )):
        references = {value for value in (operation.artifact_refs or []) if isinstance(value, str)}
        checkpoint = operation.checkpoint if isinstance(operation.checkpoint, dict) else {}
        container_id = checkpoint.get("container_file_id")
        if isinstance(container_id, str):
            references.add(container_id)
        if references & candidate_ids:
            container_operations.add(operation.id)

    latest: dict[str, tuple[str, str | None]] = {}
    for source, status in session.execute(select(
        WorkflowSourceItemRecord, WorkflowSourceOutcomeRecord.status,
    ).outerjoin(WorkflowSourceOutcomeRecord,
        WorkflowSourceOutcomeRecord.source_id == WorkflowSourceItemRecord.id)
      .where(WorkflowSourceItemRecord.assignment_id == assignment_id,
             WorkflowSourceItemRecord.owner_id == owner_id)
      .order_by(WorkflowSourceItemRecord.created_at.desc(),
                WorkflowSourceItemRecord.attempt.desc(), WorkflowSourceItemRecord.id.desc())):
        latest.setdefault(source.stored_file_id, (source.operation_id, status))
    for stored_file_id, (operation_id, status) in latest.items():
        if (stored_file_id in candidate_ids or operation_id in container_operations) and status != "parsed":
            return False
    return True
