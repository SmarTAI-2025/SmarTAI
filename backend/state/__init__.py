"""Process-wide user access helpers.

The normalized redesign removed the legacy in-memory problem/student/job/task
stores (their tables were dropped in the schema baseline). What remains here is
the thin user access layer used by auth seeding and the identity dependencies:
membership is read only from ``course_enrollments``, so there is no
``course_ids`` mirror to keep in sync.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select

from backend.db.models import UserRecord
from backend.db.session import session_scope
from backend.models import User

logger = logging.getLogger(__name__)


def _user_from_record(record: UserRecord) -> User:
    return User(
        id=record.id,
        username=record.username,
        email=record.email or "",
        role=record.role,  # type: ignore[arg-type]
        password_hash=record.password_hash,
        created_at=record.created_at,
        is_active=record.is_active,
    )


class _UserStore:
    """Dict-compatible facade over the ``users`` table.

    Kept as a mapping so legacy auth code can do ``store[id] = user`` /
    ``store.get(id)`` while the table is the source of truth.
    """

    def __getitem__(self, key: str) -> User:
        with session_scope() as session:
            record = session.get(UserRecord, key)
            if record is None:
                raise KeyError(key)
            return _user_from_record(record)

    def __setitem__(self, key: str, user: User) -> None:
        with session_scope() as session:
            record = session.get(UserRecord, key)
            now = __import__("time").time()
            values = dict(
                id=key,
                username=user.username,
                email=user.email or None,
                role=user.role,
                password_hash=user.password_hash,
                is_active=user.is_active,
                created_at=user.created_at,
                updated_at=now,
            )
            if record is None:
                session.add(UserRecord(**values))
            else:
                for k, v in values.items():
                    setattr(record, k, v)

    def __delitem__(self, key: str) -> None:
        from sqlalchemy import delete

        with session_scope() as session:
            result = session.execute(delete(UserRecord).where(UserRecord.id == key))
            if result.rowcount == 0:
                raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        with session_scope() as session:
            return session.get(UserRecord, key) is not None

    def get(self, key: str, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def values(self):
        with session_scope() as session:
            records = session.scalars(select(UserRecord).order_by(UserRecord.created_at)).all()
            return [_user_from_record(r) for r in records]

    def get_by_username(self, username: str) -> Optional[User]:
        with session_scope() as session:
            record = session.scalar(select(UserRecord).where(UserRecord.username == username))
            return _user_from_record(record) if record else None


_user_store = _UserStore()


def get_user_store() -> _UserStore:
    return _user_store


def find_user_by_username(username: str) -> Optional[User]:
    return _user_store.get_by_username(username)


def register_user(user: User) -> None:
    _user_store[user.id] = user


def remove_user(user_id: str) -> bool:
    try:
        del _user_store[user_id]
        return True
    except KeyError:
        return False
