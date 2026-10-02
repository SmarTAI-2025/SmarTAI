"""Create the first administrator, only after Alembic migrations are complete.

This command never overwrites or promotes an existing account. Subsequent
management delegation uses the authenticated private console.
"""
from __future__ import annotations
import argparse
import getpass
import sys
import time
import uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def bootstrap_admin(username: str, email: str, password: str) -> str:
    from sqlalchemy import select
    from backend.auth import hash_password
    from backend.db.models import UserRecord, AdminAuditLogRecord
    from backend.db.auth_repository import _ensure_unique_identity
    from backend.db.session import session_scope, admin_schema_ready
    from backend.services.admin_transactions import lock_administration
    from backend.services.email_registration import normalize_email
    if not 3 <= len(username.strip()) <= 64 or not 12 <= len(password) <= 128:
        raise ValueError("Use a 3–64 character username and a 12–128 character password")
    normalized_email = normalize_email(email)
    if not admin_schema_ready():
        raise ValueError("Run Alembic upgrade head before initialization")
    with session_scope() as session:
        lock_administration(session)
        if session.scalar(select(UserRecord.id).where(UserRecord.role == "admin").limit(1)):
            raise ValueError("An administrator already exists; initialization is unavailable")
        _ensure_unique_identity(session, username=username.strip(), email=normalized_email)
        now = time.time()
        user_id = f"u_{uuid.uuid4().hex}"
        session.add(UserRecord(id=user_id, username=username.strip(), email=normalized_email,
            role="admin", password_hash=hash_password(password), is_active=True,
            is_read_only=False, created_at=now, updated_at=now))
        action_id = uuid.uuid4().hex
        session.add(AdminAuditLogRecord(id=action_id, actor_id="local-bootstrap", target_user_id=user_id,
            action="first_admin_initialized", reason="local_operator", note="",
            idempotency_key=action_id, request_hash="credential_not_recorded", result="success", created_at=now))
    return user_id


def main():
    parser = argparse.ArgumentParser(description="Initialize the first SmarTAI administrator locally")
    parser.add_argument("username")
    parser.add_argument("--email", required=True)
    args = parser.parse_args()
    password = getpass.getpass("New administrator password: ")
    if getpass.getpass("Confirm password: ") != password:
        raise SystemExit("Passwords do not match")
    try:
        bootstrap_admin(args.username, args.email, password)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print("First administrator created. Sign in through the private console.")


if __name__ == "__main__":
    main()
