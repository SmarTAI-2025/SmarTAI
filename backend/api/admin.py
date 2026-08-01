"""Admin API router — user management and invitations.

Admin-only endpoints for listing users, activating/deactivating accounts,
creating teacher/student invites, and listing outstanding invites. All routes
are gated by ``require_admin``; the role guard is the single authorization
check, so every handler can assume the caller is an admin.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from backend.auth import require_admin
from backend.db.auth_repository import create_invite
from backend.db.models import CourseRecord, InviteCodeRecord, UserRecord
from backend.db.session import session_scope
from backend.models import User

router = APIRouter(prefix="/admin", tags=["admin"])


class InviteRequest(BaseModel):
    email: str = ""
    role: str = "student"
    course_id: Optional[str] = None
    expires_in_hours: int = 168  # 7 days


class ActivateRequest(BaseModel):
    is_active: bool


def _user_public(record: UserRecord) -> dict:
    return {
        "id": record.id,
        "username": record.username,
        "email": record.email or "",
        "role": record.role,
        "is_active": record.is_active,
        "created_at": record.created_at,
    }


@router.get("/users")
def admin_list_users(role: Optional[str] = None, is_active: Optional[bool] = None,
                     current: User = Depends(require_admin)):
    """List all users with optional role / active-status filters."""
    with session_scope() as session:
        stmt = select(UserRecord).order_by(UserRecord.created_at)
        if role:
            stmt = stmt.where(UserRecord.role == role)
        if is_active is not None:
            stmt = stmt.where(UserRecord.is_active == is_active)
        records = session.scalars(stmt).all()
        return [_user_public(r) for r in records]


@router.patch("/users/{user_id}/active")
def admin_set_active(user_id: str, req: ActivateRequest,
                     current: User = Depends(require_admin)):
    """Activate or deactivate a user. Deactivation is preferred over deletion
    when the user owns courses or has submissions (FK references remain valid
    but the account can no longer authenticate)."""
    with session_scope() as session:
        record = session.get(UserRecord, user_id)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found")
        # Refuse to deactivate a teacher who still owns a course; otherwise the
        # course would have no active owner. The admin must reassign ownership
        # or delete the course first.
        if not req.is_active and record.role == "teacher":
            owns = session.scalar(select(CourseRecord).where(CourseRecord.teacher_id == user_id))
            if owns is not None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail="Cannot deactivate a teacher who still owns a course",
                )
        record.is_active = req.is_active
        return _user_public(record)


@router.post("/invites")
def admin_create_invite(req: InviteRequest, current: User = Depends(require_admin)):
    if req.role not in ("teacher", "student", "admin"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Role must be teacher, student, or admin")
    # A student invite may pre-bind a course so registration auto-enrolls; the
    # course must exist and belong to a teacher.
    if req.course_id is not None and req.role == "student":
        with session_scope() as session:
            course = session.get(CourseRecord, req.course_id)
            if course is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Course not found")
    record = create_invite(invited_by=current.id, email=req.email, role=req.role,
                           course_id=req.course_id, expires_in_hours=req.expires_in_hours)
    return {"invite_code": record.code, "role": record.role, "expires_at": record.expires_at}


@router.get("/invites")
def admin_list_invites(current: User = Depends(require_admin)):
    with session_scope() as session:
        records = session.scalars(
            select(InviteCodeRecord).order_by(InviteCodeRecord.created_at.desc())
        ).all()
        return [
            {
                "code": r.code,
                "email": r.email,
                "role": r.role,
                "course_id": r.course_id,
                "created_at": r.created_at,
                "expires_at": r.expires_at,
                "used_at": r.used_at,
                "used_by": r.used_by,
            }
            for r in records
        ]
