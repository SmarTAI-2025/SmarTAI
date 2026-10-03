"""Serialize low-volume administration across processes, then recheck the actor.

The database-wide management lock is taken BEFORE any user row lock. Login and
password flows lock only their user row, so they cannot reverse this ordering.
"""
from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy import select, text

from backend.db.models import UserRecord
from backend.db.session import session_scope


def lock_administration(session):
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
    elif session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(734917002117)"))
    else:
        raise RuntimeError("Unsupported administration database")


@contextmanager
def administrator_transaction(current):
    with session_scope() as session:
        lock_administration(session)
        actor = session.scalar(select(UserRecord).where(UserRecord.id == current.id).with_for_update())
        if (actor is None or not actor.is_active or actor.is_read_only or actor.role != "admin"
                or actor.auth_version != current.auth_version
                or actor.auth_invalid_before != current.auth_invalid_before):
            raise HTTPException(403, detail="Administrator authorization changed; sign in again")
        yield session


def protect_administrator(session, current_id, target):
    if target.id == current_id:
        raise HTTPException(409, detail="An administrator cannot restrict or remove their own account")
    if target.role == "admin" and target.is_active and not target.is_read_only:
        remaining = session.scalar(select(UserRecord.id).where(
            UserRecord.role == "admin", UserRecord.is_active.is_(True),
            UserRecord.is_read_only.is_(False), UserRecord.id != target.id,
        ).limit(1))
        if remaining is None:
            raise HTTPException(409, detail="At least one active administrator is required")
