from __future__ import annotations

import hashlib
import secrets
import time
import uuid

from sqlalchemy import func, select

from backend.auth import hash_password
from backend.config import settings
from backend.db.auth_repository import revoke_all_refresh_sessions
from backend.db.models import PasswordResetRequestRecord, UserRecord
from backend.db.session import session_scope
from backend.services.email_sender import EmailDeliveryError, EmailSender, get_email_sender, password_reset_message
from backend.services.email_registration import email_domain_allowed, normalize_email


class PasswordResetError(ValueError):
    def __init__(self, code: str, *, status_code: int = 400, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after


def generate_reset_token() -> str:
    return secrets.token_urlsafe(96)


def digest_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _neutral_response() -> dict[str, int | str]:
    return {
        "status": "reset_link_requested",
        "expires_in_seconds": settings.email_verification_expiry_seconds,
        "resend_after_seconds": settings.email_verification_resend_seconds,
    }


def request_password_reset(*, email: str, source_ip: str | None, sender: EmailSender | None = None) -> dict[str, int | str]:
    """Request a reset without disclosing whether an account is present."""
    try:
        normalized_email = normalize_email(email)
    except ValueError:
        return _neutral_response()
    if not email_domain_allowed(normalized_email, settings.allowed_email_domains):
        return _neutral_response()

    sender = sender or get_email_sender()
    now = time.time()
    raw_token = generate_reset_token()
    try:
        with session_scope() as session:
            user = session.scalar(
                select(UserRecord)
                .where(UserRecord.email == normalized_email, UserRecord.is_active.is_(True))
                .with_for_update()
            )
            if user is None:
                return _neutral_response()

            subject, text_body, html_body = password_reset_message(user.username, raw_token)

            window_start = now - 3600
            email_count = session.scalar(
                select(func.count(PasswordResetRequestRecord.id)).where(
                    PasswordResetRequestRecord.user_id == user.id,
                    PasswordResetRequestRecord.created_at >= window_start,
                )
            ) or 0
            ip_count = session.scalar(
                select(func.count(PasswordResetRequestRecord.id)).where(
                    PasswordResetRequestRecord.source_ip == source_ip,
                    PasswordResetRequestRecord.created_at >= window_start,
                )
            ) or 0
            if email_count >= settings.email_verification_hourly_email_limit or ip_count >= settings.email_verification_hourly_ip_limit:
                retry_after = settings.email_verification_resend_seconds
                raise PasswordResetError("password_reset_rate_limited", retry_after=retry_after)

            active = session.scalar(
                select(PasswordResetRequestRecord)
                .where(
                    PasswordResetRequestRecord.user_id == user.id,
                    PasswordResetRequestRecord.superseded_at.is_(None),
                    PasswordResetRequestRecord.consumed_at.is_(None),
                )
                .order_by(PasswordResetRequestRecord.created_at.desc())
                .with_for_update()
            )
            if active is not None and active.resend_available_at > now:
                raise PasswordResetError(
                    "password_reset_rate_limited",
                    retry_after=max(1, int(active.resend_available_at - now)),
                )
            if active is not None:
                active.superseded_at = now

            row = PasswordResetRequestRecord(
                id=uuid.uuid4().hex,
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
            try:
                sender.send(normalized_email, subject, text_body, html_body)
            except EmailDeliveryError as exc:
                raise PasswordResetError("password_reset_unavailable", status_code=503) from exc
            except Exception as exc:
                raise PasswordResetError("password_reset_unavailable", status_code=503) from exc
            row.delivery_status = "sent"
    except PasswordResetError:
        raise
    return _neutral_response()


def confirm_password_reset(token: str, new_password: str) -> dict[str, str]:
    if not token or len(token) > 512:
        raise PasswordResetError("password_reset_link_invalid")
    if not 8 <= len(new_password) <= 128:
        raise PasswordResetError("password_reset_unavailable")
    now = time.time()
    with session_scope() as session:
        row = session.scalar(
            select(PasswordResetRequestRecord)
            .where(PasswordResetRequestRecord.token_digest == digest_reset_token(token))
            .with_for_update()
        )
        if row is None:
            raise PasswordResetError("password_reset_link_invalid")
        if row.consumed_at is not None or row.superseded_at is not None:
            raise PasswordResetError("password_reset_link_already_used")
        if row.expires_at <= now:
            raise PasswordResetError("password_reset_link_expired")
        user = session.scalar(
            select(UserRecord).where(UserRecord.id == row.user_id).with_for_update()
        )
        if user is None or not user.is_active:
            raise PasswordResetError("password_reset_unavailable")
        user.password_hash = hash_password(new_password)
        user.auth_invalid_before = now
        row.consumed_at = now
        revoke_all_refresh_sessions(session, user.id, now=now)
    return {"status": "password_reset"}
