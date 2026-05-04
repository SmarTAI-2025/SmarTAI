"""Auth API router — register / login / refresh / logout / me."""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.auth import (
    create_token,
    get_current_user,
    hash_password,
    verify_password,
)
from backend.config import settings
from backend.models import User
from backend.state import (
    find_user_by_username,
    get_invite_store,
    register_user,
)

router = APIRouter(prefix="/auth", tags=["auth"])


# ─── Request models ──────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    email: str = ""
    role: str = "teacher"  # teacher | student
    invite_code: Optional[str] = None


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.post("/register")
def register(req: RegisterRequest):
    # Public registration is closed; only an invite code can create a new
    # account. The frontend keeps the page as a facade so the link doesn't
    # 404, but we surface a clear "closed" message here.
    if settings.registration_closed and not req.invite_code:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="注册暂未开放。如需测试请联系管理员获取受邀账号。",
        )

    if find_user_by_username(req.username) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Username already exists")

    role = req.role
    course_ids: list[str] = []

    # Validate invite code if provided
    if req.invite_code:
        invites = get_invite_store()
        invite = invites.get(req.invite_code)
        if invite is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid invite code")
        role = invite.get("role", role)
        course_id = invite.get("course_id")
        if course_id:
            course_ids = [course_id]
        invites.pop(req.invite_code, None)  # one-time use

    if role not in ("teacher", "student"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Role must be teacher or student")

    user = User(
        id=f"u_{uuid.uuid4().hex[:10]}",
        username=req.username,
        email=req.email,
        role=role,  # type: ignore
        password_hash=hash_password(req.password),
        course_ids=course_ids,
    )
    register_user(user)

    token = create_token(user.id, user.role)
    return {
        "user_id": user.id,
        "token": token,
        "user": user.public(),
    }


@router.post("/login")
def login(req: LoginRequest):
    user = find_user_by_username(req.username)
    if user is None or not verify_password(req.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    token = create_token(user.id, user.role)
    return {"token": token, "user": user.public()}


@router.post("/refresh")
def refresh(current: User = Depends(get_current_user)):
    token = create_token(current.id, current.role)
    return {"token": token}


@router.post("/logout")
def logout(current: User = Depends(get_current_user)):
    # Stateless JWT — just instruct client to drop the token.
    # If you later want server-side revocation, add a JTI blacklist here.
    return {"status": "success"}


@router.get("/me")
def me(current: User = Depends(get_current_user)):
    return current.public()
