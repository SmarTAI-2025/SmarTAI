"""Courses API router — CRUD + enroll + list students."""
from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from backend.auth import get_current_user, require_teacher
from backend.models import Course, User
from backend.state import (
    get_course_store,
    get_user_store,
)

router = APIRouter(prefix="/courses", tags=["courses"])


class CreateCourseRequest(BaseModel):
    name: str
    code: str = ""
    description: str = ""


class EnrollRequest(BaseModel):
    student_ids: List[str] = []
    invite_code: Optional[str] = None


@router.post("/")
def create_course(req: CreateCourseRequest, current: User = Depends(require_teacher)):
    course = Course(
        id=f"c_{uuid.uuid4().hex[:10]}",
        name=req.name,
        code=req.code,
        description=req.description,
        teacher_id=current.id,
    )
    get_course_store()[course.id] = course
    current.course_ids = list(set([*current.course_ids, course.id]))
    return _serialize(course)


@router.get("/")
def list_courses(current: User = Depends(get_current_user)):
    store = get_course_store()
    if current.role == "admin":
        items = list(store.values())
    elif current.role == "teacher":
        items = [c for c in store.values() if c.teacher_id == current.id]
    else:
        items = [c for c in store.values() if current.id in c.student_ids]
    return [_serialize(c) for c in items]


@router.get("/{course_id}")
def get_course(course_id: str, current: User = Depends(get_current_user)):
    course = get_course_store().get(course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Course not found")
    _check_course_access(course, current)
    return _serialize(course)


@router.get("/{course_id}/students")
def get_course_students(course_id: str, current: User = Depends(require_teacher)):
    course = get_course_store().get(course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Course not found")
    _check_course_access(course, current)
    user_store = get_user_store()
    students = [user_store[sid].public() for sid in course.student_ids if sid in user_store]
    return students


@router.post("/{course_id}/enroll")
def enroll_students(course_id: str, req: EnrollRequest, current: User = Depends(require_teacher)):
    course = get_course_store().get(course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Course not found")
    _check_course_access(course, current)
    user_store = get_user_store()
    added: list[str] = []
    for sid in req.student_ids:
        if sid not in user_store:
            continue
        if sid not in course.student_ids:
            course.student_ids.append(sid)
            user = user_store[sid]
            user.course_ids = list(set([*user.course_ids, course_id]))
            added.append(sid)
    return {"status": "success", "added": added, "total": len(course.student_ids)}


@router.delete("/{course_id}")
def delete_course(course_id: str, current: User = Depends(require_teacher)):
    store = get_course_store()
    course = store.get(course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Course not found")
    _check_course_access(course, current)
    store.pop(course_id, None)
    # Detach from users
    user_store = get_user_store()
    for sid in [course.teacher_id, *course.student_ids]:
        u = user_store.get(sid)
        if u and course_id in u.course_ids:
            u.course_ids = [c for c in u.course_ids if c != course_id]
    return {"status": "success"}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _check_course_access(course: Course, user: User) -> None:
    if user.role == "admin":
        return
    if user.role == "teacher" and course.teacher_id == user.id:
        return
    if user.role == "student" and user.id in course.student_ids:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Not enrolled in this course")


def _serialize(c: Course) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "code": c.code,
        "description": c.description,
        "teacher_id": c.teacher_id,
        "student_count": len(c.student_ids),
        "created_at": c.created_at,
    }
