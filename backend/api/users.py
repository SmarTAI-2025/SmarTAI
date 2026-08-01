"""Users API router — list / patch / delete / invite.

Membership is read from ``course_enrollments``; the legacy ``course_ids``
mirror is gone, so a teacher's visible students are resolved by joining the
enrollment table for courses they own.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.auth import get_current_user, require_admin, require_teacher
from backend.db.auth_repository import create_invite
from backend.db.models import CourseEnrollmentRecord, CourseRecord, UserRecord
from backend.db.session import session_scope
from backend.models import User
from backend.state import get_user_store, remove_user

router = APIRouter(prefix="/users", tags=["users"])


class PatchUserRequest(BaseModel):
    username: Optional[str] = Field(default=None, min_length=3, max_length=64)
    email: Optional[str] = Field(default=None, max_length=254)
    role: Optional[str] = None


class InviteRequest(BaseModel):
    email: str = Field(default="", max_length=254)
    role: str = "student"
    course_id: Optional[str] = None
    expires_in_hours: int = Field(default=168, ge=1, le=720)  # 1 hour–30 days


def _visible_student_ids(teacher_id: str) -> set[str]:
    """Student ids enrolled in any course the teacher owns (authorization join)."""
    with session_scope() as session:
        rows = session.execute(
            select(CourseEnrollmentRecord.student_id)
            .join(CourseRecord, CourseRecord.id == CourseEnrollmentRecord.course_id)
            .where(CourseRecord.teacher_id == teacher_id)
        ).all()
        return {r[0] for r in rows}


@router.get("/")
def list_users(current: User = Depends(require_teacher)):
    """List users. Admin sees all; a teacher sees self plus students enrolled in
    courses they teach (resolved via course_enrollments, not course_ids)."""
    store = get_user_store()
    if current.role == "admin":
        return [u.public() for u in store.values()]
    visible_student_ids = _visible_student_ids(current.id)
    out = []
    for u in store.values():
        if u.id == current.id:
            out.append(u.public())
        elif u.role == "student" and u.id in visible_student_ids:
            out.append(u.public())
    return out


@router.patch("/{user_id}")
def patch_user(user_id: str, req: PatchUserRequest, current: User = Depends(get_current_user)):
    if current.id != user_id and current.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cannot edit other users")
    store = get_user_store()
    user = store.get(user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found")
    if req.username and req.username != user.username:
        # Disallow username changes for now (would require re-indexing)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Username cannot be changed")
    if req.email is not None:
        user.email = req.email
    if req.role is not None and current.role == "admin":
        # Admin may reassign roles, but never to a role that would orphan a
        # course they own (a course needs a teacher owner).
        if req.role != "teacher":
            with session_scope() as session:
                owns = session.scalar(
                    select(CourseRecord).where(CourseRecord.teacher_id == user_id)
                )
                if owns is not None:
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        detail="Cannot demote a teacher who still owns a course",
                    )
        user.role = req.role  # type: ignore
    store[user_id] = user
    return user.public()


@router.delete("/{user_id}")
def delete_user(user_id: str, current: User = Depends(require_admin)):
    # Prefer deactivation over hard delete when the user is referenced (FK
    # constraints on courses/enrollments/submissions would otherwise block).
    with session_scope() as session:
        record = session.get(UserRecord, user_id)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found")
        record.is_active = False
    return {"status": "success", "is_active": False}


@router.post("/invite")
def invite(req: InviteRequest, current: User = Depends(require_admin)):
    if req.role not in ("teacher", "student"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Role must be teacher or student")
    record = create_invite(invited_by=current.id, email=req.email, role=req.role,
                           course_id=req.course_id, expires_in_hours=req.expires_in_hours)
    return {"invite_code": record.code, "expires_at": record.expires_at}
