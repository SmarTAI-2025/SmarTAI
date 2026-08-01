"""Configurable registration with short access tokens and rotating sessions."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from backend.auth import create_token, get_current_user, hash_password, verify_password
from backend.config import settings
from backend.db.auth_repository import (
    AuthRepositoryError,
    create_refresh_session,
    register_with_invite,
    register_without_invite,
    revoke_refresh_session,
    rotate_refresh_session,
)
from backend.models import User
from backend.state import find_user_by_username

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str

    @field_validator("username", mode="before")
    @classmethod
    def _strip_username(cls, value):
        return value.strip() if isinstance(value, str) else value


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    email: str = ""
    # Invite registration uses the invite's role; open registration accepts only
    # teacher/student and never admin.
    role: str = "teacher"
    invite_code: Optional[str] = None

    @field_validator("username", "email", mode="before")
    @classmethod
    def _strip_identity_fields(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("invite_code", mode="before")
    @classmethod
    def _normalize_invite_code(cls, value):
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        return stripped or None


def _set_refresh_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        settings.refresh_cookie_name,
        raw,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite=settings.refresh_cookie_samesite,
        max_age=settings.refresh_session_days * 86400,
        path="/",
    )


@router.post("/register")
def register(req: RegisterRequest, response: Response):
    if settings.registration_closed and not req.invite_code:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Invitation code required")
    try:
        if req.invite_code:
            user = register_with_invite(
                username=req.username,
                email=req.email,
                role=req.role,
                password_hash=hash_password(req.password),
                invite_code=req.invite_code,
            )
        else:
            user = register_without_invite(
                username=req.username,
                email=req.email,
                password_hash=hash_password(req.password),
                role=req.role,
            )
    except AuthRepositoryError as exc:
        code = status.HTTP_409_CONFLICT if str(exc) in {"Username already exists", "Email already exists"} else status.HTTP_400_BAD_REQUEST
        raise HTTPException(code, detail=str(exc)) from exc
    refresh = create_refresh_session(user.id, settings.refresh_session_days)
    _set_refresh_cookie(response, refresh)
    return {"user_id": user.id, "token": create_token(user.id, user.role), "user": user.public()}


@router.post("/login")
def login(req: LoginRequest, response: Response):
    user = find_user_by_username(req.username)
    if user is None or not user.is_active or not verify_password(req.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    refresh = create_refresh_session(user.id, settings.refresh_session_days)
    _set_refresh_cookie(response, refresh)
    return {"token": create_token(user.id, user.role), "user": user.public()}


@router.post("/refresh")
def refresh(request: Request, response: Response):
    raw = request.cookies.get(settings.refresh_cookie_name)
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Refresh session missing")
    rotated = rotate_refresh_session(raw, settings.refresh_session_days)
    if rotated is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Refresh session expired or revoked")
    new_raw, user = rotated
    _set_refresh_cookie(response, new_raw)
    return {"token": create_token(user.id, user.role), "user": user.public()}


@router.post("/logout")
def logout(request: Request, response: Response, current: User = Depends(get_current_user)):
    revoke_refresh_session(request.cookies.get(settings.refresh_cookie_name))
    response.delete_cookie(settings.refresh_cookie_name, path="/", secure=settings.refresh_cookie_secure,
                           httponly=True, samesite=settings.refresh_cookie_samesite)
    return {"status": "success"}


@router.get("/me")
def me(current: User = Depends(get_current_user)):
    return current.public()
