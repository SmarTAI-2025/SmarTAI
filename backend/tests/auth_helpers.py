"""Issue current, revocable credentials for API tests with seeded accounts.

Authentication contract tests use real login or the raw signer explicitly.
This helper does not bypass any request authentication or authorization check.
"""
import hashlib
import time

from backend.auth import create_token
from backend.db.auth_repository import _add_refresh_session
from backend.db.models import UserRecord
from backend.db.session import session_scope


def create_session_token(user_id, role, **claims):
    with session_scope() as session:
        user = session.get(UserRecord, user_id)
        if user is None:
            return create_token(user_id, role, **claims)
        raw = _add_refresh_session(
            session, user_id=user_id, days=1, now=time.time(),
            session_scope=claims.get("session_scope", "public"),
        )
        claims.setdefault("auth_version", user.auth_version)
        claims.setdefault("session_id", hashlib.sha256(raw.encode()).hexdigest())
    return create_token(user_id, role, **claims)
