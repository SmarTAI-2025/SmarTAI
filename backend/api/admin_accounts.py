"""Explicit business-access and role management, isolated to private admin."""
import time
from typing import Literal
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select

from backend.api.admin import ActionRequest, _audit, _idempotency_key, _request_hash, _replay_or_conflict, _user_public
from backend.auth import require_admin
from backend.db.auth_repository import revoke_all_refresh_sessions
from backend.db.models import UserRecord
from backend.models import User
from backend.services.admin_transactions import administrator_transaction, protect_administrator

router = APIRouter(prefix="/admin", tags=["admin"])


class AccessRequest(ActionRequest):
    access: Literal["normal", "read_only", "login_blocked"]


class RoleRequest(ActionRequest):
    role: Literal["teacher", "admin"]


def update_account(user_id, req, current, idempotency_key, action):
    key = _idempotency_key(idempotency_key)
    digest = _request_hash(action, user_id, req.model_dump())
    with administrator_transaction(current) as session:
        replay = _replay_or_conflict(session, actor_id=current.id, idempotency_key=key, request_hash=digest)
        if replay is not None:
            return replay
        target = session.scalar(select(UserRecord).where(UserRecord.id == user_id).with_for_update())
        if target is None:
            raise HTTPException(404, detail="User not found")
        from backend.db.models import AccountClosureRecord
        if session.scalar(select(AccountClosureRecord.id).where(AccountClosureRecord.user_id == user_id)):
            raise HTTPException(409, detail="Account closure is in progress")
        before = {"role": target.role, "is_active": target.is_active, "is_read_only": target.is_read_only}
        if action == "account_access_changed":
            if req.access != "normal":
                protect_administrator(session, current.id, target)
            target.is_active = req.access != "login_blocked"
            target.is_read_only = req.access == "read_only"
            revoke = req.access == "login_blocked"
        else:
            if target.role == "admin" and req.role != "admin":
                protect_administrator(session, current.id, target)
            if req.role == "admin" and (not target.is_active or target.is_read_only or not target.email):
                raise HTTPException(409, detail="Restore access and verify an email before granting administration")
            revoke = target.role != req.role
            target.role = req.role
        now = time.time()
        if revoke:
            target.auth_invalid_before = now
            target.auth_version += 1
            revoke_all_refresh_sessions(session, target.id, now=now)
        target.updated_at = now
        result = _user_public(target)
        _audit(session, actor_id=current.id, target_user_id=user_id, action=action,
            reason=req.reason, note=req.note, idempotency_key=key, request_hash=digest,
            before_state=before, after_state={k: result[k] for k in before}, response_body=result)
        return result


@router.patch("/users/{user_id}/access")
def set_access(user_id: str, req: AccessRequest, current: User = Depends(require_admin),
               idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    return update_account(user_id, req, current, idempotency_key, "account_access_changed")


@router.patch("/users/{user_id}/role")
def set_role(user_id: str, req: RoleRequest, current: User = Depends(require_admin),
             idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    return update_account(user_id, req, current, idempotency_key, "account_role_changed")


@router.get("/users/{user_id}")
def get_account(user_id: str, current: User = Depends(require_admin)):
    from backend.db.session import session_scope
    with session_scope() as session:
        target = session.get(UserRecord, user_id)
        if target is None:
            raise HTTPException(404, detail="User not found")
        from backend.db.models import AccountClosureRecord
        closure = session.scalar(select(AccountClosureRecord).where(AccountClosureRecord.user_id == user_id))
        return {**_user_public(target), "closure": {"id": closure.id, "status": closure.status, "mode": closure.mode, "error_code": closure.error_code} if closure else None}


class ClosureRequest(ActionRequest):
    mode: Literal["normal", "blacklist"]
    confirm_username: str


@router.post("/users/{user_id}/closure", status_code=202)
def close_account(user_id: str, req: ClosureRequest, current: User = Depends(require_admin),
                  idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    from backend.db.models import AccountClosureRecord
    import uuid
    key = _idempotency_key(idempotency_key)
    digest = _request_hash("account_closure", user_id, req.model_dump())
    with administrator_transaction(current) as session:
        replay = _replay_or_conflict(session, actor_id=current.id, idempotency_key=key, request_hash=digest)
        if replay is not None:
            return replay
        target = session.scalar(select(UserRecord).where(UserRecord.id == user_id).with_for_update())
        if target is None:
            raise HTTPException(404, detail="User not found")
        protect_administrator(session, current.id, target)
        from backend.db.models import SubmissionRecord, AssignmentRecord
        shared_student_work = session.scalar(select(SubmissionRecord.id).join(
            AssignmentRecord, SubmissionRecord.assignment_id == AssignmentRecord.id
        ).where(SubmissionRecord.student_id == user_id, AssignmentRecord.teacher_id != user_id).limit(1))
        if target.role == "student" or shared_student_work:
            raise HTTPException(409, detail={"code": "account_has_shared_student_work", "message": "该账号可能参与其他教师的批改任务，请先处理相关任务后再销户。"})
        if req.confirm_username != target.username:
            raise HTTPException(400, detail="Enter the exact username to confirm closure")
        if req.mode == "blacklist" and not target.email:
            raise HTTPException(409, detail="An email is required for blacklisting")
        if session.scalar(select(AccountClosureRecord.id).where(AccountClosureRecord.user_id == user_id)):
            raise HTTPException(409, detail="Account closure is already in progress")
        now = time.time()
        target.is_active = False
        target.auth_invalid_before = now
        target.auth_version += 1
        revoke_all_refresh_sessions(session, user_id, now=now)
        closure_id = uuid.uuid4().hex
        session.add(AccountClosureRecord(id=closure_id, user_id=user_id, mode=req.mode, status="pending", created_at=now))
        response = {"status": "closure_pending", "closure_id": closure_id, "mode": req.mode}
        _audit(session, actor_id=current.id, target_user_id=user_id, action="account_closure_requested",
            reason=req.reason, note=req.note, idempotency_key=key, request_hash=digest,
            before_state=None, after_state={"mode": req.mode}, response_body=response)
        return response


@router.get("/closures/{closure_id}")
def closure_status(closure_id: str, current: User = Depends(require_admin)):
    from backend.db.models import AccountClosureRecord
    from backend.db.session import session_scope
    with session_scope() as session:
        row = session.get(AccountClosureRecord, closure_id)
        if row is None:
            raise HTTPException(404)
        return {"id": row.id, "status": row.status, "mode": row.mode, "error_code": row.error_code, "completed_at": row.completed_at}
