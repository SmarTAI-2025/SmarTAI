"""Users API router — list / patch / delete / invite."""
from __future__ import annotations

import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from backend.auth import get_current_user, require_admin, require_teacher
from backend.models import User
from backend.state import (
    get_invite_store,
    get_user_store,
    remove_user,
)

router = APIRouter(prefix="/users", tags=["users"])


class PatchUserRequest(BaseModel):
    username: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None


class InviteRequest(BaseModel):
    email: str = ""
    role: str = "student"
    course_id: Optional[str] = None
    expires_in_hours: int = 168  # 7 days


@router.get("/")
def list_users(current: User = Depends(require_teacher)):
    """List users. Admin sees all; teacher sees students in their courses."""
    store = get_user_store()
    if current.role == "admin":
        return [u.public() for u in store.values()]
    # Teacher: only students enrolled in courses they teach
    visible = []
    for u in store.values():
        if u.id == current.id:
            visible.append(u.public())
            continue
        if u.role == "student" and any(cid in current.course_ids for cid in u.course_ids):
            visible.append(u.public())
    return visible


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
        user.role = req.role  # type: ignore
    return user.public()


@router.delete("/{user_id}")
def delete_user(user_id: str, current: User = Depends(require_admin)):
    if not remove_user(user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found")
    return {"status": "success"}


@router.post("/invite")
def invite(req: InviteRequest, current: User = Depends(require_teacher)):
    if req.role not in ("teacher", "student"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Role must be teacher or student")
    code = uuid.uuid4().hex[:10].upper()
    expires = time.time() + req.expires_in_hours * 3600
    get_invite_store()[code] = {
        "role": req.role,
        "course_id": req.course_id,
        "email": req.email,
        "expires_at": expires,
        "invited_by": current.id,
    }
    return {"invite_code": code, "expires_at": expires}
