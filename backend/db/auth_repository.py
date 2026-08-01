from __future__ import annotations

import hashlib
import secrets
import time
import uuid

from sqlalchemy import select

from backend.db.models import InviteCodeRecord, RefreshSessionRecord, UserRecord
from backend.db.session import session_scope
from backend.models import User


class AuthRepositoryError(ValueError):
    pass


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _user_from_record(record: UserRecord) -> User:
    # Membership lives in course_enrollments; User carries no course_ids mirror.
    return User(
        id=record.id,
        username=record.username,
        email=record.email or "",
        role=record.role,  # type: ignore[arg-type]
        password_hash=record.password_hash,
        created_at=record.created_at,
        is_active=record.is_active,
    )


def create_invite(*, invited_by: str, email: str | None, role: str, course_id: str | None, expires_in_hours: int) -> InviteCodeRecord:
    now = time.time()
    code = secrets.token_urlsafe(9).upper()
    record = InviteCodeRecord(code=code, email=email or None, role=role, course_id=course_id,
                              invited_by=invited_by, created_at=now, expires_at=now + expires_in_hours * 3600)
    with session_scope() as session:
        session.add(record)
    return record


def _ensure_unique_identity(session, *, username: str, email: str) -> None:
    if session.scalar(select(UserRecord).where(UserRecord.username == username)) is not None:
        raise AuthRepositoryError("Username already exists")
    if email and session.scalar(select(UserRecord).where(UserRecord.email == email)) is not None:
        raise AuthRepositoryError("Email already exists")


def _persist_user(*, session, username: str, email: str, password_hash: str,
                  role: str, now: float) -> User:
    user = User(id=f"u_{uuid.uuid4().hex[:10]}", username=username, email=email,
                role=role, password_hash=password_hash, created_at=now)
    session.add(UserRecord(id=user.id, username=user.username, email=user.email or None,
                           role=user.role, password_hash=user.password_hash,
                           is_active=True, created_at=user.created_at, updated_at=now))
    session.flush()  # materialize the row so FK references in the same txn succeed on SQLite
    return user


def register_without_invite(*, username: str, email: str, password_hash: str,
                            role: str = "teacher") -> User:
    """Register a development account with a non-admin public role."""
    if role not in ("teacher", "student"):
        raise AuthRepositoryError("Invalid registration role")
    now = time.time()
    with session_scope() as session:
        _ensure_unique_identity(session, username=username, email=email)
        return _persist_user(session=session, username=username, email=email,
                             password_hash=password_hash, role=role, now=now)


def register_with_invite(*, username: str, email: str, role: str, password_hash: str, invite_code: str | None) -> User:
    now = time.time()
    with session_scope() as session:
        _ensure_unique_identity(session, username=username, email=email)
        if not invite_code:
            raise AuthRepositoryError("Invitation code required")
        invite = session.scalar(select(InviteCodeRecord).where(InviteCodeRecord.code == invite_code).with_for_update())
        if invite is None or invite.used_at is not None or invite.expires_at <= now:
            raise AuthRepositoryError("Invalid or expired invite code")
        if invite.email and invite.email.lower() != email.lower():
            raise AuthRepositoryError("Invite email does not match")
        effective_role = invite.role
        if effective_role not in ("teacher", "student", "admin"):
            raise AuthRepositoryError("Invalid invite role")
        user = _persist_user(session=session, username=username, email=email,
                             password_hash=password_hash, role=effective_role, now=now)
        # Flush the new user row before pointing the invite at it, so SQLite's
        # immediate foreign-key check on the subsequent UPDATE sees the parent.
        session.flush()
        invite.used_at = now
        invite.used_by = user.id
        # A student invite with a pre-bound course enrolls the new user on
        # registration. Done in THIS session (not via course_repository.enroll,
        # which opens a separate session that cannot see the uncommitted user
        # row) so the enrollment commits atomically with the user + invite use.
        if effective_role == "student" and invite.course_id:
            from backend.db.models import CourseEnrollmentRecord, CourseRecord
            course = session.get(CourseRecord, invite.course_id)
            if course is not None:
                existing = session.scalar(
                    select(CourseEnrollmentRecord).where(
                        CourseEnrollmentRecord.course_id == invite.course_id,
                        CourseEnrollmentRecord.student_id == user.id,
                    )
                )
                if existing is None:
                    session.add(CourseEnrollmentRecord(
                        course_id=invite.course_id, student_id=user.id, enrolled_at=now
                    ))
        return user


def create_refresh_session(user_id: str, days: int) -> str:
    raw = secrets.token_urlsafe(48)
    now = time.time()
    with session_scope() as session:
        session.add(RefreshSessionRecord(id=uuid.uuid4().hex, user_id=user_id, token_hash=_hash_token(raw),
                                         created_at=now, last_used_at=now, expires_at=now + days * 86400))
    return raw


def rotate_refresh_session(raw: str, days: int) -> tuple[str, User] | None:
    now = time.time()
    with session_scope() as session:
        record = session.scalar(select(RefreshSessionRecord).where(RefreshSessionRecord.token_hash == _hash_token(raw)))
        if record is None or record.revoked_at is not None or record.expires_at <= now:
            return None
        user_record = session.get(UserRecord, record.user_id)
        if user_record is None or not user_record.is_active:
            return None
        record.revoked_at = now
        new_raw = secrets.token_urlsafe(48)
        session.add(RefreshSessionRecord(id=uuid.uuid4().hex, user_id=record.user_id, token_hash=_hash_token(new_raw),
                                         created_at=now, last_used_at=now, expires_at=now + days * 86400))
        return new_raw, _user_from_record(user_record)


def revoke_refresh_session(raw: str | None) -> None:
    if not raw:
        return
    with session_scope() as session:
        record = session.scalar(select(RefreshSessionRecord).where(RefreshSessionRecord.token_hash == _hash_token(raw)))
        if record and record.revoked_at is None:
            record.revoked_at = time.time()
