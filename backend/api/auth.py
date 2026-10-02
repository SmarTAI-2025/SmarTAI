"""Configurable registration with short access tokens and rotating sessions."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.auth import get_current_user, hash_password, verify_password
from backend.config import settings
from backend.db.auth_repository import (
    authenticate_and_create_session,
    revoke_refresh_session,
    rotate_refresh_session,
)
from backend.models import User
from backend.db.auth_repository import user_auth_lock, revoke_all_refresh_sessions
from backend.db.models import UserRecord, PasswordResetRequestRecord, AdminAuditLogRecord
from backend.db.session import session_scope
from sqlalchemy import select
import time
import uuid
from backend.services.email_registration import (
    RegistrationError,
    request_registration,
    resend_registration,
    verify_registration,
)
from backend.services.email_sender import get_email_sender
from backend.services.password_reset import (
    PasswordResetError,
    confirm_password_reset,
    request_password_reset,
)


class _SanitizedValidationRoute(APIRoute):
    """Keep Pydantic's rejected password/token values out of 422 responses."""

    def get_route_handler(self):
        original_handler = super().get_route_handler()

        async def sanitized_handler(request: Request):
            try:
                return await original_handler(request)
            except RequestValidationError as exc:
                errors = [
                    {
                        key: value
                        for key, value in error.items()
                        if key not in {"input", "ctx"}
                    }
                    for error in exc.errors()
                ]
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={"detail": jsonable_encoder(errors)},
                )

        return sanitized_handler


router = APIRouter(
    prefix="/auth",
    tags=["auth"],
    route_class=_SanitizedValidationRoute,
)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("username", mode="before")
    @classmethod
    def _strip_username(cls, value):
        return value.strip() if isinstance(value, str) else value


class EmailRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("username", "email", mode="before")
    @classmethod
    def _strip_fields(cls, value):
        return value.strip() if isinstance(value, str) else value


class EmailRegistrationResendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=64)


class EmailRegistrationVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=512)


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)


class PasswordResetConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=8, max_length=128)


def _cookie_name(request: Request) -> str:
    return "smartai_admin_refresh" if getattr(request.app.state, "private_admin", False) else settings.refresh_cookie_name


def _set_refresh_cookie(response: Response, raw: str, request: Request) -> None:
    response.set_cookie(
        _cookie_name(request),
        raw,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite=settings.refresh_cookie_samesite,
        max_age=settings.refresh_session_days * 86400,
        path="/",
    )


def _delete_refresh_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        _cookie_name(request),
        path="/",
        secure=settings.refresh_cookie_secure,
        httponly=True,
        samesite=settings.refresh_cookie_samesite,
    )


def _registration_error(exc: RegistrationError) -> HTTPException:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return HTTPException(exc.status_code, detail={"code": exc.code}, headers=headers)


@router.post("/register/request", status_code=status.HTTP_202_ACCEPTED)
def request_email_registration(req: EmailRegistrationRequest, request: Request):
    try:
        return request_registration(
            username=req.username,
            email=req.email,
            password=req.password,
            source_ip=request.client.host if request.client else None,
            sender=get_email_sender(),
        )
    except RegistrationError as exc:
        raise _registration_error(exc) from exc


@router.post("/register/resend", status_code=status.HTTP_202_ACCEPTED)
def resend_email_registration(req: EmailRegistrationResendRequest, request: Request):
    try:
        return resend_registration(
            request_id=req.request_id,
            source_ip=request.client.host if request.client else None,
            sender=get_email_sender(),
        )
    except RegistrationError as exc:
        raise _registration_error(exc) from exc


@router.post("/register/verify")
def verify_email_registration(req: EmailRegistrationVerifyRequest, request: Request):
    try:
        return verify_registration(req.token, request.client.host if request.client else None)
    except RegistrationError as exc:
        raise _registration_error(exc) from exc


def _password_reset_error(exc: PasswordResetError) -> HTTPException:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return HTTPException(exc.status_code, detail={"code": exc.code}, headers=headers)


@router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED)
def request_password_reset_email(
    req: PasswordResetRequest, request: Request, background_tasks: BackgroundTasks,
):
    try:
        return request_password_reset(
            email=req.email,
            source_ip=request.client.host if request.client else None,
            sender=get_email_sender(),
            delivery_scheduler=background_tasks.add_task,
        )
    except PasswordResetError as exc:
        raise _password_reset_error(exc) from exc


@router.post("/password-reset/confirm")
def confirm_password_reset_email(req: PasswordResetConfirmRequest, response: Response, request: Request):
    try:
        result = confirm_password_reset(req.token, req.new_password)
    except PasswordResetError as exc:
        raise _password_reset_error(exc) from exc
    _delete_refresh_cookie(response, request)
    return result


@router.post("/login")
def login(req: LoginRequest, response: Response, request: Request):
    authenticated = authenticate_and_create_session(
        req.username,
        req.password,
        settings.refresh_session_days,
    )
    if authenticated is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    refresh, user, access = authenticated
    if getattr(request.app.state, "private_admin", False) and (user.role != "admin" or user.is_read_only):
        revoke_refresh_session(refresh)
        raise HTTPException(403, detail="Admin access required")
    _set_refresh_cookie(response, refresh, request)
    return {"token": access, "user": user.public()}


@router.post("/refresh")
def refresh(request: Request, response: Response):
    raw = request.cookies.get(_cookie_name(request))
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Refresh session missing")
    rotated = rotate_refresh_session(raw, settings.refresh_session_days)
    if rotated is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Refresh session expired or revoked")
    new_raw, user, access = rotated
    if getattr(request.app.state, "private_admin", False) and (user.role != "admin" or user.is_read_only):
        revoke_refresh_session(new_raw)
        raise HTTPException(403, detail="Admin access required")
    _set_refresh_cookie(response, new_raw, request)
    return {"token": access, "user": user.public()}


@router.post("/logout")
def logout(request: Request, response: Response, current: User = Depends(get_current_user)):
    revoke_refresh_session(request.cookies.get(_cookie_name(request)))
    _delete_refresh_cookie(response, request)
    return {"status": "success"}


@router.get("/me")
def me(current: User = Depends(get_current_user)):
    return current.public()


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


@router.post("/password-change")
def change_password(req: PasswordChangeRequest, response: Response, request: Request, current: User = Depends(get_current_user)):
    with user_auth_lock(current.id):
        with session_scope() as session:
            user = session.scalar(select(UserRecord).where(UserRecord.id == current.id).with_for_update())
            if (user is None or not user.is_active or user.auth_version != current.auth_version
                    or user.auth_invalid_before != current.auth_invalid_before):
                raise HTTPException(401, detail="Sign in again")
            if not verify_password(req.current_password, user.password_hash):
                raise HTTPException(400, detail={"code": "current_password_incorrect"})
            if req.current_password == req.new_password:
                raise HTTPException(400, detail={"code": "new_password_unchanged"})
            now = time.time()
            user.password_hash = hash_password(req.new_password)
            user.auth_version += 1
            user.auth_invalid_before = now
            user.updated_at = now
            revoke_all_refresh_sessions(session, user.id, now=now)
            for row in session.scalars(select(PasswordResetRequestRecord).where(
                PasswordResetRequestRecord.user_id == user.id,
                PasswordResetRequestRecord.consumed_at.is_(None),
                PasswordResetRequestRecord.superseded_at.is_(None),
            ).with_for_update()):
                row.superseded_at = now
            if user.role == "admin":
                action_id = uuid.uuid4().hex
                session.add(AdminAuditLogRecord(id=action_id, actor_id=user.id,
                    target_user_id=user.id, action="own_password_changed", reason="account_security",
                    note="", idempotency_key=action_id, request_hash="credential_not_recorded",
                    result="success", created_at=now))
    _delete_refresh_cookie(response, request)
    return {"status": "password_changed", "sign_in_required": True}
