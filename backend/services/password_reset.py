from __future__ import annotations

import hashlib
import hmac
import logging
import math
import secrets
import time
import uuid
from contextlib import contextmanager
from threading import Lock
from typing import Iterator

from sqlalchemy import and_, delete, func, or_, select, text

from backend.auth import hash_password
from backend.config import settings
from backend.db.auth_repository import revoke_all_refresh_sessions, user_auth_lock
from backend.db.models import (
    PasswordResetRateEventRecord,
    PasswordResetRequestRecord,
    UserRecord,
)
from backend.db.session import session_scope
from backend.services.email_sender import EmailSender, get_email_sender, password_reset_message
from backend.services.email_registration import email_domain_allowed, normalize_email


logger = logging.getLogger(__name__)


class PasswordResetError(ValueError):
    def __init__(self, code: str, *, status_code: int = 400, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after


_RESET_FLOW_LOCKS = tuple(Lock() for _ in range(64))


def _reset_flow_keys(normalized_email: str, source_ip: str | None) -> tuple[str, ...]:
    keys = [f"password-reset:email:{normalized_email}"]
    if source_ip is not None:
        keys.append(f"password-reset:ip:{source_ip}")
    return tuple(keys)


def _reset_flow_lock(key: str) -> Lock:
    prefix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    return _RESET_FLOW_LOCKS[int(prefix, 16) % len(_RESET_FLOW_LOCKS)]


@contextmanager
def _process_reset_flow_locks(
    normalized_email: str,
    source_ip: str | None,
) -> Iterator[None]:
    unique = {
        id(lock): lock
        for key in _reset_flow_keys(normalized_email, source_ip)
        for lock in (_reset_flow_lock(key),)
    }
    locks = [unique[key] for key in sorted(unique)]
    for lock in locks:
        lock.acquire()
    try:
        yield
    finally:
        for lock in reversed(locks):
            lock.release()


def _database_reset_flow_lock_id(key: str) -> int:
    return int.from_bytes(
        hashlib.sha256(key.encode("utf-8")).digest()[:8],
        "big",
        signed=True,
    )


def _acquire_database_reset_flow_locks(
    session,
    *,
    normalized_email: str,
    source_ip: str | None,
) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    lock_ids = sorted({
        _database_reset_flow_lock_id(key)
        for key in _reset_flow_keys(normalized_email, source_ip)
    })
    for lock_id in lock_ids:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": lock_id},
        )


def generate_reset_token() -> str:
    return secrets.token_urlsafe(96)


def digest_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _rate_limit_digest(purpose: str, value: str) -> str:
    """Create a purpose-separated, non-reversible limiter key."""
    return hmac.new(
        settings.jwt_secret.encode("utf-8"),
        f"password-reset:{purpose}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _neutral_response() -> dict[str, int | str]:
    return {
        "status": "reset_link_requested",
        "expires_in_seconds": settings.email_verification_expiry_seconds,
        "resend_after_seconds": settings.email_verification_resend_seconds,
    }


def _activate_delivered_request(*, user_id: str, request_id: str) -> None:
    """Activate a delivered token under the newest-successful-wins rule.

    SMTP completions can arrive out of creation order across workers. A
    successful request supersedes only older rows, never a newer pending row.
    Thus an older success remains usable while a newer delivery is pending; a
    newer success later replaces it, while a newer failure leaves it usable.
    """
    with session_scope() as session:
        user = session.scalar(
            select(UserRecord).where(UserRecord.id == user_id).with_for_update()
        )
        delivered = session.scalar(
            select(PasswordResetRequestRecord)
            .where(PasswordResetRequestRecord.id == request_id)
            .with_for_update()
        )
        if (
            user is None
            or not user.is_active
            or delivered is None
            or delivered.delivery_status != "pending"
        ):
            raise PasswordResetError("password_reset_unavailable")

        delivered.delivery_status = "sent"
        if delivered.superseded_at is not None:
            return

        activated_at = time.time()
        newer_sent = session.scalar(
            select(PasswordResetRequestRecord.id).where(
                PasswordResetRequestRecord.user_id == user_id,
                PasswordResetRequestRecord.id != request_id,
                PasswordResetRequestRecord.superseded_at.is_(None),
                PasswordResetRequestRecord.consumed_at.is_(None),
                PasswordResetRequestRecord.delivery_status == "sent",
                or_(
                    PasswordResetRequestRecord.created_at > delivered.created_at,
                    and_(
                        PasswordResetRequestRecord.created_at == delivered.created_at,
                        PasswordResetRequestRecord.id > delivered.id,
                    ),
                ),
            )
        )
        if newer_sent is not None:
            delivered.superseded_at = activated_at
            return

        older_rows = session.scalars(
            select(PasswordResetRequestRecord)
            .where(
                PasswordResetRequestRecord.user_id == user_id,
                PasswordResetRequestRecord.id != request_id,
                PasswordResetRequestRecord.superseded_at.is_(None),
                PasswordResetRequestRecord.consumed_at.is_(None),
                PasswordResetRequestRecord.delivery_status.in_(("pending", "sent")),
                or_(
                    PasswordResetRequestRecord.created_at < delivered.created_at,
                    and_(
                        PasswordResetRequestRecord.created_at == delivered.created_at,
                        PasswordResetRequestRecord.id < delivered.id,
                    ),
                ),
            )
            .with_for_update()
        ).all()
        for older in older_rows:
            older.superseded_at = activated_at


def request_password_reset(*, email: str, source_ip: str | None, sender: EmailSender | None = None) -> dict[str, int | str]:
    """Request a reset without disclosing whether an account is present."""
    try:
        normalized_email = normalize_email(email)
    except ValueError:
        return _neutral_response()
    if not email_domain_allowed(normalized_email, settings.allowed_email_domains):
        return _neutral_response()

    with _process_reset_flow_locks(normalized_email, source_ip):
        return _request_password_reset_locked(
            normalized_email=normalized_email,
            source_ip=source_ip,
            sender=sender,
        )


def _request_password_reset_locked(
    *,
    normalized_email: str,
    source_ip: str | None,
    sender: EmailSender | None,
) -> dict[str, int | str]:
    sender = sender or get_email_sender()
    now = time.time()
    raw_token = generate_reset_token()

    email_digest = _rate_limit_digest("email", normalized_email)
    source_ip_digest = (
        _rate_limit_digest("ip", source_ip) if source_ip is not None else None
    )
    window_start = now - 3600
    retry_candidates: list[int] = []
    # Account lookup happens only after this shared anonymous budget check.
    # Accepted attempts for unknown and known mailboxes consume the same
    # budget without persisting plaintext email/IP values. Already-rejected
    # attempts are not appended indefinitely, and expired events are pruned,
    # so a throttled caller cannot grow the limiter table without bound.
    with session_scope() as session:
        _acquire_database_reset_flow_locks(
            session,
            normalized_email=normalized_email,
            source_ip=source_ip,
        )
        session.execute(
            delete(PasswordResetRateEventRecord).where(
                PasswordResetRateEventRecord.created_at < window_start
            )
        )
        email_count, email_oldest, email_latest = session.execute(
            select(
                func.count(PasswordResetRateEventRecord.id),
                func.min(PasswordResetRateEventRecord.created_at),
                func.max(PasswordResetRateEventRecord.created_at),
            ).where(
                PasswordResetRateEventRecord.email_digest == email_digest,
                PasswordResetRateEventRecord.created_at >= window_start,
            )
        ).one()
        ip_count = 0
        ip_oldest = None
        if source_ip_digest is not None:
            ip_count, ip_oldest = session.execute(
                select(
                    func.count(PasswordResetRateEventRecord.id),
                    func.min(PasswordResetRateEventRecord.created_at),
                ).where(
                    PasswordResetRateEventRecord.source_ip_digest == source_ip_digest,
                    PasswordResetRateEventRecord.created_at >= window_start,
                )
            ).one()
        if email_latest is not None:
            cooldown_remaining = (
                email_latest
                + settings.email_verification_resend_seconds
                - now
            )
            if cooldown_remaining > 0:
                retry_candidates.append(max(1, math.ceil(cooldown_remaining)))
        if (
            email_count >= settings.email_verification_hourly_email_limit
            and email_oldest is not None
        ):
            retry_candidates.append(
                max(1, math.ceil(email_oldest + 3600 - now))
            )
        if (
            ip_count >= settings.email_verification_hourly_ip_limit
            and ip_oldest is not None
        ):
            retry_candidates.append(
                max(1, math.ceil(ip_oldest + 3600 - now))
            )
        if not retry_candidates:
            session.add(PasswordResetRateEventRecord(
                id=uuid.uuid4().hex,
                email_digest=email_digest,
                source_ip_digest=source_ip_digest,
                created_at=now,
            ))
    if retry_candidates:
        raise PasswordResetError(
            "password_reset_rate_limited",
            retry_after=max(retry_candidates),
        )

    request_id = uuid.uuid4().hex
    user_id: str | None = None
    username: str | None = None
    delivery_stage = "persist_pending"
    try:
        # Phase 1: persist a pending token without invalidating the last usable
        # token. SMTP is an external side effect and must not run inside this
        # transaction: a delivery failure should be recorded, not roll the
        # request row out of existence.
        with session_scope() as session:
            user = session.scalar(
                select(UserRecord)
                .where(
                    func.lower(UserRecord.email) == normalized_email,
                    UserRecord.is_active.is_(True),
                )
                .with_for_update()
            )
            if user is None:
                return _neutral_response()
            user_id = user.id
            username = user.username

            row = PasswordResetRequestRecord(
                id=request_id,
                user_id=user.id,
                token_digest=digest_reset_token(raw_token),
                created_at=now,
                expires_at=now + settings.email_verification_expiry_seconds,
                resend_available_at=now + settings.email_verification_resend_seconds,
                delivery_status="pending",
                source_ip=source_ip,
            )
            session.add(row)
            session.flush()

        assert user_id is not None and username is not None
        delivery_stage = "smtp"
        try:
            subject, text_body, html_body = password_reset_message(username, raw_token)
            sender.send(normalized_email, subject, text_body, html_body)
        except Exception:
            logger.warning(
                "Password-reset request suppressed request_id=%s "
                "stage=smtp code=password_reset_unavailable",
                request_id,
            )
            with session_scope() as session:
                failed = session.scalar(
                    select(PasswordResetRequestRecord)
                    .where(PasswordResetRequestRecord.id == request_id)
                    .with_for_update()
                )
                if failed is not None and failed.delivery_status == "pending":
                    failed.delivery_status = "failed"
                    failed.last_delivery_error_code = "password_reset_unavailable"
            return _neutral_response()

        # Phase 2: only after SMTP returns successfully do we activate the new
        # token and supersede older usable tokens in one transaction. If this
        # commit fails, the durable row remains pending and cannot be consumed.
        delivery_stage = "activate"
        _activate_delivered_request(user_id=user_id, request_id=request_id)
    except Exception:
        # The public request contract is deliberately identical for unknown
        # accounts, rate limits, and delivery failures. Log only the exception
        # request id, stable internal stage, and stable error code. Exception
        # classes/messages can contain provider details or recipient data.
        logger.warning(
            "Password-reset request suppressed request_id=%s stage=%s "
            "code=password_reset_unavailable",
            request_id,
            delivery_stage,
        )
        return _neutral_response()
    return _neutral_response()


def confirm_password_reset(token: str, new_password: str) -> dict[str, str]:
    if not token or len(token) > 512:
        raise PasswordResetError("password_reset_link_invalid")
    if not 8 <= len(new_password) <= 128:
        raise PasswordResetError("password_reset_unavailable")
    token_digest = digest_reset_token(token)
    with session_scope() as session:
        user_id = session.scalar(
            select(PasswordResetRequestRecord.user_id).where(
                PasswordResetRequestRecord.token_digest == token_digest
            )
        )
    if user_id is None:
        raise PasswordResetError("password_reset_link_invalid")

    # Every path that verifies credentials, creates/rotates a refresh session,
    # or resets a password takes this process lock and then the same user row
    # lock. That ordering makes reset invalidation atomic on PostgreSQL and on
    # the SQLite development path.
    with user_auth_lock(user_id):
        with session_scope() as session:
            user = session.scalar(
                select(UserRecord).where(UserRecord.id == user_id).with_for_update()
            )
            row = session.scalar(
                select(PasswordResetRequestRecord)
                .where(PasswordResetRequestRecord.token_digest == token_digest)
                .with_for_update()
            )
            now = time.time()
            if row is None:
                raise PasswordResetError("password_reset_link_invalid")
            if row.delivery_status != "sent":
                raise PasswordResetError("password_reset_link_invalid")
            if row.consumed_at is not None or row.superseded_at is not None:
                raise PasswordResetError("password_reset_link_already_used")
            if row.expires_at <= now:
                raise PasswordResetError("password_reset_link_expired")
            if user is None or not user.is_active or row.user_id != user.id:
                raise PasswordResetError("password_reset_unavailable")
            other_active_rows = session.scalars(
                select(PasswordResetRequestRecord)
                .where(
                    PasswordResetRequestRecord.user_id == user.id,
                    PasswordResetRequestRecord.id != row.id,
                    PasswordResetRequestRecord.superseded_at.is_(None),
                    PasswordResetRequestRecord.consumed_at.is_(None),
                    PasswordResetRequestRecord.delivery_status.in_(("pending", "sent")),
                )
                .with_for_update()
            ).all()
            for other in other_active_rows:
                other.superseded_at = now
            user.password_hash = hash_password(new_password)
            user.auth_invalid_before = now
            row.consumed_at = now
            revoke_all_refresh_sessions(session, user.id, now=now)
    return {"status": "password_reset"}
