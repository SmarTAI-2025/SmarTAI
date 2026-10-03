"""Preview in admin; execution ONLY in the offline maintenance-only app."""
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from backend.auth import require_admin
from backend.models import User
from backend.services.admin_reset import ResetError, _read_json, _write_json, execute_reset, preview_reset, scope_from_settings, _validate_scope, _digest

router = APIRouter(prefix="/admin/maintenance", tags=["admin"])
COOKIE = "smartai_maintenance_operation"
COOKIE_PATH = "/api/admin/maintenance"
_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="maintenance-reset")
_future = None

def configured_scope():
    from backend.config import settings
    client = None
    if settings.storage_backend == "object":
        if os.getenv("SMARTAI_ADMIN_RESET_ALLOW_OBJECT_STORAGE", "false").lower() != "true":
            raise ResetError("reset_object_storage_requires_explicit_authorization")
        from backend.storage import build_storage
        client = build_storage().client
    return scope_from_settings(object_client=client)

def _error(exc):
    code = str(exc) if isinstance(exc, ResetError) else "reset_failed_keep_services_stopped"
    return HTTPException(409, detail={"code": code, "message": "维护检查未通过，请保留当前记录、保持普通服务停止后重试。"})

def _offline(request):
    if not getattr(request.app.state, "maintenance_only", False):
        raise HTTPException(409, detail={"code": "reset_offline_service_required", "message": "请停止普通服务并启动回环地址的专用维护服务。"})

def _grant(scope, request, fingerprint=None):
    value = request.cookies.get(COOKIE, "")
    match = re.fullmatch(r"([0-9a-f]{64}-[0-9a-f]{32})\.([A-Za-z0-9_-]{43})", value)
    if match is None or fingerprint is not None and match[1] != fingerprint:
        return None
    record = _read_json(scope.maintenance_dir / f"authorization-{match[1]}.json")
    if not record or record["expires_at"] < time.time() or record["scope_fingerprint"] != _digest(_validate_scope(scope)):
        return None
    if not hmac.compare_digest(record["token_hash"], hashlib.sha256(match[2].encode()).hexdigest()):
        return None
    return record

def _same_origin(request):
    # A grant cookie is not a login cookie. Mutations require same-origin;
    # maintenance is served on its own private origin, never cross-site CORS.
    origin = request.headers.get("origin")
    if origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, detail="Same-origin maintenance confirmation required")

@router.get("/preview")
def reset_preview(request: Request, response: Response, current: User = Depends(require_admin)):
    response.headers["Cache-Control"] = "no-store"
    from backend.config import settings
    try:
        scope = configured_scope()
        value = preview_reset(scope)
        return {"available": True, "execution_available": bool(getattr(request.app.state, "maintenance_only", False) and scope.password_hash), **value}
    except ResetError as exc:
        return {"available": False, "reason": str(exc), "environment": settings.runtime_environment}
    except Exception:
        response.status_code = 503
        return {"available": False, "reason": "preview_unavailable", "environment": settings.runtime_environment}

class ResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}-[0-9a-f]{32}$")
    confirmation: str = Field(max_length=160)
    maintenance_password: SecretStr = Field(min_length=1, max_length=128)
    services_stopped: bool

@router.post("/execute", status_code=202)
def reset_execute(body: ResetRequest, request: Request, response: Response):
    global _future
    _offline(request); _same_origin(request)
    try:
        scope = configured_scope()
        with _lock:
            record = _grant(scope, request, body.fingerprint)
            if record is None:
                authorization = request.headers.get("authorization", "")
                scheme, _, token = authorization.partition(" ")
                if scheme.lower() != "bearer" or not token:
                    raise HTTPException(401, detail="Administrator login required")
                from backend.auth import get_optional_user, get_current_user
                from backend.state import get_user_store
                current = require_admin(get_current_user(request, get_optional_user(token, get_user_store(), request)))
                from backend.services.admin_transactions import administrator_transaction
                with administrator_transaction(current):
                    pass
            from backend.services.maintenance_auth import verify_maintenance_password
            verify_maintenance_password(scope, body.maintenance_password.get_secret_value())
            from backend.services.admin_reset import CONFIRMATION, _process_lock
            if not body.services_stopped or body.confirmation != f"{CONFIRMATION} {body.fingerprint[:12]}":
                raise ResetError("reset_confirmation_mismatch")
            if _future is not None and not _future.done():
                active = _read_json(scope.maintenance_dir / "authorized-reset.json")
                if not active or active["fingerprint"] != body.fingerprint:
                    raise ResetError("reset_another_plan_incomplete")
            else:
                # Check lifetime locks before accepting a job. execute_reset
                # rechecks atomically; a restarted service makes it fail closed.
                with _process_lock(scope):
                    pass
            active_path = scope.maintenance_dir / "active-reset.json"
            active = _read_json(active_path)
            receipt = _read_json(scope.maintenance_dir / f"receipt-{body.fingerprint}.json")
            approved = _read_json(scope.maintenance_dir / f"authorization-{body.fingerprint}.json")
            if active and active["fingerprint"] != body.fingerprint:
                raise ResetError("reset_another_plan_incomplete")
            if active is None and receipt is None and approved is None:
                preview = preview_reset(scope)
                if preview["fingerprint"].split("-")[0] != body.fingerprint.split("-")[0]:
                    raise ResetError("reset_preview_stale")
            token = secrets.token_urlsafe(32)
            record = {"fingerprint": body.fingerprint, "scope_fingerprint": _digest(_validate_scope(scope)), "token_hash": hashlib.sha256(token.encode()).hexdigest(), "expires_at": time.time()+86400, "authorized_at": time.time(), "actor_digest": record.get("actor_digest") if record else hashlib.sha256(current.id.encode()).hexdigest()}
            _write_json(scope.maintenance_dir / f"authorization-{body.fingerprint}.json", record)
            _write_json(scope.maintenance_dir / "authorized-reset.json", {"fingerprint": body.fingerprint})
            if _future is None or _future.done():
                def run():
                    try:
                        execute_reset(scope, fingerprint=body.fingerprint, confirmation=body.confirmation, services_stopped=True, maintenance_password=body.maintenance_password.get_secret_value())
                    except Exception as exc:
                        _write_json(scope.maintenance_dir / f"failure-{body.fingerprint}.json", {"fingerprint": body.fingerprint, "phase": "failed", "error_code": str(exc) if isinstance(exc, ResetError) else "reset_failed_keep_services_stopped"})
                _future = _executor.submit(run)
            response.set_cookie(COOKIE, f"{body.fingerprint}.{token}", httponly=True, secure=request.url.scheme == "https", samesite="strict", path=COOKIE_PATH, max_age=86400)
            response.headers["Cache-Control"] = "no-store"
            return {"fingerprint": body.fingerprint, "status": "accepted"}
    except ResetError as exc:
        raise _error(exc) from None

@router.get("/status")
def reset_status(request: Request, response: Response):
    _offline(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        scope = configured_scope()
        record = _grant(scope, request)
        if not record:
            raise HTTPException(401, detail="No valid maintenance operation session")
        fingerprint = record["fingerprint"]
        receipt = _read_json(scope.maintenance_dir / f"receipt-{fingerprint}.json")
        if receipt:
            return {"status": "completed", **receipt}
        active = _read_json(scope.maintenance_dir / "active-reset.json")
        failure = _read_json(scope.maintenance_dir / f"failure-{fingerprint}.json")
        if _future is not None and not _future.done():
            return {"fingerprint": fingerprint, "status": "running", "phase": active.get("phase", "validating") if active else "validating", "files_remaining": active.get("files_remaining") if active else None}
        value = active if active and active["fingerprint"] == fingerprint else failure
        return {"fingerprint": fingerprint, "status": "failed" if value else "interrupted", "phase": value.get("phase", "failed") if value else "interrupted", "error_code": value.get("error_code", "reset_interrupted_resume_same_plan") if value else "reset_interrupted_resume_same_plan"}
    except ResetError as exc:
        raise _error(exc) from None
