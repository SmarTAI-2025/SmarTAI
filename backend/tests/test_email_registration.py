from __future__ import annotations

import hashlib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.services.email_registration import (
    digest_token,
    email_domain_allowed,
    generate_token,
    normalize_email,
)
from backend.services.email_sender import verification_message
from backend.db.models import EmailVerificationRequestRecord, UserRecord
from backend.db.session import create_schema, session_scope
from backend.services.email_registration import (
    RegistrationError,
    request_registration,
    resend_registration,
    verify_registration,
)
from fastapi.testclient import TestClient
from backend.main import app


def test_normalize_email_trims_lowercases_and_removes_one_domain_dot():
    assert normalize_email("  Teacher@MAIL.USTC.EDU.CN.  ") == "teacher@mail.ustc.edu.cn"


@pytest.mark.parametrize(
    "email",
    [
        "teacher@.ustc.edu.cn",
        "teacher@dept..ustc.edu.cn",
        "teacher@ustc.edu.cn..",
        "teacher name@ustc.edu.cn",
        ".teacher@ustc.edu.cn",
        "teacher..name@ustc.edu.cn",
    ],
)
def test_normalize_email_rejects_malformed_canonical_forms(email: str):
    with pytest.raises(ValueError):
        normalize_email(email)


@pytest.mark.parametrize(
    "email, allowed",
    [
        ("name@ustc.edu.cn", True),
        ("name@mail.ustc.edu.cn", True),
        ("name@dept.mail.ustc.edu.cn", True),
        ("name@evilustc.edu.cn", False),
        ("name@ustc.edu.cn.example.com", False),
        ("name@gmail.com", False),
    ],
)
def test_email_domain_allowed_uses_dot_boundary(email: str, allowed: bool):
    assert email_domain_allowed(email, "ustc.edu.cn") is allowed


def test_email_domain_allowed_fails_closed_without_domains():
    assert email_domain_allowed("teacher@ustc.edu.cn", "") is False
    assert email_domain_allowed("teacher@gmail.com", "*") is True


def test_email_domain_configuration_is_canonical_and_invalid_entries_fail_closed():
    assert email_domain_allowed("teacher@mail.ustc.edu.cn", " USTC.EDU.CN. ") is True
    assert email_domain_allowed("teacher@ustc.edu.cn", "..,bad_domain") is False


def test_generated_token_is_high_entropy_and_only_digest_is_persisted():
    token = generate_token()
    assert len(token) >= 32
    assert digest_token(token) == hashlib.sha256(token.encode()).hexdigest()
    assert token != digest_token(token)


def test_verification_message_greets_user_and_escapes_html_username():
    subject, text, html = verification_message("Teacher <A>", "token-value")

    assert subject == "确认 SmarTAI 教师账号"
    assert "你好，Teacher <A>" in text
    assert "你好，Teacher &lt;A&gt;" in html


def test_verification_request_persists_digest_and_identity_fields_only():
    create_schema()
    request = EmailVerificationRequestRecord(
        id="req-test-primitive",
        normalized_username="teacher-test",
        normalized_email="teacher@mail.ustc.edu.cn",
        password_hash="$2b$12$hashed-password",
        token_digest=digest_token("raw-token-never-stored"),
        created_at=1,
        expires_at=1801,
        resend_available_at=61,
        delivery_status="sent",
    )
    with session_scope() as session:
        session.add(request)
    with session_scope() as session:
        stored = session.get(EmailVerificationRequestRecord, request.id)
        assert stored is not None
        assert stored.token_digest == digest_token("raw-token-never-stored")
        assert not hasattr(stored, "token")
        assert stored.password_hash != "raw-token-never-stored"


class _FakeSender:
    def __init__(self, *, fail: bool = False):
        self.messages: list[dict[str, str]] = []
        self.fail = fail

    def send(self, to_email: str, subject: str, text_body: str, html_body: str) -> None:
        if self.fail:
            raise RuntimeError("smtp secret must not escape")
        self.messages.append({"to": to_email, "subject": subject, "text": text_body, "html": html_body})


def test_request_and_verify_creates_teacher_only_after_email_confirmation(monkeypatch):
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    result = request_registration(
        username="new-teacher",
        email="Teacher@MAIL.USTC.EDU.CN",
        password="long-enough-password",
        source_ip="127.0.0.1",
        sender=sender,
    )
    assert result["status"] == "verification_required"
    assert sender.messages and "#token=" in sender.messages[0]["text"]
    token = sender.messages[0]["text"].split("#token=", 1)[1].split()[0]
    assert verify_registration(token) == {"status": "registered"}
    assert verify_registration(token) == {"status": "already_verified"}


def test_failed_delivery_records_internal_failure_and_allows_a_clean_retry(monkeypatch):
    sender = _FakeSender(fail=True)
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    with pytest.raises(RegistrationError) as exc_info:
        request_registration(
            username="delivery-failure",
            email="failure@ustc.edu.cn",
            password="long-enough-password",
            source_ip="127.0.0.1",
            sender=sender,
        )
    assert exc_info.value.code == "registration_email_delivery_failed"
    with session_scope() as session:
        stored = session.query(EmailVerificationRequestRecord).filter_by(
            normalized_username="delivery-failure"
        ).one()
        assert stored.delivery_status == "failed"
        assert stored.last_delivery_error_code == "registration_email_delivery_failed"
        assert stored.superseded_at is not None
    retry_sender = _FakeSender()
    result = request_registration(
        username="delivery-failure",
        email="failure@ustc.edu.cn",
        password="long-enough-password",
        source_ip="127.0.0.1",
        sender=retry_sender,
    )
    assert result["status"] == "verification_required"
    assert len(retry_sender.messages) == 1


def _seed_user(*, user_id: str, username: str, email: str) -> None:
    now = time.time()
    with session_scope() as session:
        session.add(
            UserRecord(
                id=user_id,
                username=username,
                email=email,
                role="teacher",
                password_hash="$2b$12$existing-password-hash",
                is_active=True,
                created_at=now,
                updated_at=now,
            )
        )


def test_request_does_not_enumerate_existing_username_before_email_proof(monkeypatch):
    _seed_user(
        user_id="u_existing_username",
        username="already-taken",
        email="owner@ustc.edu.cn",
    )
    sender = _FakeSender()
    monkeypatch.setattr("backend.api.auth.get_email_sender", lambda: sender)
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    client = TestClient(app)

    existing = client.post(
        "/auth/register/request",
        json={
            "username": "already-taken",
            "email": "new-owner@ustc.edu.cn",
            "password": "long-enough-password",
        },
    )
    available = client.post(
        "/auth/register/request",
        json={
            "username": "available-name",
            "email": "available@ustc.edu.cn",
            "password": "long-enough-password",
        },
    )

    assert existing.status_code == available.status_code == 202
    assert set(existing.json()) == set(available.json())
    assert existing.json()["status"] == available.json()["status"] == "verification_required"
    assert existing.json()["expires_in_seconds"] == available.json()["expires_in_seconds"]
    assert existing.json()["resend_after_seconds"] == available.json()["resend_after_seconds"]
    assert len(sender.messages) == 2

    collision_token = sender.messages[0]["text"].split("#token=", 1)[1].split()[0]
    verified = client.post("/auth/register/verify", json={"token": collision_token})
    assert verified.status_code == 409
    assert verified.json() == {"detail": {"code": "registration_unavailable"}}


def test_request_canonicalizes_existing_email_and_only_rejects_after_proof(monkeypatch):
    _seed_user(
        user_id="u_existing_email",
        username="existing-email-owner",
        email="Taken@MAIL.USTC.EDU.CN",
    )
    sender = _FakeSender()
    monkeypatch.setattr("backend.api.auth.get_email_sender", lambda: sender)
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    client = TestClient(app)

    response = client.post(
        "/auth/register/request",
        json={
            "username": "new-name-for-existing-email",
            "email": " taken@mail.ustc.edu.cn. ",
            "password": "long-enough-password",
        },
    )
    assert response.status_code == 202
    assert response.json()["status"] == "verification_required"
    assert sender.messages[0]["to"] == "taken@mail.ustc.edu.cn"

    token = sender.messages[0]["text"].split("#token=", 1)[1].split()[0]
    verified = client.post("/auth/register/verify", json={"token": token})
    assert verified.status_code == 409
    assert verified.json() == {"detail": {"code": "registration_unavailable"}}


def test_verify_detects_a_legacy_email_with_whitespace_and_trailing_dot(monkeypatch):
    _seed_user(
        user_id="u_legacy_email",
        username="legacy-email-owner",
        email=" Legacy@MAIL.USTC.EDU.CN. ",
    )
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")

    request_registration(
        username="new-name-for-legacy-email",
        email="legacy@mail.ustc.edu.cn",
        password="long-enough-password",
        source_ip="198.51.100.63",
        sender=sender,
    )
    token = sender.messages[0]["text"].split("#token=", 1)[1].split()[0]

    with pytest.raises(RegistrationError) as exc_info:
        verify_registration(token)
    assert exc_info.value.code == "registration_unavailable"


def test_pending_delivery_token_recovers_after_sender_commit_uncertainty(monkeypatch):
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    raw_token = "pending-delivery-token"
    now = time.time()
    with session_scope() as session:
        session.add(
            EmailVerificationRequestRecord(
                id="pending-delivery-recovery",
                normalized_username="pending-delivery-user",
                normalized_email="pending-delivery@ustc.edu.cn",
                password_hash="$2b$12$pending-password-hash",
                token_digest=digest_token(raw_token),
                created_at=now,
                expires_at=now + 1800,
                resend_available_at=now + 60,
                delivery_status="pending",
                source_ip="198.51.100.70",
            )
        )

    assert verify_registration(raw_token) == {"status": "registered"}


def test_late_older_delivery_cannot_supersede_a_newer_active_token():
    from backend.services import email_registration

    now = time.time()
    email = "late-delivery@ustc.edu.cn"
    with session_scope() as session:
        session.add_all(
            [
                EmailVerificationRequestRecord(
                    id="older-delivery",
                    normalized_username="late-delivery-user",
                    normalized_email=email,
                    password_hash="$2b$12$older-password-hash",
                    token_digest=digest_token("older-delivery-token"),
                    created_at=now - 70,
                    expires_at=now + 1700,
                    resend_available_at=now - 10,
                    superseded_at=now,
                    delivery_status="pending",
                    source_ip="198.51.100.71",
                ),
                EmailVerificationRequestRecord(
                    id="newer-delivery",
                    normalized_username="late-delivery-user",
                    normalized_email=email,
                    password_hash="$2b$12$newer-password-hash",
                    token_digest=digest_token("newer-delivery-token"),
                    created_at=now,
                    expires_at=now + 1800,
                    resend_available_at=now + 60,
                    delivery_status="sent",
                    source_ip="198.51.100.72",
                ),
            ]
        )

    email_registration._record_delivery_success(
        request_id="older-delivery",
        normalized_email=email,
        normalized_username="late-delivery-user",
        source_ip="198.51.100.71",
    )

    with session_scope() as session:
        older = session.get(EmailVerificationRequestRecord, "older-delivery")
        newer = session.get(EmailVerificationRequestRecord, "newer-delivery")
        assert older is not None and older.delivery_status == "sent"
        assert older.superseded_at is not None
        assert newer is not None and newer.superseded_at is None


def test_older_delivery_finishing_first_preserves_newer_pending_winner():
    from backend.services import email_registration

    now = time.time()
    email = "delivery-order@ustc.edu.cn"
    with session_scope() as session:
        session.add_all(
            [
                EmailVerificationRequestRecord(
                    id="delivery-order-older",
                    normalized_username="delivery-order-user",
                    normalized_email=email,
                    password_hash="$2b$12$older-password-hash",
                    token_digest=digest_token("delivery-order-older-token"),
                    created_at=now - 70,
                    expires_at=now + 1700,
                    resend_available_at=now - 10,
                    delivery_status="pending",
                    source_ip="198.51.100.73",
                ),
                EmailVerificationRequestRecord(
                    id="delivery-order-newer",
                    normalized_username="delivery-order-user",
                    normalized_email=email,
                    password_hash="$2b$12$newer-password-hash",
                    token_digest=digest_token("delivery-order-newer-token"),
                    created_at=now,
                    expires_at=now + 1800,
                    resend_available_at=now + 60,
                    delivery_status="pending",
                    source_ip="198.51.100.74",
                ),
            ]
        )

    email_registration._record_delivery_success(
        request_id="delivery-order-older",
        normalized_email=email,
        normalized_username="delivery-order-user",
        source_ip="198.51.100.73",
    )
    with session_scope() as session:
        older = session.get(EmailVerificationRequestRecord, "delivery-order-older")
        newer = session.get(EmailVerificationRequestRecord, "delivery-order-newer")
        assert older is not None and older.delivery_status == "sent"
        assert older.superseded_at is None
        assert newer is not None and newer.delivery_status == "pending"
        assert newer.superseded_at is None

    email_registration._record_delivery_success(
        request_id="delivery-order-newer",
        normalized_email=email,
        normalized_username="delivery-order-user",
        source_ip="198.51.100.74",
    )
    with session_scope() as session:
        older = session.get(EmailVerificationRequestRecord, "delivery-order-older")
        newer = session.get(EmailVerificationRequestRecord, "delivery-order-newer")
        assert older is not None and older.superseded_at is not None
        assert newer is not None and newer.delivery_status == "sent"
        assert newer.superseded_at is None


def test_middle_delivery_supersedes_older_while_newer_pending_may_fail():
    from backend.services import email_registration

    now = time.time()
    email = "three-deliveries@ustc.edu.cn"
    with session_scope() as session:
        for offset, request_id, status in (
            (-120, "three-older", "sent"),
            (-60, "three-middle", "pending"),
            (0, "three-newer", "pending"),
        ):
            session.add(EmailVerificationRequestRecord(
                id=request_id,
                normalized_username="three-deliveries-user",
                normalized_email=email,
                password_hash="$2b$12$three-password-hash",
                token_digest=digest_token(f"{request_id}-token"),
                created_at=now + offset,
                expires_at=now + 1800,
                resend_available_at=now + 60,
                delivery_status=status,
                source_ip="198.51.100.75",
            ))

    email_registration._record_delivery_success(
        request_id="three-middle",
        normalized_email=email,
        normalized_username="three-deliveries-user",
        source_ip="198.51.100.75",
    )
    email_registration._record_delivery_failure(
        request_id="three-newer",
        normalized_email=email,
        normalized_username="three-deliveries-user",
        source_ip="198.51.100.75",
    )

    with session_scope() as session:
        older = session.get(EmailVerificationRequestRecord, "three-older")
        middle = session.get(EmailVerificationRequestRecord, "three-middle")
        newer = session.get(EmailVerificationRequestRecord, "three-newer")
        assert older is not None and older.superseded_at is not None
        assert middle is not None and middle.delivery_status == "sent"
        assert middle.superseded_at is None
        assert newer is not None and newer.delivery_status == "failed"
        assert newer.superseded_at is not None


def test_new_request_supersedes_previous_token(monkeypatch):
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    request_registration(
        username="first-teacher",
        email="supersede@ustc.edu.cn",
        password="long-enough-password",
        source_ip="127.0.0.1",
        sender=sender,
    )
    first_token = sender.messages[-1]["text"].split("#token=", 1)[1].split()[0]
    with session_scope() as session:
        first_row = session.query(EmailVerificationRequestRecord).filter_by(
            normalized_email="supersede@ustc.edu.cn",
        ).one()
        first_row.resend_available_at = 0
    second = request_registration(
        username="second-teacher",
        email="supersede@ustc.edu.cn",
        password="long-enough-password",
        source_ip="127.0.0.1",
        sender=sender,
    )
    assert second["request_id"]
    with pytest.raises(RegistrationError) as exc_info:
        verify_registration(first_token)
    assert exc_info.value.code == "verification_link_already_used"


def test_resend_replaces_token_and_preserves_pending_password(monkeypatch):
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    first = request_registration(
        username="resend-teacher",
        email="resend@ustc.edu.cn",
        password="long-enough-password",
        source_ip="127.0.0.1",
        sender=sender,
    )
    with session_scope() as session:
        row = session.get(EmailVerificationRequestRecord, first["request_id"])
        assert row is not None
        row.resend_available_at = 0
    second = resend_registration(request_id=first["request_id"], source_ip="127.0.0.1", sender=sender)
    assert second["request_id"] != first["request_id"]
    old_token = sender.messages[-2]["text"].split("#token=", 1)[1].split()[0]
    new_token = sender.messages[-1]["text"].split("#token=", 1)[1].split()[0]
    with pytest.raises(RegistrationError):
        verify_registration(old_token)
    assert verify_registration(new_token) == {"status": "registered"}


def test_concurrent_registration_double_click_sends_once(monkeypatch):
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")

    def request_once():
        try:
            return request_registration(
                username="double-click",
                email="double-click@ustc.edu.cn",
                password="long-enough-password",
                source_ip="198.51.100.50",
                sender=sender,
            )
        except RegistrationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: request_once(), range(2)))

    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert sum(outcome == "registration_rate_limited" for outcome in outcomes) == 1
    assert len(sender.messages) == 1


def test_concurrent_verify_creates_one_user(monkeypatch):
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    request_registration(
        username="double-confirm",
        email="double-confirm@ustc.edu.cn",
        password="long-enough-password",
        source_ip="198.51.100.51",
        sender=sender,
    )
    token = sender.messages[-1]["text"].split("#token=", 1)[1].split()[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: verify_registration(token), range(2)))

    assert sorted(outcome["status"] for outcome in outcomes) == ["already_verified", "registered"]
    with session_scope() as session:
        assert session.query(UserRecord).filter_by(username="double-confirm").count() == 1


def test_concurrent_verify_for_same_username_across_emails_is_stable(monkeypatch):
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    request_registration(
        username="shared-username",
        email="first-owner@ustc.edu.cn",
        password="long-enough-password",
        source_ip="198.51.100.61",
        sender=sender,
    )
    request_registration(
        username="shared-username",
        email="second-owner@ustc.edu.cn",
        password="long-enough-password",
        source_ip="198.51.100.62",
        sender=sender,
    )
    tokens = [message["text"].split("#token=", 1)[1].split()[0] for message in sender.messages]

    def verify_once(token: str):
        try:
            return verify_registration(token)
        except RegistrationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(verify_once, tokens))

    assert sum(outcome == {"status": "registered"} for outcome in outcomes) == 1
    assert sum(
        outcome in {"registration_unavailable", "verification_link_already_used"}
        for outcome in outcomes
        if isinstance(outcome, str)
    ) == 1
    with session_scope() as session:
        assert session.query(UserRecord).filter_by(username="shared-username").count() == 1


def test_registration_api_requires_email_verification_and_never_returns_session(monkeypatch):
    sender = _FakeSender()
    monkeypatch.setattr("backend.api.auth.get_email_sender", lambda: sender)
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    client = TestClient(app)
    response = client.post(
        "/auth/register/request",
        json={"username": "api-teacher", "email": "api@ustc.edu.cn", "password": "long-enough-password"},
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "verification_required"
    assert "token" not in response.json()
    assert client.cookies.get("smartai_refresh") is None
    token = sender.messages[-1]["text"].split("#token=", 1)[1].split()[0]
    verify = client.post("/auth/register/verify", json={"token": token})
    assert verify.status_code == 200, verify.text
    assert verify.json() == {"status": "registered"}
    login = client.post("/auth/login", json={"username": "api-teacher", "password": "long-enough-password"})
    assert login.status_code == 200, login.text


def test_registration_api_rejects_role_and_invite_fields(monkeypatch):
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    response = TestClient(app).post(
        "/auth/register/request",
        json={
            "username": "api-extra-fields",
            "email": "api-extra@ustc.edu.cn",
            "password": "long-enough-password",
            "role": "admin",
            "invite_code": "BYPASS",
        },
    )
    assert response.status_code == 422


def test_registration_api_projects_malformed_email_as_stable_unavailable(monkeypatch):
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    response = TestClient(app, raise_server_exceptions=False).post(
        "/auth/register/request",
        json={
            "username": "malformed-email",
            "email": "teacher@.ustc.edu.cn",
            "password": "long-enough-password",
        },
    )

    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "registration_unavailable"}}


def test_resend_api_returns_retry_after_header(monkeypatch):
    def rate_limited(**_kwargs):
        raise RegistrationError("registration_rate_limited", retry_after=3600)

    monkeypatch.setattr("backend.api.auth.resend_registration", rate_limited)
    response = TestClient(app).post(
        "/auth/register/resend",
        json={"request_id": "opaque-request-id"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "registration_rate_limited"}}
    assert response.headers["Retry-After"] == "3600"


def test_legacy_register_route_is_not_mounted():
    client = TestClient(app)
    response = client.post(
        "/auth/register",
        json={"username": "legacy-bypass", "email": "legacy@gmail.com", "password": "long-enough-password"},
    )
    assert response.status_code == 404
    assert all(getattr(route, "path", None) != "/auth/register" for route in app.routes)


def test_resend_enforces_hourly_email_limit(monkeypatch):
    sender = _FakeSender()
    now = time.time()
    email = "resend-limit@ustc.edu.cn"
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    monkeypatch.setattr("backend.services.email_registration.settings.email_verification_hourly_email_limit", 10)
    monkeypatch.setattr("backend.services.email_registration.settings.email_verification_hourly_ip_limit", 20)

    with session_scope() as session:
        for index in range(10):
            session.add(
                EmailVerificationRequestRecord(
                    id=f"resend-limit-{index}",
                    normalized_username="resend-limit",
                    normalized_email=email,
                    password_hash="$2b$12$hashed-password",
                    token_digest=digest_token(f"resend-limit-token-{uuid.uuid4().hex}"),
                    created_at=now - index,
                    expires_at=now + 1800,
                    resend_available_at=0,
                    superseded_at=now if index else None,
                    delivery_status="sent",
                    source_ip=f"198.51.100.{index + 1}",
                )
            )

    with pytest.raises(RegistrationError) as exc_info:
        resend_registration(
            request_id="resend-limit-0",
            source_ip="203.0.113.10",
            sender=sender,
        )

    assert exc_info.value.code == "registration_rate_limited"
    assert exc_info.value.retry_after is not None
    assert sender.messages == []


def test_missing_source_ip_is_not_counted_as_one_shared_ip(monkeypatch):
    sender = _FakeSender()
    now = time.time()
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    monkeypatch.setattr("backend.services.email_registration.settings.email_verification_hourly_email_limit", 10)
    monkeypatch.setattr("backend.services.email_registration.settings.email_verification_hourly_ip_limit", 20)
    with session_scope() as session:
        for index in range(20):
            session.add(
                EmailVerificationRequestRecord(
                    id=f"no-ip-{index}",
                    normalized_username=f"no-ip-{index}",
                    normalized_email=f"no-ip-{index}@ustc.edu.cn",
                    password_hash="$2b$12$hashed-password",
                    token_digest=digest_token(f"no-ip-token-{index}"),
                    created_at=now - index,
                    expires_at=now + 1800,
                    resend_available_at=0,
                    superseded_at=now,
                    delivery_status="sent",
                    source_ip=None,
                )
            )

    result = request_registration(
        username="no-ip-new",
        email="no-ip-new@ustc.edu.cn",
        password="long-enough-password",
        source_ip=None,
        sender=sender,
    )
    assert result["status"] == "verification_required"
    assert len(sender.messages) == 1


def test_resend_enforces_hourly_ip_limit(monkeypatch):
    sender = _FakeSender()
    now = time.time()
    source_ip = "203.0.113.42"
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    monkeypatch.setattr("backend.services.email_registration.settings.email_verification_hourly_email_limit", 10)
    monkeypatch.setattr("backend.services.email_registration.settings.email_verification_hourly_ip_limit", 20)
    with session_scope() as session:
        for index in range(20):
            session.add(
                EmailVerificationRequestRecord(
                    id=f"ip-limit-{index}",
                    normalized_username=f"ip-limit-{index}",
                    normalized_email=f"ip-limit-{index}@ustc.edu.cn",
                    password_hash="$2b$12$hashed-password",
                    token_digest=digest_token(f"ip-limit-token-{index}"),
                    created_at=now - index,
                    expires_at=now + 1800,
                    resend_available_at=0,
                    superseded_at=now if index else None,
                    delivery_status="sent",
                    source_ip=source_ip,
                )
            )

    with pytest.raises(RegistrationError) as exc_info:
        resend_registration(request_id="ip-limit-0", source_ip=source_ip, sender=sender)
    assert exc_info.value.code == "registration_rate_limited"
    assert exc_info.value.retry_after == 3600
    assert sender.messages == []


def test_failed_resend_keeps_original_request_retryable(monkeypatch):
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    first = request_registration(
        username="retry-resend",
        email="retry-resend@ustc.edu.cn",
        password="long-enough-password",
        source_ip="198.51.100.31",
        sender=sender,
    )
    with session_scope() as session:
        row = session.get(EmailVerificationRequestRecord, first["request_id"])
        assert row is not None
        row.resend_available_at = 0

    with pytest.raises(RegistrationError) as exc_info:
        resend_registration(
            request_id=first["request_id"],
            source_ip="198.51.100.31",
            sender=_FakeSender(fail=True),
        )
    assert exc_info.value.code == "registration_email_delivery_failed"

    retry = resend_registration(
        request_id=first["request_id"],
        source_ip="198.51.100.31",
        sender=sender,
    )
    assert retry["request_id"] != first["request_id"]


def test_concurrent_resend_only_one_sender_call_succeeds(monkeypatch):
    class BlockingSender(_FakeSender):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def send(self, to_email: str, subject: str, text_body: str, html_body: str) -> None:
            self.calls += 1
            super().send(to_email, subject, text_body, html_body)

    sender = BlockingSender()
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    first = request_registration(
        username="concurrent-resend",
        email="concurrent-resend@ustc.edu.cn",
        password="long-enough-password",
        source_ip="198.51.100.22",
        sender=sender,
    )
    with session_scope() as session:
        row = session.get(EmailVerificationRequestRecord, first["request_id"])
        assert row is not None
        row.resend_available_at = 0

    def resend_once():
        try:
            return resend_registration(
                request_id=first["request_id"],
                source_ip="198.51.100.22",
                sender=sender,
            )
        except RegistrationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: resend_once(), range(2)))

    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert sum(outcome == "verification_link_invalid" for outcome in outcomes) == 1
    assert sender.calls == 2


def test_request_and_resend_for_same_email_share_one_flow_lock(monkeypatch):
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    first = request_registration(
        username="shared-flow",
        email="shared-flow@ustc.edu.cn",
        password="long-enough-password",
        source_ip="198.51.100.80",
        sender=sender,
    )
    with session_scope() as session:
        row = session.get(EmailVerificationRequestRecord, first["request_id"])
        assert row is not None
        row.resend_available_at = 0

    entered = threading.Event()
    release = threading.Event()

    class FirstDeliveryBlocks(_FakeSender):
        def send(self, to_email: str, subject: str, text_body: str, html_body: str) -> None:
            entered.set()
            assert release.wait(timeout=5)
            super().send(to_email, subject, text_body, html_body)

    blocking_sender = FirstDeliveryBlocks()

    def new_request():
        try:
            return request_registration(
                username="shared-flow-new",
                email="shared-flow@ustc.edu.cn",
                password="long-enough-password",
                source_ip="198.51.100.80",
                sender=blocking_sender,
            )
        except RegistrationError as exc:
            return exc.code

    def resend():
        try:
            return resend_registration(
                request_id=first["request_id"],
                source_ip="198.51.100.80",
                sender=blocking_sender,
            )
        except RegistrationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(new_request)
        assert entered.wait(timeout=5)
        second_future = pool.submit(resend)
        release.set()
        outcomes = [first_future.result(), second_future.result()]

    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert sum(
        isinstance(outcome, str)
        and outcome in {"registration_rate_limited", "verification_link_invalid"}
        for outcome in outcomes
    ) == 1
    assert len(blocking_sender.messages) == 1


def test_verify_waits_for_same_email_request_before_creating_user(monkeypatch):
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    first = request_registration(
        username="verify-race",
        email="verify-race@ustc.edu.cn",
        password="long-enough-password",
        source_ip="198.51.100.81",
        sender=sender,
    )
    old_token = sender.messages[-1]["text"].split("#token=", 1)[1].split()[0]
    with session_scope() as session:
        row = session.get(EmailVerificationRequestRecord, first["request_id"])
        assert row is not None
        row.resend_available_at = 0

    entered = threading.Event()
    release = threading.Event()

    class BlockingSender(_FakeSender):
        def send(self, to_email: str, subject: str, text_body: str, html_body: str) -> None:
            entered.set()
            assert release.wait(timeout=5)
            super().send(to_email, subject, text_body, html_body)

    blocking_sender = BlockingSender()

    def new_request():
        return request_registration(
            username="verify-race-new",
            email="verify-race@ustc.edu.cn",
            password="long-enough-password",
            source_ip="198.51.100.81",
            sender=blocking_sender,
        )

    def verify_old():
        try:
            return verify_registration(old_token, source_ip="198.51.100.81")
        except RegistrationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        request_future = pool.submit(new_request)
        assert entered.wait(timeout=5)
        verify_future = pool.submit(verify_old)
        time.sleep(0.2)
        assert not verify_future.done()
        release.set()
        request_result = request_future.result()
        verify_result = verify_future.result()

    assert request_result["status"] == "verification_required"
    assert verify_result == "verification_link_already_used"
    with session_scope() as session:
        assert session.query(UserRecord).filter_by(username="verify-race").count() == 0


def test_resend_uses_same_email_flow_key_as_request(monkeypatch):
    from backend.services import email_registration

    sender = _FakeSender()
    email = "same-key@ustc.edu.cn"
    monkeypatch.setattr(email_registration.settings, "allowed_email_domains", "ustc.edu.cn")
    first = request_registration(
        username="same-key",
        email=email,
        password="long-enough-password",
        source_ip=None,
        sender=sender,
    )
    with session_scope() as session:
        row = session.get(EmailVerificationRequestRecord, first["request_id"])
        assert row is not None
        row.resend_available_at = 0

    observed_keys: list[str] = []
    original_flow_lock = email_registration._flow_lock

    def recording_flow_lock(key: str):
        observed_keys.append(key)
        return original_flow_lock(key)

    monkeypatch.setattr(email_registration, "_flow_lock", recording_flow_lock)
    resend_registration(request_id=first["request_id"], source_ip=None, sender=sender)

    assert f"email:{email}" in observed_keys


def test_concurrent_requests_cannot_exceed_hourly_ip_limit(monkeypatch):
    sender = _FakeSender()
    now = time.time()
    source_ip = "203.0.113.90"
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    monkeypatch.setattr("backend.services.email_registration.settings.email_verification_hourly_email_limit", 10)
    monkeypatch.setattr("backend.services.email_registration.settings.email_verification_hourly_ip_limit", 20)
    with session_scope() as session:
        for index in range(19):
            session.add(
                EmailVerificationRequestRecord(
                    id=f"concurrent-ip-{index}",
                    normalized_username=f"concurrent-ip-{index}",
                    normalized_email=f"concurrent-ip-{index}@ustc.edu.cn",
                    password_hash="$2b$12$hashed-password",
                    token_digest=digest_token(f"concurrent-ip-token-{index}"),
                    created_at=now - index,
                    expires_at=now + 1800,
                    resend_available_at=0,
                    superseded_at=now,
                    delivery_status="sent",
                    source_ip=source_ip,
                )
            )

    def request(index: int):
        try:
            return request_registration(
                username=f"concurrent-ip-new-{index}",
                email=f"concurrent-ip-new-{index}@ustc.edu.cn",
                password="long-enough-password",
                source_ip=source_ip,
                sender=sender,
            )
        except RegistrationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(request, range(2)))

    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert sum(outcome == "registration_rate_limited" for outcome in outcomes) == 1
    assert len(sender.messages) == 1


def test_postgres_registration_flow_uses_transaction_advisory_locks():
    from backend.services import email_registration

    executed: list[tuple[str, dict[str, int]]] = []

    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeSession:
        def get_bind(self):
            return FakeBind()

        def execute(self, statement, params):
            executed.append((str(statement), params))

    acquire = getattr(email_registration, "_acquire_database_flow_locks", None)
    assert callable(acquire), "registration must define PostgreSQL transaction-level flow locking"
    acquire(
        FakeSession(),
        normalized_email="worker-safe@ustc.edu.cn",
        source_ip="203.0.113.91",
    )

    assert len(executed) == 2
    assert all("pg_advisory_xact_lock" in sql for sql, _ in executed)
    assert len({params["lock_id"] for _, params in executed}) == 2


def test_postgres_registration_flow_also_locks_username_identity():
    from backend.services import email_registration

    executed: list[tuple[str, dict[str, int]]] = []

    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeSession:
        def get_bind(self):
            return FakeBind()

        def execute(self, statement, params):
            executed.append((str(statement), params))

    email_registration._acquire_database_flow_locks(
        FakeSession(),
        normalized_email="worker-safe@ustc.edu.cn",
        normalized_username="worker-safe",
        source_ip="203.0.113.91",
    )

    assert len(executed) == 3
    assert len({params["lock_id"] for _, params in executed}) == 3


def test_resend_cooldown_retry_after_rounds_up(monkeypatch):
    fixed_now = 2_000_000_000.0
    monkeypatch.setattr("backend.services.email_registration.time.time", lambda: fixed_now)
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    with session_scope() as session:
        session.add(
            EmailVerificationRequestRecord(
                id="ceil-cooldown",
                normalized_username="ceil-cooldown",
                normalized_email="ceil-cooldown@ustc.edu.cn",
                password_hash="$2b$12$hashed-password",
                token_digest=digest_token("ceil-cooldown-token"),
                created_at=fixed_now - 10,
                expires_at=fixed_now + 1800,
                resend_available_at=fixed_now + 1.01,
                delivery_status="sent",
                source_ip="198.51.100.81",
            )
        )

    with pytest.raises(RegistrationError) as exc_info:
        resend_registration(
            request_id="ceil-cooldown",
            source_ip="198.51.100.81",
            sender=_FakeSender(),
        )

    assert exc_info.value.code == "registration_rate_limited"
    assert exc_info.value.retry_after == 2


def test_registration_request_api_maps_message_construction_failure(monkeypatch):
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    monkeypatch.setattr("backend.services.email_sender.settings.runtime_environment", "production")
    monkeypatch.setattr("backend.services.email_sender.settings.public_frontend_url", "http://smartai.example.com")
    monkeypatch.setattr("backend.api.auth.get_email_sender", lambda: _FakeSender())

    response = TestClient(app, raise_server_exceptions=False).post(
        "/auth/register/request",
        json={
            "username": "bad-origin-request",
            "email": "bad-origin-request@ustc.edu.cn",
            "password": "long-enough-password",
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "registration_email_delivery_failed"}}


def test_registration_resend_api_maps_message_construction_failure(monkeypatch):
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.email_registration.settings.allowed_email_domains", "ustc.edu.cn")
    first = request_registration(
        username="bad-origin-resend",
        email="bad-origin-resend@ustc.edu.cn",
        password="long-enough-password",
        source_ip="198.51.100.82",
        sender=sender,
    )
    with session_scope() as session:
        row = session.get(EmailVerificationRequestRecord, first["request_id"])
        assert row is not None
        row.resend_available_at = 0
    monkeypatch.setattr("backend.services.email_sender.settings.runtime_environment", "production")
    monkeypatch.setattr("backend.services.email_sender.settings.public_frontend_url", "http://smartai.example.com")
    monkeypatch.setattr("backend.api.auth.get_email_sender", lambda: sender)

    response = TestClient(app, raise_server_exceptions=False).post(
        "/auth/register/resend",
        json={"request_id": first["request_id"]},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "registration_email_delivery_failed"}}
