"""Assignment and question access with optimistic locking.

Editable assignment/question rows carry a ``version`` column. Every mutating
operation is a single conditional UPDATE that matches ``teacher_id`` (ownership),
``id``/``q_id``, and the expected ``version`` in its WHERE clause, then bumps
the version. ``rowcount != 1`` therefore distinguishes the three failure modes
without a read-then-write race:

* no row matched the owner → ``NotFound`` (no payload leak);
* row exists for the owner but version differs → ``VersionConflict``;
* a state guard (e.g. publishing an empty or already-published assignment) →
  ``InvalidTransition``.

Published questions are frozen: ``update_question`` refuses once the parent
assignment leaves the editable status set.
"""
from __future__ import annotations

import time
import uuid

from sqlalchemy import func, select, update

from backend.db.models import AssignmentQuestionRecord, AssignmentRecord, CourseRecord
from backend.db.session import session_scope
from backend.domain import education
from backend.domain.errors import InvalidTransition, NotFound, VersionConflict


def _new_assignment_id() -> str:
    return f"asg_{uuid.uuid4().hex[:12]}"


def _new_question_id() -> str:
    return f"q_{uuid.uuid4().hex[:12]}"


def _question_to_dto(record: AssignmentQuestionRecord) -> education.QuestionDTO:
    return education.QuestionDTO(
        id=record.id,
        assignment_id=record.assignment_id,
        q_id=record.q_id,
        order_index=record.order_index,
        number=record.number or "",
        type=record.type,
        stem=record.stem or "",
        criterion=record.criterion or "",
        max_score=record.max_score,
        reference_answer=record.reference_answer,
        test_cases=record.test_cases,
        source=record.source,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _assignment_to_dto(record: AssignmentRecord, question_count: int) -> education.AssignmentDTO:
    return education.AssignmentDTO(
        id=record.id,
        course_id=record.course_id,
        teacher_id=record.teacher_id,
        name=record.name,
        description=record.description or "",
        status=record.status,
        due_at=record.due_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
        published_at=record.published_at,
        version=record.version,
        question_count=question_count,
    )


def _dto_from_session(session, record: AssignmentRecord) -> education.AssignmentDTO:
    """Build a DTO from a record held in the *current* session.

    Calling the public ``get_assignment`` would open a separate session that
    cannot see this transaction's uncommitted UPDATE, so post-update DTOs are
    assembled here against the same session.
    """
    count = session.scalar(
        select(func.count()).select_from(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == record.id
        )
    ) or 0
    return _assignment_to_dto(record, count)


def create_assignment(*, teacher_id: str, course_id: str, name: str, description: str = "",
                      due_at: float | None = None) -> education.AssignmentDTO:
    assignment_id = _new_assignment_id()
    now = time.time()
    with session_scope() as session:
        # The course must belong to the teacher; otherwise another teacher could
        # silently create assignments inside a course they do not own.
        course = session.scalar(
            select(CourseRecord).where(
                CourseRecord.id == course_id, CourseRecord.teacher_id == teacher_id
            )
        )
        if course is None:
            raise NotFound("course")
        session.add(
            AssignmentRecord(
                id=assignment_id,
                course_id=course_id,
                teacher_id=teacher_id,
                name=name,
                description=description,
                status=education.AssignmentStatus.DRAFT.value,
                due_at=due_at,
                created_at=now,
                updated_at=now,
                published_at=None,
                version=1,
            )
        )
        session.flush()
        record = session.get(AssignmentRecord, assignment_id)
        assert record is not None
        return _assignment_to_dto(record, 0)


def get_assignment(assignment_id: str, *, actor_id: str) -> education.AssignmentDTO:
    with session_scope() as session:
        record = session.scalar(
            select(AssignmentRecord).where(
                AssignmentRecord.id == assignment_id, AssignmentRecord.teacher_id == actor_id
            )
        )
        if record is None:
            raise NotFound("assignment")
        count = session.scalar(
            select(func.count()).select_from(AssignmentQuestionRecord).where(
                AssignmentQuestionRecord.assignment_id == assignment_id
            )
        ) or 0
        return _assignment_to_dto(record, count)


def list_assignments(course_id: str, *, actor_id: str) -> list[education.AssignmentDTO]:
    with session_scope() as session:
        records = session.scalars(
            select(AssignmentRecord)
            .where(AssignmentRecord.course_id == course_id, AssignmentRecord.teacher_id == actor_id)
            .order_by(AssignmentRecord.created_at)
        ).all()
        out: list[education.AssignmentDTO] = []
        for r in records:
            count = session.scalar(
                select(func.count()).select_from(AssignmentQuestionRecord).where(
                    AssignmentQuestionRecord.assignment_id == r.id
                )
            ) or 0
            out.append(_assignment_to_dto(r, count))
        return out


def list_assignments_for_student(student_id: str) -> list[education.AssignmentDTO]:
    """Published or closed assignments in courses the student is enrolled in.

    Enrollment is joined in SQL (authorization source = course_enrollments),
    and closed assignments remain visible so released results stay reachable.
    """
    from backend.db.models import CourseEnrollmentRecord

    with session_scope() as session:
        records = session.scalars(
            select(AssignmentRecord)
            .join(CourseEnrollmentRecord, CourseEnrollmentRecord.course_id == AssignmentRecord.course_id)
            .where(
                CourseEnrollmentRecord.student_id == student_id,
                AssignmentRecord.status.in_([
                    education.AssignmentStatus.PUBLISHED.value,
                    education.AssignmentStatus.CLOSED.value,
                ]),
            )
            .order_by(AssignmentRecord.created_at)
        ).all()
        out: list[education.AssignmentDTO] = []
        for r in records:
            count = session.scalar(
                select(func.count()).select_from(AssignmentQuestionRecord).where(
                    AssignmentQuestionRecord.assignment_id == r.id
                )
            ) or 0
            out.append(_assignment_to_dto(r, count))
        return out


def get_assignment_unscoped(assignment_id: str) -> education.AssignmentDTO:
    """Admin unscoped read of a single assignment."""
    with session_scope() as session:
        record = session.get(AssignmentRecord, assignment_id)
        if record is None:
            raise NotFound("assignment")
        count = session.scalar(
            select(func.count()).select_from(AssignmentQuestionRecord).where(
                AssignmentQuestionRecord.assignment_id == assignment_id
            )
        ) or 0
        return _assignment_to_dto(record, count)


def list_assignments_unscoped(course_id: str) -> list[education.AssignmentDTO]:
    """Admin unscoped read of every assignment in a course."""
    with session_scope() as session:
        records = session.scalars(
            select(AssignmentRecord).where(AssignmentRecord.course_id == course_id)
            .order_by(AssignmentRecord.created_at)
        ).all()
        out: list[education.AssignmentDTO] = []
        for r in records:
            count = session.scalar(
                select(func.count()).select_from(AssignmentQuestionRecord).where(
                    AssignmentQuestionRecord.assignment_id == r.id
                )
            ) or 0
            out.append(_assignment_to_dto(r, count))
        return out


def set_question_order(assignment_id: str, *, teacher_id: str,
                       ordered_q_ids: list[str]) -> list[education.QuestionDTO]:
    """Apply a new display order to a draft/ready assignment's questions.

    A single conditional UPDATE per question carries the teacher_id predicate so
    only the owner can reorder; non-editable assignments are rejected.
    """
    now = time.time()
    with session_scope() as session:
        assignment = session.scalar(
            select(AssignmentRecord).where(
                AssignmentRecord.id == assignment_id, AssignmentRecord.teacher_id == teacher_id
            )
        )
        if assignment is None:
            raise NotFound("assignment")
        if assignment.status not in education.EDITABLE_ASSIGNMENT_STATUSES:
            raise InvalidTransition("assignment_not_editable")
        for index, q_id in enumerate(ordered_q_ids):
            session.execute(
                update(AssignmentQuestionRecord)
                .where(
                    AssignmentQuestionRecord.assignment_id == assignment_id,
                    AssignmentQuestionRecord.q_id == q_id,
                )
                .values(order_index=index, updated_at=now)
            )
        return list_questions(assignment_id=assignment_id, teacher_id=teacher_id)


def add_question(assignment_id: str, *, teacher_id: str, q_id: str, order_index: int,
                 type: str, stem: str = "", number: str = "", criterion: str = "",
                 max_score: float = 10.0, reference_answer: str | None = None,
                 test_cases: list | None = None, source: dict | None = None) -> education.QuestionDTO:
    question_pk = _new_question_id()
    now = time.time()
    with session_scope() as session:
        # Owner predicate: only the assignment's teacher may add questions.
        assignment = session.scalar(
            select(AssignmentRecord).where(
                AssignmentRecord.id == assignment_id, AssignmentRecord.teacher_id == teacher_id
            )
        )
        if assignment is None:
            raise NotFound("assignment")
        if assignment.status not in education.EDITABLE_ASSIGNMENT_STATUSES:
            raise InvalidTransition("assignment_not_editable")
        record = AssignmentQuestionRecord(
            id=question_pk,
            assignment_id=assignment_id,
            q_id=q_id,
            order_index=order_index,
            number=number,
            type=type,
            stem=stem,
            criterion=criterion,
            max_score=max_score,
            reference_answer=reference_answer,
            test_cases=test_cases,
            source=source,
            version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(record)
        session.flush()
        return _question_to_dto(record)


def list_questions(assignment_id: str, *, teacher_id: str) -> list[education.QuestionDTO]:
    with session_scope() as session:
        # Owner-scoped through the assignment; a non-owner reads nothing.
        records = session.scalars(
            select(AssignmentQuestionRecord)
            .join(AssignmentRecord, AssignmentRecord.id == AssignmentQuestionRecord.assignment_id)
            .where(
                AssignmentQuestionRecord.assignment_id == assignment_id,
                AssignmentRecord.teacher_id == teacher_id,
            )
            .order_by(AssignmentQuestionRecord.order_index, AssignmentQuestionRecord.created_at)
        ).all()
        return [_question_to_dto(r) for r in records]


def get_questions_by_assignment(assignment_id: str) -> list[education.QuestionDTO]:
    """Unscoped read for grading/adapter use (run creation already authorized the teacher)."""
    with session_scope() as session:
        records = session.scalars(
            select(AssignmentQuestionRecord)
            .where(AssignmentQuestionRecord.assignment_id == assignment_id)
            .order_by(AssignmentQuestionRecord.order_index, AssignmentQuestionRecord.created_at)
        ).all()
        return [_question_to_dto(r) for r in records]


def update_question(assignment_id: str, *, teacher_id: str, q_id: str, expected_version: int,
                    **fields) -> education.QuestionDTO:
    with session_scope() as session:
        assignment = session.scalar(
            select(AssignmentRecord).where(
                AssignmentRecord.id == assignment_id, AssignmentRecord.teacher_id == teacher_id
            )
        )
        if assignment is None:
            raise NotFound("assignment")
        if assignment.status not in education.EDITABLE_ASSIGNMENT_STATUSES:
            raise InvalidTransition("assignment_not_editable")
        allowed = {"stem", "number", "criterion", "max_score", "reference_answer",
                   "test_cases", "source", "order_index", "type"}
        changes = {k: v for k, v in fields.items() if k in allowed}
        now = time.time()
        result = session.execute(
            update(AssignmentQuestionRecord)
            .where(
                AssignmentQuestionRecord.assignment_id == assignment_id,
                AssignmentQuestionRecord.q_id == q_id,
                AssignmentQuestionRecord.version == expected_version,
            )
            .values(**changes, version=expected_version + 1, updated_at=now)
        )
        if result.rowcount != 1:
            existing = session.scalar(
                select(AssignmentQuestionRecord).where(
                    AssignmentQuestionRecord.assignment_id == assignment_id,
                    AssignmentQuestionRecord.q_id == q_id,
                )
            )
            if existing is None:
                raise NotFound("question")
            raise VersionConflict("question_version_conflict")
        record = session.scalar(
            select(AssignmentQuestionRecord).where(
                AssignmentQuestionRecord.assignment_id == assignment_id,
                AssignmentQuestionRecord.q_id == q_id,
            )
        )
        assert record is not None
        return _question_to_dto(record)


def rename_assignment(assignment_id: str, *, teacher_id: str, expected_version: int,
                      name: str) -> education.AssignmentDTO:
    return _optimistic_update(
        assignment_id=assignment_id, teacher_id=teacher_id, expected_version=expected_version, name=name
    )


def _optimistic_update(assignment_id: str, *, teacher_id: str, expected_version: int,
                       **changes) -> education.AssignmentDTO:
    now = time.time()
    with session_scope() as session:
        result = session.execute(
            update(AssignmentRecord)
            .where(
                AssignmentRecord.id == assignment_id,
                AssignmentRecord.teacher_id == teacher_id,
                AssignmentRecord.version == expected_version,
            )
            .values(**changes, version=expected_version + 1, updated_at=now)
        )
        if result.rowcount != 1:
            existing = session.scalar(
                select(AssignmentRecord).where(
                    AssignmentRecord.id == assignment_id, AssignmentRecord.teacher_id == teacher_id
                )
            )
            if existing is None:
                raise NotFound("assignment")
            raise VersionConflict("assignment_version_conflict")
        updated = session.get(AssignmentRecord, assignment_id)
        assert updated is not None
        return _dto_from_session(session, updated)


def publish(assignment_id: str, *, teacher_id: str, expected_version: int) -> education.AssignmentDTO:
    now = time.time()
    with session_scope() as session:
        # A published assignment must have at least one question; otherwise the
        # student workspace would show an empty quiz. This is a state guard, not
        # a version check, so it raises InvalidTransition before any UPDATE.
        qcount = session.scalar(
            select(func.count()).select_from(AssignmentQuestionRecord).where(
                AssignmentQuestionRecord.assignment_id == assignment_id
            )
        ) or 0
        if qcount == 0:
            raise InvalidTransition("publish_requires_questions")
        result = session.execute(
            update(AssignmentRecord)
            .where(
                AssignmentRecord.id == assignment_id,
                AssignmentRecord.teacher_id == teacher_id,
                AssignmentRecord.version == expected_version,
                AssignmentRecord.status.in_(list(education.EDITABLE_ASSIGNMENT_STATUSES)),
            )
            .values(
                status=education.AssignmentStatus.PUBLISHED.value,
                published_at=now,
                version=expected_version + 1,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            existing = session.scalar(
                select(AssignmentRecord).where(
                    AssignmentRecord.id == assignment_id, AssignmentRecord.teacher_id == teacher_id
                )
            )
            if existing is None:
                raise NotFound("assignment")
            if existing.version != expected_version:
                raise VersionConflict("assignment_version_conflict")
            # Same version but status not editable (already published/closed/...).
            raise InvalidTransition("assignment_not_publishable")
        updated = session.get(AssignmentRecord, assignment_id)
        assert updated is not None
        return _dto_from_session(session, updated)


def close(assignment_id: str, *, teacher_id: str, expected_version: int) -> education.AssignmentDTO:
    """Transition a published assignment to closed (no new submissions)."""
    now = time.time()
    with session_scope() as session:
        result = session.execute(
            update(AssignmentRecord)
            .where(
                AssignmentRecord.id == assignment_id,
                AssignmentRecord.teacher_id == teacher_id,
                AssignmentRecord.version == expected_version,
                AssignmentRecord.status == education.AssignmentStatus.PUBLISHED.value,
            )
            .values(
                status=education.AssignmentStatus.CLOSED.value,
                version=expected_version + 1,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            existing = session.scalar(
                select(AssignmentRecord).where(
                    AssignmentRecord.id == assignment_id, AssignmentRecord.teacher_id == teacher_id
                )
            )
            if existing is None:
                raise NotFound("assignment")
            if existing.version != expected_version:
                raise VersionConflict("assignment_version_conflict")
            raise InvalidTransition("assignment_not_closable")
        updated = session.get(AssignmentRecord, assignment_id)
        assert updated is not None
        return _dto_from_session(session, updated)
