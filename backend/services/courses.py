"""Course and enrollment use cases.

Thin orchestration over course_repository: authorization for student-visible
course reads is resolved here by asking the enrollment table (via
``list_student_courses``) rather than any user-side mirror. Services raise
DomainError subtypes; the API layer maps them to HTTP.
"""
from __future__ import annotations

from backend.db import course_repository
from backend.domain import education
from backend.domain.errors import Forbidden, NotFound, ValidationError


# Assignments created without an explicit catalog course are attached to an
# internal owner-scoped course so normalized FKs remain intact.  It is plumbing,
# not a teacher-visible Course Library item.
_SYSTEM_COURSE_CODE = "__SMARTAI_UNASSIGNED__"


def _visible(courses: list[education.CourseDTO]) -> list[education.CourseDTO]:
    return [course for course in courses if course.code != _SYSTEM_COURSE_CODE]


def create_course(*, teacher_id: str, name: str, code: str = "", description: str = "") -> education.CourseDTO:
    if not name.strip():
        raise ValidationError("Course name is required")
    return course_repository.create_course(
        teacher_id=teacher_id, name=name.strip(), code=code, description=description
    )


def list_courses_for(actor_id: str, role: str) -> list[education.CourseDTO]:
    """Role-scoped course list. Admin reads all (unscoped); a teacher reads
    courses they own; a student reads courses they are enrolled in."""
    if role == "admin":
        # Admin unscoped read: iterate all teachers' courses via a direct query.
        return _visible(course_repository.list_all_courses())
    if role == "teacher":
        return _visible(course_repository.list_courses(actor_id=actor_id))
    if role == "student":
        return _visible(course_repository.list_student_courses(student_id=actor_id))
    raise Forbidden("invalid_role")


def get_course(*, course_id: str, actor_id: str, role: str) -> education.CourseDTO:
    if role == "admin":
        return course_repository.get_course_unscoped(course_id)
    if role == "teacher":
        return course_repository.get_course(course_id=course_id, actor_id=actor_id)
    if role == "student":
        # Students may read only courses they are enrolled in.
        enrolled = course_repository.list_student_courses(student_id=actor_id)
        for c in enrolled:
            if c.id == course_id:
                return c
        raise NotFound("course")
    raise Forbidden("invalid_role")


def enroll_students(*, course_id: str, teacher_id: str, student_ids: list[str]) -> education.CourseDTO:
    for sid in student_ids:
        course_repository.enroll(course_id=course_id, student_id=sid, actor_id=teacher_id)
    return course_repository.get_course(course_id=course_id, actor_id=teacher_id)


def delete_course(*, course_id: str, teacher_id: str) -> None:
    course_repository.delete_course(course_id=course_id, actor_id=teacher_id)
