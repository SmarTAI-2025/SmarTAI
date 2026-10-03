"""Independent maintenance secret. Never persisted in business data or audit."""
import time
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.auth import verify_password
from backend.db.models import UserRecord

def verify_maintenance_password(scope, password):
    from backend.services.admin_reset import ResetError, _read_json, _write_json, _safe_path
    if not scope.password_hash or not scope.password_hash.startswith(("$smartai-bcrypt-sha256$", "$2")):
        raise ResetError("reset_maintenance_password_not_configured")
    # This process is single-worker; the caller serializes authorizations.
    control = _safe_path(scope.maintenance_dir)
    control.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = control / "password-attempts.json"
    record = _read_json(path) or {"failed": 0, "since": time.time()}
    if time.time() - record["since"] >= 900:
        record = {"failed": 0, "since": time.time()}
    if record["failed"] >= 5:
        raise ResetError("reset_maintenance_password_rate_limited")
    if not password or not verify_password(password, scope.password_hash):
        record["failed"] += 1
        _write_json(path, record)
        raise ResetError("reset_maintenance_password_invalid")
    _write_json(path, {"failed": 0, "since": time.time()})

def authenticate_operator(scope, username, password):
    from backend.services.admin_reset import ResetError
    with Session(scope.engine) as session:
        row = session.scalar(select(UserRecord).where(UserRecord.username == username))
        if row is None or row.role != "admin" or not row.is_active or not verify_password(password, row.password_hash):
            raise ResetError("reset_administrator_authentication_required")
        return row.id
