"""Private administrator APIs.

The first administrator slice intentionally stays small: account search and
status management, session revocation, password-reset assistance, a read-only
overview, and an audit feed. Sensitive values are never returned or written to
the audit table.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select

from backend.auth import require_admin
from backend.config import settings
from backend.db.auth_repository import revoke_all_refresh_sessions
from backend.db.models import (
    AdminAuditLogRecord,
    AssignmentRecord,
    CourseRecord,
    GradingRunRecord,
    UserRecord,
)
from backend.db.session import session_scope
from backend.analytics.admin_usage import query_usage_metrics
from backend.models import User
from backend.services.admin_transactions import administrator_transaction
from backend.services.email_sender import get_email_sender
from backend.services.password_reset import PasswordResetError, request_password_reset

router = APIRouter(prefix="/admin", tags=["admin"])


class ActionRequest(BaseModel):
    reason: str = Field(default="manual_review", min_length=1, max_length=80)
    note: str = Field(default="", max_length=1000)

    @field_validator("reason", "note", mode="before")
    @classmethod
    def _strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


def _user_public(record: UserRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "username": record.username,
        "email": record.email or "",
        "role": record.role,
        "is_active": record.is_active,
        "is_read_only": record.is_read_only,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _request_hash(action: str, target_user_id: str | None, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"action": action, "target_user_id": target_user_id, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _idempotency_key(value: str | None) -> str:
    key = (value or "").strip()
    if not key:
        return f"legacy-{uuid.uuid4().hex}"
    if len(key) > 128:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key is too long")
    return key


def _replay_or_conflict(session, *, actor_id: str, idempotency_key: str, request_hash: str) -> dict[str, Any] | None:
    previous = session.scalar(
        select(AdminAuditLogRecord).where(
            AdminAuditLogRecord.actor_id == actor_id,
            AdminAuditLogRecord.idempotency_key == idempotency_key,
        )
    )
    if previous is None:
        return None
    if previous.request_hash != request_hash:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Idempotency-Key was already used for a different operation")
    return previous.response_body or {"status": previous.result}


def _audit(
    session,
    *,
    actor_id: str,
    target_user_id: str | None,
    action: str,
    reason: str,
    note: str,
    idempotency_key: str,
    request_hash: str,
    before_state: dict[str, Any] | None,
    after_state: dict[str, Any] | None,
    response_body: dict[str, Any],
) -> None:
    session.add(
        AdminAuditLogRecord(
            id=uuid.uuid4().hex,
            actor_id=actor_id,
            target_user_id=target_user_id,
            action=action,
            reason=reason,
            note=note,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            before_state=before_state,
            after_state=after_state,
            response_body=response_body,
            result="success",
            created_at=time.time(),
        )
    )


@router.get("/users")
def admin_list_users(
    role: Optional[str] = None,
    is_active: Optional[bool] = None,
    search: Optional[str] = Query(default=None, max_length=128),
    page: Optional[int] = Query(default=None, ge=1),
    page_size: Optional[int] = Query(default=None, ge=1, le=100),
    current: User = Depends(require_admin),
):
    """List users; pagination metadata is returned when page/page_size are set."""
    with session_scope() as session:
        stmt = select(UserRecord).order_by(UserRecord.created_at, UserRecord.id)
        count_stmt = select(func.count()).select_from(UserRecord)
        conditions = []
        if role:
            if role not in ("teacher", "student", "admin"):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid role")
            conditions.append(UserRecord.role == role)
        if is_active is not None:
            conditions.append(UserRecord.is_active == is_active)
        if search and search.strip():
            term = f"%{search.strip().casefold()}%"
            conditions.append(or_(func.lower(UserRecord.username).like(term), func.lower(func.coalesce(UserRecord.email, "")).like(term)))
        if conditions:
            stmt = stmt.where(*conditions)
            count_stmt = count_stmt.where(*conditions)
        total = int(session.scalar(count_stmt) or 0)
        if page is not None or page_size is not None:
            effective_page = page or 1
            effective_size = page_size or 25
            records = session.scalars(stmt.offset((effective_page - 1) * effective_size).limit(effective_size)).all()
            return {
                "items": [_user_public(record) for record in records],
                "page": effective_page,
                "page_size": effective_size,
                "total": total,
                "has_next": effective_page * effective_size < total,
            }
        return [_user_public(record) for record in session.scalars(stmt).all()]


@router.post("/users/{user_id}/sessions/revoke")
def admin_revoke_sessions(
    user_id: str,
    req: ActionRequest,
    current: User = Depends(require_admin),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _idempotency_key(idempotency_key)
    request_hash = _request_hash("revoke_sessions", user_id, req.model_dump())
    with administrator_transaction(current) as session:
        replay = _replay_or_conflict(session, actor_id=current.id, idempotency_key=key, request_hash=request_hash)
        if replay is not None:
            return replay
        record = session.scalar(select(UserRecord).where(UserRecord.id == user_id).with_for_update())
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found")
        now = time.time()
        record.auth_invalid_before = max(record.auth_invalid_before or 0.0, now)
        record.auth_version += 1
        record.updated_at = now
        revoke_all_refresh_sessions(session, user_id, now=now)
        response_body = {"status": "sessions_revoked", "user_id": user_id}
        _audit(session, actor_id=current.id, target_user_id=user_id, action="sessions_revoked", reason=req.reason, note=req.note, idempotency_key=key, request_hash=request_hash, before_state=None, after_state={"auth_invalid_before": now, "auth_version": record.auth_version}, response_body=response_body)
        return response_body


@router.post("/users/{user_id}/password-reset", status_code=status.HTTP_202_ACCEPTED)
def admin_password_reset_assistance(
    user_id: str,
    req: ActionRequest,
    request: Request,
    current: User = Depends(require_admin),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Ask the existing one-time reset flow to email the account address."""
    key = _idempotency_key(idempotency_key)
    request_hash = _request_hash("password_reset_assistance", user_id, req.model_dump())
    with administrator_transaction(current) as session:
        replay = _replay_or_conflict(session, actor_id=current.id, idempotency_key=key, request_hash=request_hash)
        if replay is not None:
            return replay
        record = session.scalar(select(UserRecord).where(UserRecord.id == user_id).with_for_update())
        if record is None:
            raise HTTPException(404, detail="User not found")
        if not record.email or not record.is_active:
            raise HTTPException(409, detail="An active account with an email is required")
        email = record.email
        # Reserve BEFORE network I/O. Retries/crashes never automatically send
        # a second email; a new explicit request remains subject to cooldown.
        pending = {"status": "reset_request_recorded", "user_id": user_id, "delivery": "unconfirmed"}
        _audit(session, actor_id=current.id, target_user_id=user_id,
            action="password_reset_assistance_requested", reason=req.reason, note=req.note,
            idempotency_key=key, request_hash=request_hash, before_state=None, after_state=None, response_body=pending)
    try:
        result = request_password_reset(email=email, source_ip=request.client.host if request.client else None, sender=get_email_sender())
        response_body = {"status": "reset_link_requested", "user_id": user_id, "delivery": result.get("status")}
    except PasswordResetError:
        response_body = {"status": "reset_request_recorded", "user_id": user_id, "delivery": "unconfirmed"}
    with session_scope() as session:
        row = session.scalar(select(AdminAuditLogRecord).where(AdminAuditLogRecord.actor_id == current.id, AdminAuditLogRecord.idempotency_key == key).with_for_update())
        row.response_body = response_body
        row.result = "requested" if response_body["status"] == "reset_link_requested" else "unconfirmed"
    return response_body


@router.get("/audit")
def admin_list_audit(
    target_user_id: str | None = None,
    action: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    current: User = Depends(require_admin),
):
    with session_scope() as session:
        stmt = select(AdminAuditLogRecord).order_by(AdminAuditLogRecord.created_at.desc()).limit(limit)
        if target_user_id:
            stmt = stmt.where(AdminAuditLogRecord.target_user_id == target_user_id)
        if action:
            stmt = stmt.where(AdminAuditLogRecord.action == action)
        records = session.scalars(stmt).all()
        identities = {value for row in records for value in (row.actor_id, row.target_user_id) if value}
        names = dict(session.execute(select(UserRecord.id, UserRecord.username).where(UserRecord.id.in_(identities))).all()) if identities else {}
        return [{"actor_name": names.get(row.actor_id), "target_name": names.get(row.target_user_id), "id": row.id, "actor_id": row.actor_id, "target_user_id": row.target_user_id, "action": row.action, "reason": row.reason, "note": row.note, "result": row.result, "before_state": row.before_state, "after_state": row.after_state, "created_at": row.created_at} for row in records]


@router.get("/overview")
def admin_overview(current: User = Depends(require_admin)):
    """Return durable counts plus the last 30 days of collected usage."""
    with session_scope() as session:
        from backend.services.business_config import read_business_config
        config = read_business_config(session)
        users_total = int(session.scalar(select(func.count()).select_from(UserRecord)) or 0)
        users_active = int(session.scalar(select(func.count()).select_from(UserRecord).where(UserRecord.is_active.is_(True))) or 0)
        users_with_email = int(session.scalar(select(func.count()).select_from(UserRecord).where(UserRecord.email.is_not(None))) or 0)
        courses = int(session.scalar(select(func.count()).select_from(CourseRecord)) or 0)
        assignments = int(session.scalar(select(func.count()).select_from(AssignmentRecord)) or 0)
        grading_runs = int(session.scalar(select(func.count()).select_from(GradingRunRecord)) or 0)
    now = time.time()
    usage = query_usage_metrics(
        start=now - 30 * 86400,
        end=now,
        granularity="day",
        metrics=["active_users", "activated_users", "new_users", "login_successes", "tasks_created", "courses_created", "assignments_created", "grading_runs_started", "submissions_created"],
    )
    return {
        "as_of": time.time(),
        "users": {"total": users_total, "active": users_active, "with_email": users_with_email},
        "education": {"courses": courses, "assignments": assignments, "grading_runs": grading_runs},
        "usage": usage,
        "shared_pool": {"enabled": bool(settings.shared_pool_enabled), "daily_request_limit": int(config.values["shared_pool_daily_request_limit"]), "daily_estimated_token_limit": int(config.values["shared_pool_daily_estimated_token_limit"]), "source": "resolved_business_configuration"},
    }
