from __future__ import annotations

import hashlib

import pytest

from backend.services.email_registration import (
    digest_token,
    email_domain_allowed,
    generate_token,
    normalize_email,
)
from backend.db.models import EmailVerificationRequestRecord
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


def test_generated_token_is_high_entropy_and_only_digest_is_persisted():
    token = generate_token()
    assert len(token) >= 32
    assert digest_token(token) == hashlib.sha256(token.encode()).hexdigest()
    assert token != digest_token(token)


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


def test_failed_delivery_rolls_back_pending_request(monkeypatch):
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
        assert session.query(EmailVerificationRequestRecord).filter_by(
            normalized_username="delivery-failure"
        ).count() == 0


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


def test_legacy_register_cannot_bypass_email_verification(monkeypatch):
    monkeypatch.setattr("backend.config.settings.registration_closed", False)
    response = TestClient(app).post(
        "/auth/register",
        json={"username": "legacy-bypass", "email": "legacy@gmail.com", "password": "long-enough-password"},
    )
    assert response.status_code in {403, 410}
