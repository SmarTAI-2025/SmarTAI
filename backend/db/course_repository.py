"""Transactional course and enrollment access.

The course aggregate owns its rows: every read/write carries the actor id and
the authorization predicate (teacher ownership or student enrollment) is part
of the SQL statement, not a post-load Python check. A non-owner therefore reads
zero rows, which the service layer surfaces as ``NotFound`` — never a payload
leak with a 403-shaped body that reveals the resource exists.

Cross-aggregate orchestration (e.g. "publish assignment after enrolling") stays
in services; this module owns only the course/enrollment tables.
"""
from __future__ import annotations

import time
import uuid

from sqlalchemy import delete, select

from backend.db.models import CourseEnrollmentRecord, CourseRecord, UserRecord
from backend.db.session import session_scope
from backend.domain import education
from backend.domain.errors import NotFound, ValidationError


def _new_course_id() -> str:
    return f"course_{uuid.uuid4().hex[:12]}"


def _to_dto(record: CourseRecord, student_ids: list[str]) -> education.CourseDTO:
    return education.CourseDTO(
        id=record.id,
        name=record.name,
        code=record.code or "",
        description=record.description or "",
        teacher_id=record.teacher_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        student_ids=student_ids,
    )


def create_course(*, teacher_id: str, name: str, code: str = "", description: str = "") -> education.CourseDTO:
    course_id = _new_course_id()
    now = time.time()
    with session_scope() as session:
        session.add(
            CourseRecord(
                id=course_id,
                name=name,
                code=code,
                description=description,
                teacher_id=teacher_id,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        record = session.get(CourseRecord, course_id)
        assert record is not None
        return _to_dto(record, [])


def get_course(course_id: str, *, actor_id: str) -> education.CourseDTO:
    with session_scope() as session:
        # Owner predicate in SQL: a non-owner selects nothing.
        record = session.scalar(
            select(CourseRecord).where(
                CourseRecord.id == course_id, CourseRecord.teacher_id == actor_id
            )
        )
        if record is None:
            raise NotFound("course")
        student_ids = _load_student_ids(session, course_id)
        return _to_dto(record, student_ids)


def list_courses(*, actor_id: str) -> list[education.CourseDTO]:
    with session_scope() as session:
        records = session.scalars(
            select(CourseRecord).where(CourseRecord.teacher_id == actor_id).order_by(CourseRecord.created_at)
        ).all()
        return [_to_dto(r, _load_student_ids(session, r.id)) for r in records]


def list_all_courses() -> list[education.CourseDTO]:
    """Admin unscoped read of every course with its enrolled students."""
    with session_scope() as session:
        records = session.scalars(
            select(CourseRecord).order_by(CourseRecord.created_at)
        ).all()
        return [_to_dto(r, _load_student_ids(session, r.id)) for r in records]


def get_course_unscoped(course_id: str) -> education.CourseDTO:
    """Admin unscoped read of a single course."""
    with session_scope() as session:
        record = session.get(CourseRecord, course_id)
        if record is None:
            raise NotFound("course")
        return _to_dto(record, _load_student_ids(session, course_id))


def delete_course(course_id: str, *, actor_id: str) -> None:
    with session_scope() as session:
        # Owner predicate gates the DELETE so another teacher cannot drop it.
        result = session.execute(
            delete(CourseRecord).where(
                CourseRecord.id == course_id, CourseRecord.teacher_id == actor_id
            )
        )
        if result.rowcount == 0:
            raise NotFound("course")
        # Enrollments cascade via ON DELETE; explicitness here would be redundant
        # but harmless — the FK rule is the source of truth.


def _load_student_ids(session, course_id: str) -> list[str]:
    return list(
        session.scalars(
            select(CourseEnrollmentRecord.student_id)
            .where(CourseEnrollmentRecord.course_id == course_id)
            .order_by(CourseEnrollmentRecord.enrolled_at)
        )
    )


def enroll(course_id: str, *, student_id: str, actor_id: str | None = None) -> education.EnrollmentDTO:
    """Enroll a student. ``actor_id`` (course owner) gates the write when given.

    Only persisted student-role users may be enrolled; a teacher id is rejected
    as a validation error so membership scoping never silently admits a teacher.
    """
    now = time.time()
    with session_scope() as session:
        course = session.scalar(
            select(CourseRecord).where(CourseRecord.id == course_id)
        )
        if course is None:
            raise NotFound("course")
        if actor_id is not None and course.teacher_id != actor_id:
            raise NotFound("course")
        student = session.get(UserRecord, student_id)
        if student is None:
            raise NotFound("student")
        if student.role != "student":
            raise ValidationError("Only student-role users may be enrolled")
        existing = session.scalar(
            select(CourseEnrollmentRecord).where(
                CourseEnrollmentRecord.course_id == course_id,
                CourseEnrollmentRecord.student_id == student_id,
            )
        )
        if existing is None:
            session.add(
                CourseEnrollmentRecord(
                    course_id=course_id, student_id=student_id, enrolled_at=now
                )
            )
        return education.EnrollmentDTO(
            course_id=course_id, student_id=student_id, enrolled_at=existing.enrolled_at if existing else now
        )


def is_enrolled(course_id: str, *, student_id: str) -> bool:
    with session_scope() as session:
        return session.scalar(
            select(CourseEnrollmentRecord)
            .where(
                CourseEnrollmentRecord.course_id == course_id,
                CourseEnrollmentRecord.student_id == student_id,
            )
        ) is not None


def list_student_courses(student_id: str) -> list[education.CourseDTO]:
    """Courses a student is enrolled in (authorization source for student reads)."""
    with session_scope() as session:
        records = session.scalars(
            select(CourseRecord)
            .join(CourseEnrollmentRecord, CourseEnrollmentRecord.course_id == CourseRecord.id)
            .where(CourseEnrollmentRecord.student_id == student_id)
            .order_by(CourseRecord.created_at)
        ).all()
        return [_to_dto(r, [student_id]) for r in records]
