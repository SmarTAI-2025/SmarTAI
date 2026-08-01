"""Immutable submission revision and answer access.

A student has exactly one current submission per assignment; resubmission
creates a *new immutable revision* (numbered 1, 2, 3, …) and repoints
``current_revision_id`` at it. Prior revisions are never mutated, so a regrade
or audit can always address the exact snapshot a run graded.

Both student-online and teacher-import ingestion flow through ``add_revision``:
the only difference is the ``source`` enum. Answers are persisted as normalized
``submission_answers`` rows (one per question per revision), and the
(revision, question) uniqueness is enforced in SQL.

Reads are student-scoped (the owner student) or teacher-scoped (the assignment's
teacher); the predicate lives in SQL so a non-actor reads nothing.
"""
from __future__ import annotations

import time
import uuid

from sqlalchemy import func, select

from backend.db.models import (
    AssignmentRecord,
    CourseEnrollmentRecord,
    SubmissionAnswerRecord,
    SubmissionRecord,
    SubmissionRevisionRecord,
)
from backend.db.session import session_scope
from backend.domain import education
from backend.domain.errors import AssignmentClosed, Forbidden, NotFound


def _new_submission_id() -> str:
    return f"sub_{uuid.uuid4().hex[:12]}"


def _new_revision_id() -> str:
    return f"rev_{uuid.uuid4().hex[:12]}"


def _new_answer_id() -> str:
    return f"ans_{uuid.uuid4().hex[:12]}"


def _submission_to_dto(record: SubmissionRecord, revision: SubmissionRevisionRecord | None) -> education.SubmissionDTO:
    return education.SubmissionDTO(
        id=record.id,
        assignment_id=record.assignment_id,
        student_id=record.student_id,
        current_revision_id=record.current_revision_id,
        current_revision_number=revision.revision_number if revision else None,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _require_open_assignment(session, assignment_id: str, student_id: str) -> AssignmentRecord:
    """Authorize a student submission without creating any rows."""
    assignment = session.scalar(
        select(AssignmentRecord).where(AssignmentRecord.id == assignment_id)
    )
    if assignment is None:
        raise NotFound("assignment")
    enrolled = session.scalar(
        select(CourseEnrollmentRecord).where(
            CourseEnrollmentRecord.course_id == assignment.course_id,
            CourseEnrollmentRecord.student_id == student_id,
        )
    )
    if enrolled is None:
        raise Forbidden("not_enrolled")
    if assignment.status != education.AssignmentStatus.PUBLISHED.value:
        raise AssignmentClosed("assignment_closed")
    return assignment


def validate_submission_access(assignment_id: str, *, student_id: str) -> None:
    """Validate enrollment/open state before an expensive OCR or LLM call."""
    with session_scope() as session:
        _require_open_assignment(session, assignment_id, student_id)


def create_submission(assignment_id: str, *, student_id: str) -> education.SubmissionDTO:
    """Create the (single) current submission for a student, gated by enrollment
    and by the assignment being open for submissions.

    The upsert is idempotent on (assignment, student): if a row already exists it
    is returned unchanged rather than violating the unique constraint.
    """
    now = time.time()
    with session_scope() as session:
        _require_open_assignment(session, assignment_id, student_id)
        existing = session.scalar(
            select(SubmissionRecord).where(
                SubmissionRecord.assignment_id == assignment_id,
                SubmissionRecord.student_id == student_id,
            )
        )
        if existing is not None:
            revision = session.get(SubmissionRevisionRecord, existing.current_revision_id) if existing.current_revision_id else None
            return _submission_to_dto(existing, revision)
        record = SubmissionRecord(
            id=_new_submission_id(),
            assignment_id=assignment_id,
            student_id=student_id,
            current_revision_id=None,
            created_at=now,
            updated_at=now,
        )
        session.add(record)
        session.flush()
        return _submission_to_dto(record, None)


def add_revision(submission_id: str, *, student_id: str, source: str,
                 file_name: str = "",
                 answers: list[dict] | None = None) -> education.SubmissionRevisionDTO:
    """Append a new immutable revision with its answer rows.

    ``answers`` is a list of dicts with keys ``question_id``, ``q_id``,
    ``type``, ``number``, ``content``, ``flag``.
    """
    now = time.time()
    with session_scope() as session:
        submission = session.scalar(
            select(SubmissionRecord).where(
                SubmissionRecord.id == submission_id,
                SubmissionRecord.student_id == student_id,
            )
        )
        if submission is None:
            raise NotFound("submission")
        assignment = session.get(AssignmentRecord, submission.assignment_id)
        if assignment is None:
            raise NotFound("assignment")
        if assignment.status != education.AssignmentStatus.PUBLISHED.value:
            raise AssignmentClosed("assignment_closed")
        next_number = (session.scalar(
            select(func.max(SubmissionRevisionRecord.revision_number)).where(
                SubmissionRevisionRecord.submission_id == submission_id
            )
        ) or 0) + 1
        revision_id = _new_revision_id()
        revision = SubmissionRevisionRecord(
            id=revision_id,
            submission_id=submission_id,
            revision_number=next_number,
            source=source,
            file_name=file_name,
            created_at=now,
        )
        session.add(revision)
        session.flush()  # materialize revision so answer FKs resolve on SQLite
        for ans in answers or []:
            session.add(
                SubmissionAnswerRecord(
                    id=_new_answer_id(),
                    revision_id=revision_id,
                    question_id=ans["question_id"],
                    q_id=ans["q_id"],
                    number=ans.get("number", ""),
                    type=ans.get("type", ""),
                    content=ans.get("content", ""),
                    flag=ans.get("flag", []),
                    created_at=now,
                )
            )
        # Repoint current revision atomically in the same transaction.
        submission.current_revision_id = revision_id
        submission.updated_at = now
        # Build the DTO in this session: get_revision would open a separate
        # session that cannot see the uncommitted revision row.
        answer_rows = session.scalars(
            select(SubmissionAnswerRecord)
            .where(SubmissionAnswerRecord.revision_id == revision_id)
            .order_by(SubmissionAnswerRecord.id)
        ).all()
        return education.SubmissionRevisionDTO(
            id=revision.id,
            submission_id=revision.submission_id,
            revision_number=revision.revision_number,
            source=revision.source,
            file_name=revision.file_name or "",
            created_at=revision.created_at,
            answers=[
                education.SubmissionAnswerDTO(
                    id=a.id, revision_id=a.revision_id, question_id=a.question_id, q_id=a.q_id,
                    number=a.number or "", type=a.type or "", content=a.content or "",
                    flag=list(a.flag or []),
                )
                for a in answer_rows
            ],
        )


def get_submission(submission_id: str, *, actor_id: str) -> education.SubmissionDTO:
    with session_scope() as session:
        # Student reads their own submission; teacher reads through the assignment.
        record = session.scalar(
            select(SubmissionRecord).where(SubmissionRecord.id == submission_id)
        )
        if record is None:
            raise NotFound("submission")
        _authorize_read(session, record, actor_id)
        revision = session.get(SubmissionRevisionRecord, record.current_revision_id) if record.current_revision_id else None
        return _submission_to_dto(record, revision)


def list_submissions(assignment_id: str, *, actor_id: str) -> list[education.SubmissionDTO]:
    with session_scope() as session:
        assignment = session.scalar(
            select(AssignmentRecord).where(AssignmentRecord.id == assignment_id)
        )
        if assignment is None:
            raise NotFound("assignment")
        query = select(SubmissionRecord).where(SubmissionRecord.assignment_id == assignment_id)
        if actor_id != assignment.teacher_id:
            # A student only sees their own submission for this assignment.
            query = query.where(SubmissionRecord.student_id == actor_id)
        records = session.scalars(query.order_by(SubmissionRecord.created_at)).all()
        out: list[education.SubmissionDTO] = []
        for r in records:
            revision = session.get(SubmissionRevisionRecord, r.current_revision_id) if r.current_revision_id else None
            out.append(_submission_to_dto(r, revision))
        return out


def get_submission_for_student(assignment_id: str, *, student_id: str) -> education.SubmissionDTO | None:
    with session_scope() as session:
        record = session.scalar(
            select(SubmissionRecord).where(
                SubmissionRecord.assignment_id == assignment_id,
                SubmissionRecord.student_id == student_id,
            )
        )
        if record is None:
            return None
        revision = session.get(SubmissionRevisionRecord, record.current_revision_id) if record.current_revision_id else None
        return _submission_to_dto(record, revision)


def get_revision(revision_id: str, *, actor_id: str) -> education.SubmissionRevisionDTO:
    with session_scope() as session:
        revision = session.get(SubmissionRevisionRecord, revision_id)
        if revision is None:
            raise NotFound("revision")
        submission = session.get(SubmissionRecord, revision.submission_id)
        assert submission is not None
        _authorize_read(session, submission, actor_id)
        answers = session.scalars(
            select(SubmissionAnswerRecord)
            .where(SubmissionAnswerRecord.revision_id == revision_id)
            .order_by(SubmissionAnswerRecord.id)
        ).all()
        return education.SubmissionRevisionDTO(
            id=revision.id,
            submission_id=revision.submission_id,
            revision_number=revision.revision_number,
            source=revision.source,
            file_name=revision.file_name or "",
            created_at=revision.created_at,
            answers=[
                education.SubmissionAnswerDTO(
                    id=a.id,
                    revision_id=a.revision_id,
                    question_id=a.question_id,
                    q_id=a.q_id,
                    number=a.number or "",
                    type=a.type or "",
                    content=a.content or "",
                    flag=list(a.flag or []),
                )
                for a in answers
            ],
        )


def get_current_revision_for_run(submission_id: str) -> education.SubmissionRevisionDTO | None:
    """Unscoped current-revision read for grading run assembly (already authorized)."""
    with session_scope() as session:
        submission = session.get(SubmissionRecord, submission_id)
        if submission is None or submission.current_revision_id is None:
            return None
        revision = session.get(SubmissionRevisionRecord, submission.current_revision_id)
        assert revision is not None
        answers = session.scalars(
            select(SubmissionAnswerRecord)
            .where(SubmissionAnswerRecord.revision_id == revision.id)
            .order_by(SubmissionAnswerRecord.id)
        ).all()
        return education.SubmissionRevisionDTO(
            id=revision.id,
            submission_id=revision.submission_id,
            revision_number=revision.revision_number,
            source=revision.source,
            file_name=revision.file_name or "",
            created_at=revision.created_at,
            answers=[
                education.SubmissionAnswerDTO(
                    id=a.id, revision_id=a.revision_id, question_id=a.question_id, q_id=a.q_id,
                    number=a.number or "", type=a.type or "", content=a.content or "",
                    flag=list(a.flag or []),
                )
                for a in answers
            ],
        )


def _authorize_read(session, submission: SubmissionRecord, actor_id: str) -> None:
    if submission.student_id == actor_id:
        return
    assignment = session.get(AssignmentRecord, submission.assignment_id)
    if assignment is not None and assignment.teacher_id == actor_id:
        return
    raise NotFound("submission")
