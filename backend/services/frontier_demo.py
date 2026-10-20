"""Narrow anonymous task capabilities; all work uses the normal task pipeline.

Only admission and identity are special. Credentials are deployment-owned and
every logical model call consumes one durable, global daily allowance before
dispatch, including failed calls. These counts are not vendor billing tokens.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import jwt
from fastapi import HTTPException
from sqlalchemy import select, text

from backend.config import settings
from backend.db.frontier_demo_models import FrontierDemoDailyUsageRecord
from backend.db.models import UserRecord
from backend.db.session import get_session
from backend.models import User

USER_PREFIX = "frontier_demo_"
SESSION_SECONDS = 2 * 3600


def is_frontier_user(user) -> bool:
    # IDs are generated only by this service. The prefix also survives worker
    # recovery where request-local auth_scope is intentionally not persisted.
    return bool(user and str(user.id).startswith(USER_PREFIX)
                and user.role == "teacher" and not user.email and not user.password_hash)


def queue_cleanup(*, owner_id: str | None = None) -> int:
    """Revoke and durably queue ended/expired guests, including after restart.

    The absolute two-hour lifetime is unchanged. Daily admission counters are
    deliberately independent of guest deletion, so ending a session cannot
    buy another allowance. An explicit owner is used only by the end route.
    """
    from backend.db.models import AccountClosureRecord
    from backend.db.session import session_scope
    from backend.services.admin_transactions import lock_administration
    now = time.time()
    with session_scope() as session:
        lock_administration(session)
        candidates = select(UserRecord).where(
            UserRecord.id.startswith(USER_PREFIX, autoescape=True),
            UserRecord.role == "teacher", UserRecord.password_hash == "",
            (UserRecord.email.is_(None)) | (UserRecord.email == ""),
            ~select(AccountClosureRecord.id).where(
                AccountClosureRecord.user_id == UserRecord.id).exists(),
        )
        if owner_id is not None:
            candidates = candidates.where(UserRecord.id == owner_id)
        else:
            candidates = candidates.where(
                (UserRecord.created_at <= now - SESSION_SECONDS)
                | UserRecord.is_active.is_(False))
        users = list(session.scalars(candidates.order_by(UserRecord.created_at).limit(100).with_for_update()))
        for user in users:
            user.is_active = False
            user.auth_version += 1
            user.auth_invalid_before = now
            user.updated_at = now
            session.add(AccountClosureRecord(id=uuid.uuid4().hex, user_id=user.id,
                mode="frontier_demo", status="pending", created_at=now))
        return len(users)


def _expired():
    return HTTPException(401, detail={"code": "frontier_demo_session_expired"})


def create_token(user: User, *, deadline: int | None = None) -> str:
    now = time.time()
    deadline = deadline or int(now + SESSION_SECONDS)
    return jwt.encode({
        "sub": user.id, "role": "teacher", "scope": "frontier_demo",
        "session_scope": "public", "auth_version": user.auth_version,
        "iat": now, "exp": min(int(now + settings.frontier_demo_session_minutes * 60), deadline),
        "session_exp": deadline, "jti": uuid.uuid4().hex,
    }, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _session_owner(token: str, *, cleanup_only: bool = False) -> tuple[User, int]:
    """Cleanup may outlive admission, but never signature or identity revocation."""
    from backend.state import get_user_store
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm],
                             options={"verify_exp": False, "require": ["sub", "iat", "exp", "session_exp"]})
        deadline = int(payload["session_exp"])
        user = get_user_store().get(payload["sub"])
        if (payload.get("scope") != "frontier_demo"
                or payload.get("session_scope") != "public" or payload.get("role") != "teacher"
                or not is_frontier_user(user)
                or not user.is_active or user.role != "teacher"
                or payload.get("auth_version") != user.auth_version
                or (user.auth_invalid_before is not None and float(payload["iat"]) <= user.auth_invalid_before)
                or (not cleanup_only and (not settings.frontier_demo_enabled or deadline <= time.time()))):
            raise _expired()
    except (jwt.InvalidTokenError, TypeError, ValueError, OverflowError):
        raise _expired() from None
    return user.model_copy(update={"auth_scope": "frontier_demo"}), deadline


def renew_token(token: str) -> tuple[User, str]:
    """Expired access can renew its original owner within an absolute deadline."""
    user, deadline = _session_owner(token)
    return user, create_token(user, deadline=deadline)


def cleanup_user(token: str) -> User:
    """Resolve only the original owner for destruction; never issue credentials."""
    user, _ = _session_owner(token, cleanup_only=True)
    return user


def _daily_row(session, now):
    """SQLite serializes writers; PostgreSQL locks one upserted row per UTC day."""
    dialect = session.bind.dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
        session.execute(text("BEGIN IMMEDIATE"))
    else:
        from sqlalchemy.dialects.postgresql import insert
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    session.execute(insert(FrontierDemoDailyUsageRecord).values(
        day=day, sessions=0, requests=0, estimated_input_tokens=0, last_session_at=0,
    ).on_conflict_do_nothing(index_elements=["day"]))
    return session.scalar(select(FrontierDemoDailyUsageRecord).where(
        FrontierDemoDailyUsageRecord.day == day).with_for_update())


def issue_user() -> User:
    now = time.time()
    identity = uuid.uuid4().hex
    user = User(id=USER_PREFIX + identity, username="frontier-" + identity,
                role="teacher", password_hash="", created_at=now, auth_scope="frontier_demo")
    with get_session() as session:
        row = _daily_row(session, now)
        if row.sessions >= settings.frontier_demo_daily_session_limit:
            raise HTTPException(429, detail={"code": "frontier_demo_daily_limit_reached"})
        remaining = settings.frontier_demo_session_cooldown_seconds - (now - row.last_session_at)
        if remaining > 0:
            raise HTTPException(429, detail={"code": "frontier_demo_session_cooldown"},
                                headers={"Retry-After": str(int(remaining) + 1)})
        row.sessions += 1
        row.last_session_at = now
        session.add(UserRecord(id=user.id, username=user.username, email=None,
                               password_hash="", role="teacher", created_at=now, updated_at=now))
        session.commit()
    return user


def admit_model_call(owner_id: str, estimated_input_tokens: int) -> None:
    from backend.llm.registry import SharedPoolLimitError
    if not settings.frontier_demo_enabled:
        raise SharedPoolLimitError("shared_pool_disabled")
    now = time.time()
    with get_session() as session:
        row = _daily_row(session, now)
        owner = session.get(UserRecord, owner_id)
        if (owner is None or not is_frontier_user(owner) or not owner.is_active
                or owner.is_read_only or owner.created_at + SESSION_SECONDS <= now):
            raise SharedPoolLimitError("shared_pool_disabled")
        if (row.requests + 1 > settings.frontier_demo_daily_request_limit
                or row.estimated_input_tokens + estimated_input_tokens > settings.frontier_demo_daily_estimated_token_limit):
            raise SharedPoolLimitError("shared_pool_daily_limit_reached")
        row.requests += 1
        row.estimated_input_tokens += estimated_input_tokens
        session.commit()
