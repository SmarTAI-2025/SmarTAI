"""Owner-scoped course labels for the current teacher workflow."""
from __future__ import annotations

from backend.db import course_repository
from backend.domain import education
from backend.domain.errors import Forbidden, ValidationError


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
    if role != "teacher":
        raise Forbidden("invalid_role")
    return _visible(course_repository.list_courses(actor_id=actor_id))


def get_course(*, course_id: str, actor_id: str, role: str) -> education.CourseDTO:
    if role != "teacher":
        raise Forbidden("invalid_role")
    return course_repository.get_course(course_id=course_id, actor_id=actor_id)


def delete_course(*, course_id: str, teacher_id: str) -> None:
    course_repository.delete_course(course_id=course_id, actor_id=teacher_id)
