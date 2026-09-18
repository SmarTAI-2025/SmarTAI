from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from contextlib import contextmanager
from threading import RLock
from typing import Iterator

from sqlalchemy import func, select

from backend.auth import create_token, verify_password
from backend.db.models import InviteCodeRecord, RefreshSessionRecord, UserRecord
from backend.db.session import session_scope
from backend.models import User


class AuthRepositoryError(ValueError):
    pass


# PostgreSQL row locks provide cross-process serialization. SQLite ignores
# SELECT ... FOR UPDATE, so a small striped lock set supplies the equivalent
# guarantee inside the application process used for local development/tests.
_USER_AUTH_LOCKS = tuple(RLock() for _ in range(64))


def _user_auth_lock_for(user_id: str) -> RLock:
    prefix = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:8]
    return _USER_AUTH_LOCKS[int(prefix, 16) % len(_USER_AUTH_LOCKS)]


@contextmanager
def user_auth_lock(user_id: str) -> Iterator[None]:
    lock = _user_auth_lock_for(user_id)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _canonical_email(email: str | None) -> str:
    if not email:
        return ""
    normalized = email.strip().casefold()
    local, separator, domain = normalized.rpartition("@")
    if not separator:
        return normalized
    return f"{local}@{domain.rstrip('.')}"


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
        auth_invalid_before=record.auth_invalid_before,
    )


def create_invite(*, invited_by: str, email: str | None, role: str, course_id: str | None, expires_in_hours: int) -> InviteCodeRecord:
    now = time.time()
    code = secrets.token_urlsafe(9).upper()
    normalized_email = _canonical_email(email)
    record = InviteCodeRecord(code=code, email=normalized_email or None, role=role, course_id=course_id,
                              invited_by=invited_by, created_at=now, expires_at=now + expires_in_hours * 3600)
    with session_scope() as session:
        session.add(record)
    return record


def _ensure_unique_identity(session, *, username: str, email: str) -> None:
    normalized_email = _canonical_email(email)
    if session.scalar(select(UserRecord).where(UserRecord.username == username)) is not None:
        raise AuthRepositoryError("Username already exists")
    if normalized_email and session.scalar(
        select(UserRecord).where(func.lower(UserRecord.email) == normalized_email)
    ) is not None:
        raise AuthRepositoryError("Email already exists")


def _persist_user(*, session, username: str, email: str, password_hash: str,
                  role: str, now: float) -> User:
    normalized_email = _canonical_email(email)
    user = User(id=f"u_{uuid.uuid4().hex[:10]}", username=username, email=normalized_email,
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
    email = _canonical_email(email)
    now = time.time()
    with session_scope() as session:
        _ensure_unique_identity(session, username=username, email=email)
        return _persist_user(session=session, username=username, email=email,
                             password_hash=password_hash, role=role, now=now)


def register_with_invite(*, username: str, email: str, role: str, password_hash: str, invite_code: str | None) -> User:
    email = _canonical_email(email)
    now = time.time()
    with session_scope() as session:
        _ensure_unique_identity(session, username=username, email=email)
        if not invite_code:
            raise AuthRepositoryError("Invitation code required")
        invite = session.scalar(select(InviteCodeRecord).where(InviteCodeRecord.code == invite_code).with_for_update())
        if invite is None or invite.used_at is not None or invite.expires_at <= now:
            raise AuthRepositoryError("Invalid or expired invite code")
        if invite.email and _canonical_email(invite.email) != email:
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


def _locked_user(session, user_id: str) -> UserRecord | None:
    return session.scalar(
        select(UserRecord).where(UserRecord.id == user_id).with_for_update()
    )


def _add_refresh_session(session, *, user_id: str, days: int, now: float) -> str:
    raw = secrets.token_urlsafe(48)
    session.add(RefreshSessionRecord(
        id=uuid.uuid4().hex,
        user_id=user_id,
        token_hash=_hash_token(raw),
        created_at=now,
        last_used_at=now,
        expires_at=now + days * 86400,
    ))
    return raw


def create_refresh_session(user_id: str, days: int) -> str:
    """Create a refresh session while serialized with password invalidation."""
    with user_auth_lock(user_id):
        with session_scope() as session:
            user = _locked_user(session, user_id)
            if user is None or not user.is_active:
                raise AuthRepositoryError("User unavailable")
            return _add_refresh_session(
                session,
                user_id=user_id,
                days=days,
                now=time.time(),
            )


def authenticate_and_create_session(
    username: str,
    password: str,
    days: int,
) -> tuple[str, User, str] | None:
    """Verify credentials and issue both tokens under the user's auth lock.

    Creating the access token before releasing the row lock is intentional: a
    concurrent password reset can then only happen before verification (and the
    old password fails) or afterwards (and its invalidation marker revokes both
    tokens issued here).
    """
    with session_scope() as session:
        user_id = session.scalar(
            select(UserRecord.id).where(UserRecord.username == username)
        )
    if user_id is None:
        return None

    with user_auth_lock(user_id):
        with session_scope() as session:
            user_record = _locked_user(session, user_id)
            if (
                user_record is None
                or user_record.username != username
                or not user_record.is_active
                or not verify_password(password, user_record.password_hash)
            ):
                return None
            now = time.time()
            refresh = _add_refresh_session(
                session,
                user_id=user_id,
                days=days,
                now=now,
            )
            user = _user_from_record(user_record)
            access = create_token(user.id, user.role)
            return refresh, user, access


def rotate_refresh_session(raw: str, days: int) -> tuple[str, User, str] | None:
    token_hash = _hash_token(raw)
    # Resolve the lock key without retaining a transaction while waiting on the
    # process lock. The record is re-read and locked in the real transaction.
    with session_scope() as session:
        user_id = session.scalar(
            select(RefreshSessionRecord.user_id).where(
                RefreshSessionRecord.token_hash == token_hash
            )
        )
    if user_id is None:
        return None

    with user_auth_lock(user_id):
        with session_scope() as session:
            user_record = _locked_user(session, user_id)
            if user_record is None or not user_record.is_active:
                return None
            record = session.scalar(
                select(RefreshSessionRecord)
                .where(RefreshSessionRecord.token_hash == token_hash)
                .with_for_update()
            )
            now = time.time()
            if record is None or record.revoked_at is not None or record.expires_at <= now:
                return None
            if (
                user_record.auth_invalid_before is not None
                and record.created_at <= user_record.auth_invalid_before
            ):
                record.revoked_at = now
                return None
            record.revoked_at = now
            new_raw = _add_refresh_session(
                session,
                user_id=user_id,
                days=days,
                now=now,
            )
            user = _user_from_record(user_record)
            access = create_token(user.id, user.role)
            return new_raw, user, access


def revoke_refresh_session(raw: str | None) -> None:
    if not raw:
        return
    token_hash = _hash_token(raw)
    with session_scope() as session:
        user_id = session.scalar(
            select(RefreshSessionRecord.user_id).where(
                RefreshSessionRecord.token_hash == token_hash
            )
        )
    if user_id is None:
        return
    with user_auth_lock(user_id):
        with session_scope() as session:
            user_record = _locked_user(session, user_id)
            if user_record is None:
                return
            record = session.scalar(
                select(RefreshSessionRecord)
                .where(RefreshSessionRecord.token_hash == token_hash)
                .with_for_update()
            )
            if record is not None and record.revoked_at is None:
                record.revoked_at = time.time()


def revoke_all_refresh_sessions(session, user_id: str, *, now: float | None = None) -> None:
    """Revoke active sessions in a transaction holding the user's auth lock."""
    revoked_at = now if now is not None else time.time()
    session.query(RefreshSessionRecord).filter(
        RefreshSessionRecord.user_id == user_id,
        RefreshSessionRecord.revoked_at.is_(None),
    ).update({RefreshSessionRecord.revoked_at: revoked_at}, synchronize_session=False)
