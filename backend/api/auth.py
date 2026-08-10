"""Configurable registration with short access tokens and rotating sessions."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from backend.auth import (
    create_frontier_demo_token,
    create_token,
    get_current_user,
    get_task_user,
    hash_password,
    verify_password,
)
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
from backend.state import find_user_by_username, register_user

router = APIRouter(prefix="/auth", tags=["auth"])


class _FrontierDemoSessionIssuer:
    """Bound anonymous session issuance for the single-process demo host."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._day = ""
        self._issued = 0
        self._last_issued_at = 0.0

    def reserve(self) -> None:
        now = time.time()
        day = datetime.now(timezone.utc).date().isoformat()
        limit = max(0, int(settings.frontier_demo_daily_session_limit))
        cooldown = max(0.0, float(settings.frontier_demo_session_cooldown_seconds))
        with self._lock:
            if day != self._day:
                self._day = day
                self._issued = 0
                self._last_issued_at = 0.0
            if limit <= 0 or self._issued >= limit:
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={"code": "frontier_demo_daily_limit_reached"},
                )
            if now - self._last_issued_at < cooldown:
                retry_after = max(1, int(cooldown - (now - self._last_issued_at)) + 1)
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={"code": "frontier_demo_session_cooldown"},
                    headers={"Retry-After": str(retry_after)},
                )
            self._issued += 1
            self._last_issued_at = now


_frontier_demo_session_issuer = _FrontierDemoSessionIssuer()


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


@router.post("/frontier-demo-session")
def create_frontier_demo_session(response: Response):
    """Issue a passwordless short task capability for synthetic demo data."""
    if not settings.frontier_demo_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "frontier_demo_disabled"},
        )
    if not settings.shared_pool_enabled or not settings.gemini_api_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "frontier_demo_provider_unavailable"},
        )
    _frontier_demo_session_issuer.reserve()

    identity = uuid.uuid4().hex
    user = User(
        id=f"frontier_{identity}",
        username=f"frontier-{identity}",
        email="",
        role="teacher",
        password_hash="",
    )
    register_user(user)
    lifetime_minutes = max(1, min(int(settings.frontier_demo_session_minutes), 60))
    response.headers["Cache-Control"] = "no-store"
    return {
        "token": create_frontier_demo_token(
            user.id,
            expires_in_minutes=lifetime_minutes,
        ),
        "token_type": "bearer",
        "expires_in": lifetime_minutes * 60,
        "user": user.public(),
        "synthetic_data_only": True,
    }


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
def me(current: User = Depends(get_task_user)):
    return current.public()
