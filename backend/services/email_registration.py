from __future__ import annotations

import hashlib
import secrets
import time
import uuid

from sqlalchemy import func, select

from backend.auth import hash_password
from backend.config import settings
from backend.db.auth_repository import _ensure_unique_identity, _persist_user
from backend.db.models import EmailVerificationRequestRecord, UserRecord
from backend.db.session import session_scope
from backend.services.email_sender import EmailDeliveryError, EmailSender, get_email_sender, verification_message


class RegistrationError(ValueError):
    def __init__(self, code: str, *, status_code: int = 400, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after


def normalize_email(value: str) -> str:
    """Normalize a user-entered email without attempting mailbox validation."""
    normalized = value.strip().casefold()
    local, separator, domain = normalized.rpartition("@")
    if not separator or not local or not domain or "@" in local:
        raise ValueError("invalid email")
    domain = domain.rstrip(".")
    if not domain:
        raise ValueError("invalid email")
    return f"{local}@{domain}"


def email_domain_allowed(email: str, configured_domains: str) -> bool:
    try:
        domain = normalize_email(email).rsplit("@", 1)[1]
    except ValueError:
        return False
    allowed = [item.strip().casefold().rstrip(".") for item in configured_domains.split(",") if item.strip()]
    if "*" in allowed:
        return True
    return any(domain == item or domain.endswith(f".{item}") for item in allowed if item)


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def digest_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_identity(username: str, email: str, password: str) -> tuple[str, str]:
    normalized_username = username.strip()
    if not 3 <= len(normalized_username) <= 64:
        raise RegistrationError("registration_unavailable")
    normalized_email = normalize_email(email)
    if not email_domain_allowed(normalized_email, settings.allowed_email_domains):
        raise RegistrationError("registration_email_domain_not_allowed")
    if not 8 <= len(password) <= 128:
        raise RegistrationError("registration_unavailable")
    return normalized_username, normalized_email


def request_registration(*, username: str, email: str, password: str, source_ip: str | None,
                         sender: EmailSender | None = None) -> dict[str, object]:
    normalized_username, normalized_email = _validate_identity(username, email, password)
    sender = sender or get_email_sender()
    now = time.time()
    raw_token = generate_token()
    request_id = uuid.uuid4().hex
    subject, text_body, html_body = verification_message(raw_token)
    try:
        with session_scope() as session:
            window_start = now - 3600
            email_count = session.scalar(
                select(func.count(EmailVerificationRequestRecord.id)).where(
                    EmailVerificationRequestRecord.normalized_email == normalized_email,
                    EmailVerificationRequestRecord.created_at >= window_start,
                )
            ) or 0
            ip_count = session.scalar(
                select(func.count(EmailVerificationRequestRecord.id)).where(
                    EmailVerificationRequestRecord.source_ip == source_ip,
                    EmailVerificationRequestRecord.created_at >= window_start,
                )
            ) or 0
            if email_count >= settings.email_verification_hourly_email_limit or ip_count >= settings.email_verification_hourly_ip_limit:
                raise RegistrationError("registration_rate_limited", retry_after=3600)
            if session.scalar(select(UserRecord).where(UserRecord.username == normalized_username)) is not None:
                raise RegistrationError("registration_unavailable", status_code=409)
            if session.scalar(select(UserRecord).where(UserRecord.email == normalized_email)) is not None:
                raise RegistrationError("registration_unavailable", status_code=409)
            active = session.scalars(
                select(EmailVerificationRequestRecord).where(
                    EmailVerificationRequestRecord.normalized_email == normalized_email,
                    EmailVerificationRequestRecord.superseded_at.is_(None),
                    EmailVerificationRequestRecord.verified_at.is_(None),
                )
            ).all()
            for previous in active:
                previous.superseded_at = now
            row = EmailVerificationRequestRecord(
                id=request_id,
                normalized_username=normalized_username,
                normalized_email=normalized_email,
                password_hash=hash_password(password),
                token_digest=digest_token(raw_token),
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
                raise RegistrationError("registration_email_delivery_failed", status_code=503) from exc
            except Exception as exc:
                raise RegistrationError("registration_email_delivery_failed", status_code=503) from exc
            row.delivery_status = "sent"
    except RegistrationError:
        raise
    return {
        "status": "verification_required",
        "request_id": request_id,
        "expires_in_seconds": settings.email_verification_expiry_seconds,
        "resend_after_seconds": settings.email_verification_resend_seconds,
    }


def resend_registration(*, request_id: str, source_ip: str | None,
                         sender: EmailSender | None = None) -> dict[str, object]:
    sender = sender or get_email_sender()
    now = time.time()
    raw_token = generate_token()
    new_request_id = uuid.uuid4().hex
    with session_scope() as session:
        previous = session.scalar(
            select(EmailVerificationRequestRecord)
            .where(EmailVerificationRequestRecord.id == request_id)
            .with_for_update()
        )
        if previous is None or previous.verified_at is not None or previous.superseded_at is not None:
            raise RegistrationError("verification_link_invalid")
        if previous.expires_at <= now:
            raise RegistrationError("verification_link_expired")
        if previous.resend_available_at > now:
            retry_after = max(1, int(previous.resend_available_at - now))
            raise RegistrationError("registration_rate_limited", retry_after=retry_after)
        if not email_domain_allowed(previous.normalized_email, settings.allowed_email_domains):
            raise RegistrationError("registration_email_domain_not_allowed")
        subject, text_body, html_body = verification_message(raw_token)
        replacement = EmailVerificationRequestRecord(
            id=new_request_id,
            normalized_username=previous.normalized_username,
            normalized_email=previous.normalized_email,
            password_hash=previous.password_hash,
            token_digest=digest_token(raw_token),
            created_at=now,
            expires_at=now + settings.email_verification_expiry_seconds,
            resend_available_at=now + settings.email_verification_resend_seconds,
            delivery_status="pending",
            source_ip=source_ip,
        )
        session.add(replacement)
        session.flush()
        try:
            sender.send(previous.normalized_email, subject, text_body, html_body)
        except Exception as exc:
            raise RegistrationError("registration_email_delivery_failed", status_code=503) from exc
        previous.superseded_at = now
        replacement.delivery_status = "sent"
    return {
        "status": "verification_required",
        "request_id": new_request_id,
        "expires_in_seconds": settings.email_verification_expiry_seconds,
        "resend_after_seconds": settings.email_verification_resend_seconds,
    }


def verify_registration(token: str) -> dict[str, str]:
    if not token or len(token) > 512:
        raise RegistrationError("verification_link_invalid")
    now = time.time()
    with session_scope() as session:
        row = session.scalar(
            select(EmailVerificationRequestRecord)
            .where(EmailVerificationRequestRecord.token_digest == digest_token(token))
            .with_for_update()
        )
        if row is None:
            raise RegistrationError("verification_link_invalid")
        if row.verified_at is not None:
            return {"status": "already_verified"}
        if row.superseded_at is not None:
            raise RegistrationError("verification_link_already_used")
        if row.expires_at <= now:
            raise RegistrationError("verification_link_expired")
        if not email_domain_allowed(row.normalized_email, settings.allowed_email_domains):
            raise RegistrationError("registration_email_domain_not_allowed")
        _ensure_unique_identity(
            session,
            username=row.normalized_username,
            email=row.normalized_email,
        )
        _persist_user(
            session=session,
            username=row.normalized_username,
            email=row.normalized_email,
            password_hash=row.password_hash,
            role="teacher",
            now=now,
        )
        row.verified_at = now
        return {"status": "registered"}
