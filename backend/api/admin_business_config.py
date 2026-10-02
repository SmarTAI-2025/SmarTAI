"""Private-only, audited business settings. Mount only in the admin app."""
from __future__ import annotations

import time
from typing import Any
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from backend.api.admin import _audit, _request_hash, _replay_or_conflict
from backend.auth import require_admin
from backend.config import settings
from backend.db.business_config_models import BusinessConfigRecord, UserStorageConfigRecord
from backend.db.models import AccountClosureRecord, UserRecord
from backend.db.session import session_scope
from backend.models import User
from backend.services.admin_transactions import administrator_transaction
from backend.services.business_config import BOUNDS, STORAGE_KEYS, read_business_config, validate_changes

router = APIRouter(prefix="/admin/business-config", tags=["admin"])


class ConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0, strict=True)
    expected_global_version: int | None = Field(default=None, ge=0, strict=True)
    changes: dict[str, Any]
    reason: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=1000)

    @field_validator("reason", "note", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value


def _view(session, owner_id=None):
    config = read_business_config(session, owner_id)
    keys = STORAGE_KEYS if owner_id is not None else config.values.keys()
    overrides = config.user_overrides if owner_id is not None else config.global_overrides
    result = {
        "scope": "user" if owner_id is not None else "global", "owner_id": owner_id,
        "version": config.user_version if owner_id is not None else config.global_version,
        "global_version": config.global_version,
        "fields": {key: {"effective": config.values[key], "source": config.sources[key],
                         "override": overrides.get(key), "bounds": BOUNDS.get(key)} for key in keys},
    }
    if owner_id is not None:
        from backend.db.source_storage_repository import _usage_in_session as source_usage
        from backend.db.knowledge_storage_repository import _usage_in_session as knowledge_usage
        result["usage"] = {"unfinished_source_quota_bytes": source_usage(session, owner_id).as_dict(),
                           "knowledge_storage_quota_bytes": knowledge_usage(session, owner_id).as_dict()}
        # Use exactly the config snapshot returned above for both displayed limits.
        for key, usage in result["usage"].items():
            usage["limit_bytes"] = config.values[key]
            usage["available_bytes"] = max(0, config.values[key] - usage["used_bytes"])
    else:
        result["read_only"] = {
            "email_verification_expiry_seconds": settings.email_verification_expiry_seconds,
            "password_recovery_independent_of_registration": config.registration_rules_managed,
            "shared_pool_daily_request_limit": settings.shared_pool_daily_request_limit,
            "shared_pool_daily_estimated_token_limit": settings.shared_pool_daily_estimated_token_limit,
            "history_query_llm_daily_limit": settings.history_query_llm_daily_limit,
            "model_quota_note": "现有模型日额度按进程计数，尚非可靠的全局计量；此处只读。",
        }
    return result


def _target(session, user_id):
    target = session.scalar(select(UserRecord).where(UserRecord.id == user_id).with_for_update())
    if target is None:
        raise HTTPException(404, detail="User not found")
    if session.scalar(select(AccountClosureRecord.id).where(AccountClosureRecord.user_id == user_id)):
        raise HTTPException(409, detail={"code": "account_closing", "message": "账号正在销户，不能修改配额。"})


@router.get("")
def get_global(current: User = Depends(require_admin)):
    with session_scope() as session:
        return _view(session)


@router.get("/users/{user_id}")
def get_user_config(user_id: str, current: User = Depends(require_admin)):
    with session_scope() as session:
        if session.get(UserRecord, user_id) is None:
            raise HTTPException(404, detail="User not found")
        return _view(session, user_id)


def _save(req, current, key, owner_id=None):
    if not key or not key.strip() or len(key) > 128:
        raise HTTPException(400, detail="An Idempotency-Key of 1–128 characters is required")
    if owner_id is not None and req.expected_global_version is None:
        raise HTTPException(422, detail="expected_global_version is required for user overrides")
    try:
        changes = validate_changes(req.changes, user_scope=owner_id is not None)
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "invalid_business_configuration", "message": str(exc)}) from exc
    action = "user_storage_configuration_changed" if owner_id is not None else "business_configuration_changed"
    digest = _request_hash(action, owner_id, req.model_dump())
    with administrator_transaction(current) as session:
        replay = _replay_or_conflict(session, actor_id=current.id, idempotency_key=key.strip(), request_hash=digest)
        if replay is not None:
            return replay
        if owner_id is not None:
            _target(session, owner_id)
        config = read_business_config(session, owner_id)
        version = config.user_version if owner_id is not None else config.global_version
        if version != req.expected_version or (owner_id is not None and config.global_version != req.expected_global_version):
            raise HTTPException(409, detail={"code": "business_configuration_conflict", "message": "配置已被其他管理员修改，请重新载入后核对。"})
        before = dict(config.user_overrides if owner_id is not None else config.global_overrides)
        after = dict(before)
        for name, value in changes.items():
            if value is None:
                after.pop(name, None)
            else:
                after[name] = value
        model = UserStorageConfigRecord if owner_id is not None else BusinessConfigRecord
        identity = {"owner_id": owner_id} if owner_id is not None else {"id": "global"}
        values = {"overrides": after, "version": version + 1, "updated_at": time.time()}
        if owner_id is None:
            values["registration_rules_managed"] = config.registration_rules_managed or (
                "allowed_email_domains" in changes and changes["allowed_email_domains"] is not None)
        if version == 0:
            session.add(model(**identity, **values))
        else:
            identity_column = model.owner_id if owner_id is not None else model.id
            result = session.execute(update(model).where(identity_column == (owner_id or "global"), model.version == version).values(**values))
            if result.rowcount != 1:
                raise HTTPException(409, detail={"code": "business_configuration_conflict"})
        session.flush()
        response = _view(session, owner_id)
        _audit(session, actor_id=current.id, target_user_id=owner_id, action=action,
               reason=req.reason, note=req.note, idempotency_key=key.strip(), request_hash=digest,
               before_state={"version": version, "overrides": before},
               after_state={"version": version + 1, "overrides": after}, response_body=response)
        return response


@router.patch("")
def save_global(req: ConfigUpdate, current: User = Depends(require_admin),
                idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    return _save(req, current, idempotency_key)


@router.patch("/users/{user_id}")
def save_user_config(user_id: str, req: ConfigUpdate, current: User = Depends(require_admin),
                     idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    return _save(req, current, idempotency_key, user_id)
