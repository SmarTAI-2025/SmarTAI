from __future__ import annotations

import hashlib
import time

import pytest

from backend.auth import hash_password, verify_password
from backend.auth import create_token
from backend.db.auth_repository import create_refresh_session
from backend.db.models import PasswordResetRequestRecord, RefreshSessionRecord, UserRecord
from backend.db.session import session_scope
from backend.main import app
from backend.services.password_reset import (
    PasswordResetError,
    confirm_password_reset,
    digest_reset_token,
    generate_reset_token,
    request_password_reset,
)
from backend.services.email_sender import password_reset_message
from fastapi.testclient import TestClient


class _FakeSender:
    def __init__(self, *, fail: bool = False):
        self.messages: list[dict[str, str]] = []
        self.fail = fail

    def send(self, to_email: str, subject: str, text_body: str, html_body: str) -> None:
        if self.fail:
            raise RuntimeError("smtp secret must not escape")
        self.messages.append({"to": to_email, "subject": subject, "text": text_body, "html": html_body})


def _create_user(user_id: str = "reset-user", email: str = "teacher@ustc.edu.cn") -> None:
    with session_scope() as session:
        session.add(
            UserRecord(
                id=user_id,
                username=user_id,
                email=email,
                role="teacher",
                password_hash=hash_password("old-password-123"),
                is_active=True,
            )
        )


def test_reset_request_is_neutral_for_unknown_email(monkeypatch):
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.password_reset.settings.allowed_email_domains", "ustc.edu.cn")

    result = request_password_reset(
        email="missing@ustc.edu.cn", source_ip="127.0.0.1", sender=sender
    )

    assert result == {
        "status": "reset_link_requested",
        "expires_in_seconds": 1800,
        "resend_after_seconds": 60,
    }
    assert sender.messages == []


def test_password_reset_message_greets_username_and_escapes_html():
    subject, text, html = password_reset_message("Teacher <A>", "token-value")

    assert subject == "SmarTAI 密码重置"
    assert "你好，Teacher <A>" in text
    assert "你好，Teacher &lt;A&gt;" in html


def test_reset_request_sends_link_and_persists_only_digest(monkeypatch):
    _create_user()
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.password_reset.settings.allowed_email_domains", "ustc.edu.cn")

    result = request_password_reset(
        email="Teacher@USTC.EDU.CN", source_ip="127.0.0.1", sender=sender
    )

    assert result["status"] == "reset_link_requested"
    assert len(sender.messages) == 1
    assert sender.messages[0]["subject"] == "SmarTAI 密码重置"
    token = sender.messages[0]["text"].split("#token=", 1)[1].split()[0]
    assert len(token) >= 120
    assert digest_reset_token(token) == hashlib.sha256(token.encode()).hexdigest()
    with session_scope() as session:
        row = session.query(PasswordResetRequestRecord).one()
        assert row.token_digest == digest_reset_token(token)
        assert not hasattr(row, "token")
        assert token not in row.token_digest


def test_confirm_reset_changes_password_and_consumes_link(monkeypatch):
    _create_user()
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.password_reset.settings.allowed_email_domains", "ustc.edu.cn")
    request_password_reset(email="teacher@ustc.edu.cn", source_ip="127.0.0.1", sender=sender)
    token = sender.messages[0]["text"].split("#token=", 1)[1].split()[0]

    assert confirm_password_reset(token, "new-password-456") == {"status": "password_reset"}
    with session_scope() as session:
        user = session.get(UserRecord, "reset-user")
        row = session.query(PasswordResetRequestRecord).one()
        assert user is not None and verify_password("new-password-456", user.password_hash)
        assert row.consumed_at is not None
        assert user.auth_invalid_before is not None

    with pytest.raises(PasswordResetError) as exc_info:
        confirm_password_reset(token, "another-password-789")
    assert exc_info.value.code == "password_reset_link_already_used"


def test_smtp_failure_does_not_leave_reset_request(monkeypatch):
    _create_user()
    monkeypatch.setattr("backend.services.password_reset.settings.allowed_email_domains", "ustc.edu.cn")
    with pytest.raises(PasswordResetError) as exc_info:
        request_password_reset(
            email="teacher@ustc.edu.cn",
            source_ip="127.0.0.1",
            sender=_FakeSender(fail=True),
        )
    assert exc_info.value.code == "password_reset_unavailable"
    with session_scope() as session:
        assert session.query(PasswordResetRequestRecord).count() == 0


def test_expired_reset_link_is_rejected(monkeypatch):
    _create_user()
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.password_reset.settings.allowed_email_domains", "ustc.edu.cn")
    monkeypatch.setattr("backend.services.password_reset.settings.email_verification_expiry_seconds", 1)
    request_password_reset(email="teacher@ustc.edu.cn", source_ip="127.0.0.1", sender=sender)
    token = sender.messages[0]["text"].split("#token=", 1)[1].split()[0]
    with session_scope() as session:
        session.query(PasswordResetRequestRecord).one().expires_at = time.time() - 1

    with pytest.raises(PasswordResetError) as exc_info:
        confirm_password_reset(token, "new-password-456")
    assert exc_info.value.code == "password_reset_link_expired"


def test_password_reset_api_is_neutral_and_invalidates_old_auth(monkeypatch):
    _create_user()
    sender = _FakeSender()
    monkeypatch.setattr("backend.api.auth.get_email_sender", lambda: sender)
    monkeypatch.setattr("backend.services.password_reset.settings.allowed_email_domains", "ustc.edu.cn")
    client = TestClient(app)

    missing = client.post("/auth/password-reset/request", json={"email": "missing@ustc.edu.cn"})
    existing = client.post("/auth/password-reset/request", json={"email": "teacher@ustc.edu.cn"})
    assert missing.status_code == existing.status_code == 202
    assert missing.json() == existing.json()
    assert "token" not in existing.json()

    old_access_token = create_token("reset-user", "teacher")
    refresh_one = create_refresh_session("reset-user", 30)
    refresh_two = create_refresh_session("reset-user", 30)
    token = sender.messages[0]["text"].split("#token=", 1)[1].split()[0]
    confirmed = client.post(
        "/auth/password-reset/confirm",
        json={"token": token, "new_password": "new-password-456"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json() == {"status": "password_reset"}
    assert "token" not in confirmed.json()
    assert client.post(
        "/auth/password-reset/confirm",
        json={"token": token, "new_password": "another-password-789"},
    ).status_code == 400
    with session_scope() as session:
        rows = session.query(RefreshSessionRecord).filter(
            RefreshSessionRecord.user_id == "reset-user"
        ).all()
        assert {row.revoked_at is not None for row in rows} == {True}

    protected = client.get("/auth/me", headers={"Authorization": f"Bearer {old_access_token}"})
    assert protected.status_code == 401
    fresh_login = client.post(
        "/auth/login",
        json={"username": "reset-user", "password": "new-password-456"},
    )
    assert fresh_login.status_code == 200
    fresh_me = client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {fresh_login.json()['token']}"},
    )
    assert fresh_me.status_code == 200
