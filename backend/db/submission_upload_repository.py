"""Lease-fenced staging and atomic publication of recognized submissions."""
from __future__ import annotations

import time

from sqlalchemy import func, select

from backend.db import submission_repository as submissions
from backend.db.models import AssignmentQuestionRecord, SubmissionAnswerRecord, SubmissionRecord, SubmissionRevisionRecord
from backend.db.session import session_scope
from backend.db.source_outcome_repository import _lock_source_write_operation
from backend.db.workflow_repository import _lock_live_assignment
from backend.domain.errors import NotFound, VersionConflict


def _locked(session, operation, student_id):
    row = _lock_source_write_operation(
        session, owner_id=operation.owner_id, assignment_id=operation.assignment_id,
        operation_id=operation.id, expected_attempt=operation.attempt,
        expected_lease_token=operation.lease_token,
    )
    _lock_live_assignment(session, assignment_id=operation.assignment_id, owner_id=operation.owner_id)
    submissions._require_open_assignment(session, operation.assignment_id, student_id)
    submission = session.scalar(select(SubmissionRecord).where(
        SubmissionRecord.assignment_id == operation.assignment_id,
        SubmissionRecord.student_id == student_id,
    ).with_for_update())
    if submission is None:
        raise NotFound("submission")
    return row, submission


def stage_revision(*, operation, student_id, source, filename):
    with session_scope() as session:
        row, submission = _locked(session, operation, student_id)
        checkpoint = dict(row.checkpoint or {})
        revision_id = checkpoint.get("revision_id")
        if revision_id:
            revision = session.get(SubmissionRevisionRecord, revision_id)
            if revision is None or revision.submission_id != submission.id:
                raise NotFound("revision")
            return revision_id
        number = (session.scalar(select(func.max(SubmissionRevisionRecord.revision_number)).where(
            SubmissionRevisionRecord.submission_id == submission.id,
        )) or 0) + 1
        revision_id = submissions._new_revision_id()
        session.add(SubmissionRevisionRecord(
            id=revision_id, submission_id=submission.id, revision_number=number,
            source=source, file_name=filename, created_at=time.time(),
        ))
        checkpoint.update(revision_id=revision_id, base_revision_id=submission.current_revision_id)
        row.checkpoint = checkpoint
        row.checkpoint_revision += 1
        row.checkpoint_stage = "submission_staged"
        row.updated_at = time.time()
        return revision_id


def publish_revision(*, operation, student_id, answers, expected_questions):
    """Do not replace a newer manual correction or a competing upload."""
    with session_scope() as session:
        row, submission = _locked(session, operation, student_id)
        current_questions = session.execute(select(AssignmentQuestionRecord.id, AssignmentQuestionRecord.version)
            .where(AssignmentQuestionRecord.assignment_id == operation.assignment_id).with_for_update()).all()
        if sorted(tuple(item) for item in current_questions) != sorted(expected_questions):
            raise VersionConflict("Questions changed during recognition.", code="recognition_plan_changed")
        checkpoint = dict(row.checkpoint or {})
        revision_id = checkpoint.get("revision_id")
        revision = session.get(SubmissionRevisionRecord, revision_id)
        if revision is None or revision.submission_id != submission.id:
            raise NotFound("revision")
        if submission.current_revision_id != checkpoint.get("base_revision_id"):
            raise VersionConflict("Submission changed during recognition.", code="submission_revision_changed")
        now = time.time()
        for answer in answers:
            session.add(SubmissionAnswerRecord(
                id=submissions._new_answer_id(), revision_id=revision_id,
                question_id=answer["question_id"], q_id=answer["q_id"],
                type=answer.get("type", ""), number=answer.get("number") or "",
                content=answer.get("content", ""), flag=answer.get("flag", []), created_at=now,
            ))
        submission.current_revision_id = revision_id
        submission.updated_at = now
        row.status = "completed"
        row.completed_at = now
        row.updated_at = now
        row.checkpoint_stage = "submission_published"
        row.checkpoint_revision += 1
        row.terminal_summary = {"revision_id": revision_id}
    return submissions.get_revision(revision_id, actor_id=student_id)
