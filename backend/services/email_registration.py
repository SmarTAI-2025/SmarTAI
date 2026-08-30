from __future__ import annotations

import hashlib
import math
import secrets
import time
import uuid
from contextlib import contextmanager
from threading import Lock
from typing import Iterator

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError

from backend.auth import hash_password
from backend.config import settings
from backend.db.auth_repository import AuthRepositoryError, _ensure_unique_identity, _persist_user
from backend.db.models import EmailVerificationRequestRecord, UserRecord
from backend.db.session import session_scope
from backend.services.email_sender import EmailSender, get_email_sender, verification_message


class RegistrationError(ValueError):
    def __init__(self, code: str, *, status_code: int = 400, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after


_FLOW_LOCKS = tuple(Lock() for _ in range(64))


def _flow_lock(key: str) -> Lock:
    digest_prefix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    return _FLOW_LOCKS[int(digest_prefix, 16) % len(_FLOW_LOCKS)]


def _registration_flow_keys(
    normalized_email: str,
    source_ip: str | None,
    normalized_username: str | None = None,
) -> tuple[str, ...]:
    keys = [f"email:{normalized_email}"]
    if normalized_username is not None:
        keys.append(f"username:{normalized_username}")
    if source_ip is not None:
        keys.append(f"ip:{source_ip}")
    return tuple(keys)


@contextmanager
def _process_flow_locks(
    normalized_email: str,
    source_ip: str | None,
    normalized_username: str | None = None,
) -> Iterator[None]:
    unique_locks = {
        id(lock): lock
        for key in _registration_flow_keys(
            normalized_email,
            source_ip,
            normalized_username,
        )
        for lock in (_flow_lock(key),)
    }
    locks = [unique_locks[key] for key in sorted(unique_locks)]
    for lock in locks:
        lock.acquire()
    try:
        yield
    finally:
        for lock in reversed(locks):
            lock.release()


def _database_flow_lock_id(key: str) -> int:
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big", signed=True)


def _acquire_database_flow_locks(
    session,
    *,
    normalized_email: str,
    source_ip: str | None,
    normalized_username: str | None = None,
) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    lock_ids = sorted({
        _database_flow_lock_id(key)
        for key in _registration_flow_keys(
            normalized_email,
            source_ip,
            normalized_username,
        )
    })
    for lock_id in lock_ids:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": lock_id},
        )


def _normalize_domain(value: str) -> str:
    domain = value.strip().casefold()
    if domain.endswith("."):
        domain = domain[:-1]
    if not domain or domain.startswith(".") or domain.endswith(".") or ".." in domain:
        raise ValueError("invalid email")
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid email") from exc
    if len(domain) > 253:
        raise ValueError("invalid email")
    labels = domain.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or any(not (character.isascii() and (character.isalnum() or character == "-")) for character in label)
        for label in labels
    ):
        raise ValueError("invalid email")
    return domain


def normalize_email(value: str) -> str:
    """Canonicalize a school email while deliberately avoiding mailbox lookups."""
    normalized = value.strip().casefold()
    local, separator, raw_domain = normalized.rpartition("@")
    if (
        not separator
        or not local
        or "@" in local
        or len(local) > 64
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in local)
    ):
        raise ValueError("invalid email")
    domain = _normalize_domain(raw_domain)
    canonical = f"{local}@{domain}"
    if len(canonical) > 320:
        raise ValueError("invalid email")
    return canonical


def email_domain_allowed(email: str, configured_domains: str) -> bool:
    try:
        domain = normalize_email(email).rsplit("@", 1)[1]
    except ValueError:
        return False
    configured = [item.strip() for item in configured_domains.split(",") if item.strip()]
    if any(item == "*" for item in configured):
        return True
    allowed: list[str] = []
    for item in configured:
        try:
            allowed.append(_normalize_domain(item))
        except ValueError:
            continue
    return any(domain == item or domain.endswith(f".{item}") for item in allowed if item)


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def digest_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_identity(username: str, email: str, password: str) -> tuple[str, str]:
    normalized_username = username.strip()
    if not 3 <= len(normalized_username) <= 64:
        raise RegistrationError("registration_unavailable")
    try:
        normalized_email = normalize_email(email)
    except ValueError as exc:
        raise RegistrationError("registration_unavailable") from exc
    if not email_domain_allowed(normalized_email, settings.allowed_email_domains):
        raise RegistrationError("registration_email_domain_not_allowed")
    if not 8 <= len(password) <= 128:
        raise RegistrationError("registration_unavailable")
    return normalized_username, normalized_email


def request_registration(*, username: str, email: str, password: str, source_ip: str | None,
                         sender: EmailSender | None = None) -> dict[str, object]:
    normalized_username, normalized_email = _validate_identity(username, email, password)
    with _process_flow_locks(normalized_email, source_ip, normalized_username):
        return _request_registration_locked(
            normalized_username=normalized_username,
            normalized_email=normalized_email,
            password=password,
            source_ip=source_ip,
            sender=sender,
        )


def _request_registration_locked(
    *,
    normalized_username: str,
    normalized_email: str,
    password: str,
    source_ip: str | None,
    sender: EmailSender | None = None,
) -> dict[str, object]:
    sender = sender or get_email_sender()
    now = time.time()
    raw_token = generate_token()
    request_id = uuid.uuid4().hex
    try:
        subject, text_body, html_body = verification_message(normalized_username, raw_token)
    except Exception as exc:
        raise RegistrationError("registration_email_delivery_failed", status_code=503) from exc
    try:
        with session_scope() as session:
            _acquire_database_flow_locks(
                session,
                normalized_email=normalized_email,
                source_ip=source_ip,
                normalized_username=normalized_username,
            )
            window_start = now - 3600
            email_count = session.scalar(
                select(func.count(EmailVerificationRequestRecord.id)).where(
                    EmailVerificationRequestRecord.normalized_email == normalized_email,
                    EmailVerificationRequestRecord.created_at >= window_start,
                )
            ) or 0
            ip_count = 0
            if source_ip is not None:
                ip_count = session.scalar(
                    select(func.count(EmailVerificationRequestRecord.id)).where(
                        EmailVerificationRequestRecord.source_ip == source_ip,
                        EmailVerificationRequestRecord.created_at >= window_start,
                    )
                ) or 0
            if email_count >= settings.email_verification_hourly_email_limit or ip_count >= settings.email_verification_hourly_ip_limit:
                raise RegistrationError("registration_rate_limited", retry_after=3600)
            active = session.scalars(
                select(EmailVerificationRequestRecord).where(
                    EmailVerificationRequestRecord.normalized_email == normalized_email,
                    EmailVerificationRequestRecord.superseded_at.is_(None),
                    EmailVerificationRequestRecord.verified_at.is_(None),
                    EmailVerificationRequestRecord.delivery_status != "failed",
                )
            ).all()
            cooling_down = [
                previous.resend_available_at
                for previous in active
                if previous.resend_available_at > now
            ]
            if cooling_down:
                retry_after = max(1, math.ceil(max(cooling_down) - now))
                raise RegistrationError("registration_rate_limited", retry_after=retry_after)
            # Account existence is deliberately not checked on this anonymous
            # endpoint. Every valid school address gets the same response and
            # real mail; uniqueness is disclosed only after token ownership is
            # proved by the verify transaction below.
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
    except RegistrationError:
        raise
    # The pending digest has committed before SMTP. This avoids holding a
    # database transaction across network I/O and leaves a recoverable token if
    # SMTP accepted the mail but the final delivery-status update is interrupted.
    try:
        sender.send(normalized_email, subject, text_body, html_body)
    except Exception as exc:
        _record_delivery_failure(
            request_id=request_id,
            normalized_email=normalized_email,
            normalized_username=normalized_username,
            source_ip=source_ip,
        )
        raise RegistrationError("registration_email_delivery_failed", status_code=503) from exc
    try:
        _record_delivery_success(
            request_id=request_id,
            normalized_email=normalized_email,
            normalized_username=normalized_username,
            source_ip=source_ip,
        )
    except RegistrationError:
        raise
    except Exception as exc:
        raise RegistrationError("registration_email_delivery_failed", status_code=503) from exc
    return {
        "status": "verification_required",
        "request_id": request_id,
        "expires_in_seconds": settings.email_verification_expiry_seconds,
        "resend_after_seconds": settings.email_verification_resend_seconds,
    }


def resend_registration(*, request_id: str, source_ip: str | None,
                         sender: EmailSender | None = None) -> dict[str, object]:
    with session_scope() as session:
        identity = session.execute(
            select(
                EmailVerificationRequestRecord.normalized_email,
                EmailVerificationRequestRecord.normalized_username,
            ).where(
                EmailVerificationRequestRecord.id == request_id
            )
        ).one_or_none()
    if identity is None:
        raise RegistrationError("verification_link_invalid")
    normalized_email, normalized_username = identity
    with _process_flow_locks(normalized_email, source_ip, normalized_username):
        return _resend_registration_locked(
            request_id=request_id,
            normalized_email=normalized_email,
            normalized_username=normalized_username,
            source_ip=source_ip,
            sender=sender,
        )


def _resend_registration_locked(
    *,
    request_id: str,
    normalized_email: str,
    normalized_username: str,
    source_ip: str | None,
    sender: EmailSender | None = None,
) -> dict[str, object]:
    sender = sender or get_email_sender()
    now = time.time()
    raw_token = generate_token()
    new_request_id = uuid.uuid4().hex
    with session_scope() as session:
        _acquire_database_flow_locks(
            session,
            normalized_email=normalized_email,
            source_ip=source_ip,
            normalized_username=normalized_username,
        )
        previous = session.scalar(
            select(EmailVerificationRequestRecord)
            .where(EmailVerificationRequestRecord.id == request_id)
            .with_for_update()
        )
        if (
            previous is None
            or previous.verified_at is not None
            or previous.superseded_at is not None
            or previous.delivery_status == "failed"
        ):
            raise RegistrationError("verification_link_invalid")
        if previous.expires_at <= now:
            raise RegistrationError("verification_link_expired")
        if previous.resend_available_at > now:
            retry_after = max(1, math.ceil(previous.resend_available_at - now))
            raise RegistrationError("registration_rate_limited", retry_after=retry_after)
        if not email_domain_allowed(previous.normalized_email, settings.allowed_email_domains):
            raise RegistrationError("registration_email_domain_not_allowed")
        window_start = now - 3600
        email_count = session.scalar(
            select(func.count(EmailVerificationRequestRecord.id)).where(
                EmailVerificationRequestRecord.normalized_email == previous.normalized_email,
                EmailVerificationRequestRecord.created_at >= window_start,
            )
        ) or 0
        ip_count = 0
        if source_ip is not None:
            ip_count = session.scalar(
                select(func.count(EmailVerificationRequestRecord.id)).where(
                    EmailVerificationRequestRecord.source_ip == source_ip,
                    EmailVerificationRequestRecord.created_at >= window_start,
                )
            ) or 0
        if (
            email_count >= settings.email_verification_hourly_email_limit
            or ip_count >= settings.email_verification_hourly_ip_limit
        ):
            raise RegistrationError("registration_rate_limited", retry_after=3600)
        try:
            subject, text_body, html_body = verification_message(previous.normalized_username, raw_token)
        except Exception as exc:
            raise RegistrationError("registration_email_delivery_failed", status_code=503) from exc
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
        previous.resend_available_at = now + settings.email_verification_resend_seconds
        session.add(replacement)
        session.flush()
    try:
        sender.send(normalized_email, subject, text_body, html_body)
    except Exception as exc:
        _record_delivery_failure(
            request_id=new_request_id,
            normalized_email=normalized_email,
            normalized_username=normalized_username,
            source_ip=source_ip,
            retryable_request_id=request_id,
        )
        raise RegistrationError("registration_email_delivery_failed", status_code=503) from exc
    try:
        _record_delivery_success(
            request_id=new_request_id,
            normalized_email=normalized_email,
            normalized_username=normalized_username,
            source_ip=source_ip,
        )
    except RegistrationError:
        raise
    except Exception as exc:
        raise RegistrationError("registration_email_delivery_failed", status_code=503) from exc
    return {
        "status": "verification_required",
        "request_id": new_request_id,
        "expires_in_seconds": settings.email_verification_expiry_seconds,
        "resend_after_seconds": settings.email_verification_resend_seconds,
    }


def verify_registration(token: str, source_ip: str | None = None) -> dict[str, str]:
    if not token or len(token) > 512:
        raise RegistrationError("verification_link_invalid")
    token_digest = digest_token(token)
    with session_scope() as session:
        identity = session.execute(
            select(
                EmailVerificationRequestRecord.normalized_email,
                EmailVerificationRequestRecord.normalized_username,
            ).where(
                EmailVerificationRequestRecord.token_digest == token_digest
            )
        ).one_or_none()
    if identity is None:
        raise RegistrationError("verification_link_invalid")
    normalized_email, normalized_username = identity
    with _process_flow_locks(normalized_email, source_ip, normalized_username):
        return _verify_registration_locked(
            token_digest,
            normalized_email=normalized_email,
            normalized_username=normalized_username,
            source_ip=source_ip,
        )


def _verify_registration_locked(
    token_digest: str,
    *,
    normalized_email: str,
    normalized_username: str,
    source_ip: str | None,
) -> dict[str, str]:
    now = time.time()
    try:
        with session_scope() as session:
            _acquire_database_flow_locks(
                session,
                normalized_email=normalized_email,
                source_ip=source_ip,
                normalized_username=normalized_username,
            )
            row = session.scalar(
                select(EmailVerificationRequestRecord)
                .where(EmailVerificationRequestRecord.token_digest == token_digest)
                .with_for_update()
            )
            if row is None or row.delivery_status == "failed":
                raise RegistrationError("verification_link_invalid")
            if row.verified_at is not None:
                return {"status": "already_verified"}
            if row.superseded_at is not None:
                raise RegistrationError("verification_link_already_used")
            if row.expires_at <= now:
                raise RegistrationError("verification_link_expired")
            if not email_domain_allowed(row.normalized_email, settings.allowed_email_domains):
                raise RegistrationError("registration_email_domain_not_allowed")
            stored_email = func.lower(func.trim(UserRecord.email))
            identity_exists = (
                session.scalar(
                    select(UserRecord.id).where(
                        or_(
                            UserRecord.username == row.normalized_username,
                            stored_email == row.normalized_email,
                            stored_email == f"{row.normalized_email}.",
                        )
                    )
                )
                is not None
            )
            if identity_exists:
                row.superseded_at = now
                row.last_delivery_error_code = "registration_unavailable"
            else:
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
                _supersede_other_flows(session, row=row, now=now, include_username=True)
    except AuthRepositoryError as exc:
        _consume_unavailable_request(token_digest, now=now)
        raise RegistrationError("registration_unavailable", status_code=409) from exc
    except IntegrityError as exc:
        _consume_unavailable_request(token_digest, now=now)
        raise RegistrationError("registration_unavailable", status_code=409) from exc
    if identity_exists:
        raise RegistrationError("registration_unavailable", status_code=409)
    return {"status": "registered"}


def _supersede_other_flows(
    session,
    *,
    row: EmailVerificationRequestRecord,
    now: float,
    include_username: bool = False,
) -> None:
    identity_filter = EmailVerificationRequestRecord.normalized_email == row.normalized_email
    if include_username:
        identity_filter = or_(
            identity_filter,
            EmailVerificationRequestRecord.normalized_username == row.normalized_username,
        )
    others = session.scalars(
        select(EmailVerificationRequestRecord).where(
            EmailVerificationRequestRecord.id != row.id,
            EmailVerificationRequestRecord.verified_at.is_(None),
            EmailVerificationRequestRecord.superseded_at.is_(None),
            identity_filter,
        )
    ).all()
    for other in others:
        other.superseded_at = now


def _record_delivery_failure(
    *,
    request_id: str,
    normalized_email: str,
    normalized_username: str,
    source_ip: str | None,
    retryable_request_id: str | None = None,
) -> None:
    try:
        with session_scope() as session:
            _acquire_database_flow_locks(
                session,
                normalized_email=normalized_email,
                source_ip=source_ip,
                normalized_username=normalized_username,
            )
            row = session.scalar(
                select(EmailVerificationRequestRecord)
                .where(EmailVerificationRequestRecord.id == request_id)
                .with_for_update()
            )
            if row is not None and row.verified_at is None:
                row.delivery_status = "failed"
                row.last_delivery_error_code = "registration_email_delivery_failed"
                row.superseded_at = time.time()
            if retryable_request_id is not None:
                retryable = session.scalar(
                    select(EmailVerificationRequestRecord)
                    .where(EmailVerificationRequestRecord.id == retryable_request_id)
                    .with_for_update()
                )
                if (
                    retryable is not None
                    and retryable.verified_at is None
                    and retryable.superseded_at is None
                ):
                    retryable.resend_available_at = time.time()
    except Exception:
        # Preserve the stable SMTP failure even when best-effort status recovery
        # cannot reach the database. The persisted pending token remains usable
        # if the SMTP server accepted the message before the transport failed.
        return


def _record_delivery_success(
    *,
    request_id: str,
    normalized_email: str,
    normalized_username: str,
    source_ip: str | None,
) -> None:
    now = time.time()
    with session_scope() as session:
        _acquire_database_flow_locks(
            session,
            normalized_email=normalized_email,
            source_ip=source_ip,
            normalized_username=normalized_username,
        )
        row = session.scalar(
            select(EmailVerificationRequestRecord)
            .where(EmailVerificationRequestRecord.id == request_id)
            .with_for_update()
        )
        if row is None:
            raise RegistrationError("registration_email_delivery_failed", status_code=503)
        row.delivery_status = "sent"
        row.last_delivery_error_code = None
        # Delivery completion order is not request order. A newer request that
        # is still pending must never be cancelled by an older, slower SMTP
        # call. The newest successfully delivered row eventually becomes the
        # only active token; if that newer delivery fails, the older sent token
        # remains available for recovery.
        if row.superseded_at is not None or row.verified_at is not None:
            return
        newer = session.scalars(
            select(EmailVerificationRequestRecord).where(
                EmailVerificationRequestRecord.id != row.id,
                EmailVerificationRequestRecord.normalized_email == row.normalized_email,
                EmailVerificationRequestRecord.verified_at.is_(None),
                EmailVerificationRequestRecord.superseded_at.is_(None),
                EmailVerificationRequestRecord.delivery_status != "failed",
                or_(
                    EmailVerificationRequestRecord.created_at > row.created_at,
                    and_(
                        EmailVerificationRequestRecord.created_at == row.created_at,
                        EmailVerificationRequestRecord.id > row.id,
                    ),
                ),
            )
        ).all()
        if any(candidate.delivery_status == "sent" for candidate in newer):
            row.superseded_at = now
            return
        older = session.scalars(
            select(EmailVerificationRequestRecord).where(
                EmailVerificationRequestRecord.id != row.id,
                EmailVerificationRequestRecord.normalized_email == row.normalized_email,
                EmailVerificationRequestRecord.verified_at.is_(None),
                EmailVerificationRequestRecord.superseded_at.is_(None),
                EmailVerificationRequestRecord.delivery_status != "failed",
                or_(
                    EmailVerificationRequestRecord.created_at < row.created_at,
                    and_(
                        EmailVerificationRequestRecord.created_at == row.created_at,
                        EmailVerificationRequestRecord.id < row.id,
                    ),
                ),
            )
        ).all()
        for previous in older:
            previous.superseded_at = now


def _consume_unavailable_request(token_digest: str, *, now: float) -> None:
    try:
        with session_scope() as session:
            row = session.scalar(
                select(EmailVerificationRequestRecord)
                .where(EmailVerificationRequestRecord.token_digest == token_digest)
                .with_for_update()
            )
            if row is not None and row.verified_at is None:
                row.superseded_at = row.superseded_at or now
                row.last_delivery_error_code = "registration_unavailable"
    except Exception:
        return
