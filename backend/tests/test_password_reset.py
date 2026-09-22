from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.auth import hash_password, verify_password
from backend.db.auth_repository import (
    authenticate_and_create_session,
    create_refresh_session,
    rotate_refresh_session,
)
from backend.db.models import (
    PasswordResetRateEventRecord,
    PasswordResetRequestRecord,
    RefreshSessionRecord,
    UserRecord,
)
from backend.db.session import session_scope
from backend.main import app
from backend.services.password_reset import (
    PasswordResetError,
    _activate_delivered_request,
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
    # Compatibility for a pre-canonicalization local row. New writes and the
    # migration normalize this, while lookup remains safe during transition.
    _create_user(email="Teacher@USTC.EDU.CN")
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.password_reset.settings.allowed_email_domains", "ustc.edu.cn")

    result = request_password_reset(
        email="teacher@ustc.edu.cn", source_ip="127.0.0.1", sender=sender
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


def test_smtp_failure_is_neutral_and_does_not_log_provider_detail(monkeypatch, caplog):
    _create_user()
    monkeypatch.setattr("backend.services.password_reset.settings.allowed_email_domains", "ustc.edu.cn")
    existing = request_password_reset(
        email="teacher@ustc.edu.cn",
        source_ip="127.0.0.1",
        sender=_FakeSender(fail=True),
    )
    missing = request_password_reset(
        email="missing@ustc.edu.cn",
        source_ip="127.0.0.2",
        sender=_FakeSender(fail=True),
    )
    assert existing == missing == {
        "status": "reset_link_requested",
        "expires_in_seconds": 1800,
        "resend_after_seconds": 60,
    }
    assert "smtp secret must not escape" not in caplog.text
    assert "RuntimeError" not in caplog.text
    with session_scope() as session:
        reset_rows = session.query(PasswordResetRequestRecord).all()
        assert len(reset_rows) == 1
        assert reset_rows[0].delivery_status == "failed"
        assert reset_rows[0].last_delivery_error_code == "password_reset_unavailable"
        assert (
            f"request_id={reset_rows[0].id} stage=smtp "
            "code=password_reset_unavailable"
        ) in caplog.text
        assert session.query(PasswordResetRateEventRecord).count() == 2


def test_failed_replacement_keeps_previous_sent_token_usable(monkeypatch):
    _create_user()
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.password_reset.settings.allowed_email_domains", "ustc.edu.cn")
    request_password_reset(
        email="teacher@ustc.edu.cn",
        source_ip="127.0.0.1",
        sender=sender,
    )
    first_token = sender.messages[0]["text"].split("#token=", 1)[1].split()[0]
    with session_scope() as session:
        session.query(PasswordResetRequestRecord).one().resend_available_at = 0
        session.query(PasswordResetRateEventRecord).update({
            PasswordResetRateEventRecord.created_at: time.time() - 61
        })

    replacement = request_password_reset(
        email="teacher@ustc.edu.cn",
        source_ip="127.0.0.2",
        sender=_FakeSender(fail=True),
    )

    assert replacement["status"] == "reset_link_requested"
    with session_scope() as session:
        rows = session.query(PasswordResetRequestRecord).order_by(
            PasswordResetRequestRecord.created_at
        ).all()
        assert [row.delivery_status for row in rows] == ["sent", "failed"]
        assert rows[0].superseded_at is None
    assert confirm_password_reset(first_token, "new-password-456") == {
        "status": "password_reset"
    }


def test_pending_reset_token_cannot_be_consumed():
    _create_user()
    raw_token = generate_reset_token()
    now = time.time()
    with session_scope() as session:
        session.add(PasswordResetRequestRecord(
            id="pending-reset",
            user_id="reset-user",
            token_digest=digest_reset_token(raw_token),
            created_at=now,
            expires_at=now + 1800,
            resend_available_at=now + 60,
            delivery_status="pending",
        ))

    with pytest.raises(PasswordResetError) as exc_info:
        confirm_password_reset(raw_token, "new-password-456")
    assert exc_info.value.code == "password_reset_link_invalid"


def test_older_late_sender_cannot_supersede_newer_success():
    _create_user()
    slow_token = generate_reset_token()
    winner_token = generate_reset_token()
    now = time.time()
    with session_scope() as session:
        session.add_all([
            PasswordResetRequestRecord(
                id="slow-delivery",
                user_id="reset-user",
                token_digest=digest_reset_token(slow_token),
                created_at=now,
                expires_at=now + 1800,
                resend_available_at=now + 60,
                delivery_status="pending",
            ),
            PasswordResetRequestRecord(
                id="delivery-winner",
                user_id="reset-user",
                token_digest=digest_reset_token(winner_token),
                created_at=now + 61,
                expires_at=now + 1861,
                resend_available_at=now + 121,
                delivery_status="pending",
            ),
        ])

    _activate_delivered_request(user_id="reset-user", request_id="delivery-winner")
    _activate_delivered_request(user_id="reset-user", request_id="slow-delivery")

    with session_scope() as session:
        slow = session.get(PasswordResetRequestRecord, "slow-delivery")
        winner = session.get(PasswordResetRequestRecord, "delivery-winner")
        assert slow is not None and slow.delivery_status == "sent"
        assert slow.superseded_at is not None
        assert winner is not None and winner.delivery_status == "sent"
        assert winner.superseded_at is None
    with pytest.raises(PasswordResetError) as exc_info:
        confirm_password_reset(slow_token, "new-password-456")
    assert exc_info.value.code == "password_reset_link_already_used"
    assert confirm_password_reset(winner_token, "new-password-456") == {
        "status": "password_reset"
    }


def test_newer_success_supersedes_older_even_when_older_finishes_first():
    _create_user()
    older_token = generate_reset_token()
    newer_token = generate_reset_token()
    now = time.time()
    with session_scope() as session:
        session.add_all([
            PasswordResetRequestRecord(
                id="older-first-success",
                user_id="reset-user",
                token_digest=digest_reset_token(older_token),
                created_at=now,
                expires_at=now + 1800,
                resend_available_at=now + 60,
                delivery_status="pending",
            ),
            PasswordResetRequestRecord(
                id="newer-later-success",
                user_id="reset-user",
                token_digest=digest_reset_token(newer_token),
                created_at=now + 61,
                expires_at=now + 1861,
                resend_available_at=now + 121,
                delivery_status="pending",
            ),
        ])

    _activate_delivered_request(user_id="reset-user", request_id="older-first-success")
    with session_scope() as session:
        older = session.get(PasswordResetRequestRecord, "older-first-success")
        newer = session.get(PasswordResetRequestRecord, "newer-later-success")
        assert older is not None and older.delivery_status == "sent"
        assert older.superseded_at is None
        assert newer is not None and newer.delivery_status == "pending"
        assert newer.superseded_at is None

    _activate_delivered_request(user_id="reset-user", request_id="newer-later-success")
    with session_scope() as session:
        older = session.get(PasswordResetRequestRecord, "older-first-success")
        newer = session.get(PasswordResetRequestRecord, "newer-later-success")
        assert older is not None and older.superseded_at is not None
        assert newer is not None and newer.delivery_status == "sent"
        assert newer.superseded_at is None
    with pytest.raises(PasswordResetError) as exc_info:
        confirm_password_reset(older_token, "new-password-456")
    assert exc_info.value.code == "password_reset_link_already_used"
    assert confirm_password_reset(newer_token, "new-password-456") == {
        "status": "password_reset"
    }


def test_newer_delivery_failure_keeps_older_success_usable():
    _create_user()
    older_token = generate_reset_token()
    newer_token = generate_reset_token()
    now = time.time()
    with session_scope() as session:
        session.add_all([
            PasswordResetRequestRecord(
                id="older-fallback",
                user_id="reset-user",
                token_digest=digest_reset_token(older_token),
                created_at=now,
                expires_at=now + 1800,
                resend_available_at=now + 60,
                delivery_status="pending",
            ),
            PasswordResetRequestRecord(
                id="newer-failure",
                user_id="reset-user",
                token_digest=digest_reset_token(newer_token),
                created_at=now + 61,
                expires_at=now + 1861,
                resend_available_at=now + 121,
                delivery_status="pending",
            ),
        ])

    _activate_delivered_request(user_id="reset-user", request_id="older-fallback")
    with session_scope() as session:
        failed = session.get(PasswordResetRequestRecord, "newer-failure")
        assert failed is not None
        failed.delivery_status = "failed"
        failed.last_delivery_error_code = "password_reset_unavailable"

    assert confirm_password_reset(older_token, "new-password-456") == {
        "status": "password_reset"
    }


def test_consuming_reset_token_invalidates_other_pending_delivery():
    _create_user()
    active_token = generate_reset_token()
    delayed_token = generate_reset_token()
    now = time.time()
    with session_scope() as session:
        session.add_all([
            PasswordResetRequestRecord(
                id="active-reset",
                user_id="reset-user",
                token_digest=digest_reset_token(active_token),
                created_at=now,
                expires_at=now + 1800,
                resend_available_at=now + 60,
                delivery_status="sent",
            ),
            PasswordResetRequestRecord(
                id="delayed-reset",
                user_id="reset-user",
                token_digest=digest_reset_token(delayed_token),
                created_at=now + 61,
                expires_at=now + 1861,
                resend_available_at=now + 121,
                delivery_status="pending",
            ),
        ])

    assert confirm_password_reset(active_token, "new-password-456") == {
        "status": "password_reset"
    }
    _activate_delivered_request(user_id="reset-user", request_id="delayed-reset")
    with session_scope() as session:
        delayed = session.get(PasswordResetRequestRecord, "delayed-reset")
        assert delayed is not None and delayed.delivery_status == "sent"
        assert delayed.superseded_at is not None
    with pytest.raises(PasswordResetError) as exc_info:
        confirm_password_reset(delayed_token, "another-password-789")
    assert exc_info.value.code == "password_reset_link_already_used"


def test_unknown_email_consumes_same_private_rate_limit_budget(monkeypatch):
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.password_reset.settings.allowed_email_domains", "ustc.edu.cn")
    monkeypatch.setattr("backend.services.password_reset.settings.email_verification_hourly_email_limit", 1)
    monkeypatch.setattr("backend.services.password_reset.settings.email_verification_hourly_ip_limit", 10)

    first = request_password_reset(
        email="Future@USTC.EDU.CN",
        source_ip="198.51.100.10",
        sender=sender,
    )
    _create_user(email="future@ustc.edu.cn")
    with pytest.raises(PasswordResetError) as exc_info:
        request_password_reset(
            email="future@ustc.edu.cn",
            source_ip="198.51.100.10",
            sender=sender,
        )

    assert first == {
        "status": "reset_link_requested",
        "expires_in_seconds": 1800,
        "resend_after_seconds": 60,
    }
    assert exc_info.value.code == "password_reset_rate_limited"
    assert exc_info.value.retry_after is not None
    assert sender.messages == []
    with session_scope() as session:
        events = session.query(PasswordResetRateEventRecord).all()
        assert len(events) == 1
        serialized = repr([(row.email_digest, row.source_ip_digest) for row in events])
        assert "future@ustc.edu.cn" not in serialized
        assert "198.51.100.10" not in serialized


def test_rejected_reset_requests_do_not_grow_rate_event_table(monkeypatch):
    monkeypatch.setattr("backend.services.password_reset.settings.allowed_email_domains", "ustc.edu.cn")
    monkeypatch.setattr("backend.services.password_reset.settings.email_verification_hourly_email_limit", 1)
    monkeypatch.setattr("backend.services.password_reset.settings.email_verification_hourly_ip_limit", 1)

    assert request_password_reset(
        email="limited@ustc.edu.cn",
        source_ip="198.51.100.99",
        sender=_FakeSender(),
    )["status"] == "reset_link_requested"
    for _ in range(20):
        with pytest.raises(PasswordResetError) as exc_info:
            request_password_reset(
                email="limited@ustc.edu.cn",
                source_ip="198.51.100.99",
                sender=_FakeSender(),
            )
        assert exc_info.value.code == "password_reset_rate_limited"

    with session_scope() as session:
        assert session.query(PasswordResetRateEventRecord).count() == 1


def test_rate_limit_response_is_identical_for_unknown_and_existing_email(monkeypatch):
    _create_user()
    sender = _FakeSender()
    monkeypatch.setattr("backend.api.auth.get_email_sender", lambda: sender)
    monkeypatch.setattr("backend.services.password_reset.settings.allowed_email_domains", "ustc.edu.cn")
    monkeypatch.setattr("backend.services.password_reset.settings.email_verification_hourly_email_limit", 10)
    monkeypatch.setattr("backend.services.password_reset.settings.email_verification_hourly_ip_limit", 10)
    monkeypatch.setattr("backend.services.password_reset.settings.email_verification_resend_seconds", 60)
    client = TestClient(app)

    assert client.post(
        "/auth/password-reset/request",
        json={"email": "missing@ustc.edu.cn"},
    ).status_code == 202
    assert client.post(
        "/auth/password-reset/request",
        json={"email": "teacher@ustc.edu.cn"},
    ).status_code == 202

    missing_limited = client.post(
        "/auth/password-reset/request",
        json={"email": "missing@ustc.edu.cn"},
    )
    existing_limited = client.post(
        "/auth/password-reset/request",
        json={"email": "teacher@ustc.edu.cn"},
    )

    assert missing_limited.status_code == existing_limited.status_code == 400
    assert missing_limited.json() == existing_limited.json() == {
        "detail": {"code": "password_reset_rate_limited"}
    }
    assert int(missing_limited.headers["Retry-After"]) in range(1, 61)
    assert int(existing_limited.headers["Retry-After"]) in range(1, 61)


def test_ip_hourly_limit_is_identical_for_unknown_and_existing_email(monkeypatch):
    _create_user()
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.password_reset.settings.allowed_email_domains", "ustc.edu.cn")
    monkeypatch.setattr("backend.services.password_reset.settings.email_verification_resend_seconds", 0)
    monkeypatch.setattr("backend.services.password_reset.settings.email_verification_hourly_email_limit", 10)
    monkeypatch.setattr("backend.services.password_reset.settings.email_verification_hourly_ip_limit", 1)

    assert request_password_reset(
        email="missing-one@ustc.edu.cn",
        source_ip="198.51.100.20",
        sender=sender,
    )["status"] == "reset_link_requested"
    assert request_password_reset(
        email="teacher@ustc.edu.cn",
        source_ip="198.51.100.21",
        sender=sender,
    )["status"] == "reset_link_requested"

    with pytest.raises(PasswordResetError) as missing_error:
        request_password_reset(
            email="missing-two@ustc.edu.cn",
            source_ip="198.51.100.20",
            sender=sender,
        )
    with pytest.raises(PasswordResetError) as existing_error:
        request_password_reset(
            email="teacher@ustc.edu.cn",
            source_ip="198.51.100.21",
            sender=sender,
        )

    assert missing_error.value.code == existing_error.value.code == (
        "password_reset_rate_limited"
    )
    assert missing_error.value.retry_after in range(1, 3601)
    assert existing_error.value.retry_after in range(1, 3601)


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
    old_login = client.post(
        "/auth/login",
        json={"username": "reset-user", "password": "old-password-123"},
    )
    assert old_login.status_code == 200

    missing = client.post("/auth/password-reset/request", json={"email": "missing@ustc.edu.cn"})
    existing = client.post("/auth/password-reset/request", json={"email": "teacher@ustc.edu.cn"})
    assert missing.status_code == existing.status_code == 202
    assert missing.json() == existing.json()
    assert "token" not in existing.json()

    old_access_token = old_login.json()["token"]
    refresh_one = client.cookies.get("smartai_refresh")
    assert refresh_one
    refresh_two = create_refresh_session("reset-user", 30)
    token = sender.messages[0]["text"].split("#token=", 1)[1].split()[0]
    confirmed = client.post(
        "/auth/password-reset/confirm",
        json={"token": token, "new_password": "new-password-456"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json() == {"status": "password_reset"}
    assert "token" not in confirmed.json()
    assert client.cookies.get("smartai_refresh") is None
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


def test_password_reset_validation_does_not_echo_token_or_password():
    client = TestClient(app)
    secret_token = "sensitive-token-" + ("x" * 520)
    secret_password = "sensitive-password-" + ("y" * 140)

    response = client.post(
        "/auth/password-reset/confirm",
        json={"token": secret_token, "new_password": secret_password},
    )

    assert response.status_code == 422
    assert secret_token not in response.text
    assert secret_password not in response.text
    assert all("input" not in error for error in response.json()["detail"])


def _create_reset_token(monkeypatch) -> str:
    sender = _FakeSender()
    monkeypatch.setattr("backend.services.password_reset.settings.allowed_email_domains", "ustc.edu.cn")
    request_password_reset(
        email="teacher@ustc.edu.cn",
        source_ip="203.0.113.4",
        sender=sender,
    )
    return sender.messages[0]["text"].split("#token=", 1)[1].split()[0]


def test_login_race_cannot_issue_surviving_session_after_password_reset(monkeypatch):
    _create_user()
    token = _create_reset_token(monkeypatch)
    entered_verify = threading.Event()
    release_verify = threading.Event()
    reset_started = threading.Event()
    original_verify = verify_password

    def blocking_verify(password: str, password_hash: str) -> bool:
        entered_verify.set()
        assert release_verify.wait(timeout=5)
        return original_verify(password, password_hash)

    monkeypatch.setattr("backend.db.auth_repository.verify_password", blocking_verify)

    def reset_worker():
        reset_started.set()
        return confirm_password_reset(token, "new-password-456")

    with ThreadPoolExecutor(max_workers=2) as executor:
        login_future = executor.submit(
            authenticate_and_create_session,
            "reset-user",
            "old-password-123",
            30,
        )
        assert entered_verify.wait(timeout=5)
        reset_future = executor.submit(reset_worker)
        assert reset_started.wait(timeout=5)
        assert not reset_future.done()
        release_verify.set()
        authenticated = login_future.result(timeout=5)
        assert reset_future.result(timeout=5) == {"status": "password_reset"}

    assert authenticated is not None
    refresh, _user, access = authenticated
    assert rotate_refresh_session(refresh, 30) is None
    client = TestClient(app)
    assert client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {access}"},
    ).status_code == 401


def test_refresh_race_cannot_survive_password_reset(monkeypatch):
    _create_user()
    token = _create_reset_token(monkeypatch)
    original_refresh = create_refresh_session("reset-user", 30)
    entered_issue = threading.Event()
    release_issue = threading.Event()
    reset_started = threading.Event()

    from backend.db import auth_repository

    original_add = auth_repository._add_refresh_session

    def blocking_add(session, *, user_id: str, days: int, now: float):
        entered_issue.set()
        assert release_issue.wait(timeout=5)
        return original_add(session, user_id=user_id, days=days, now=now)

    monkeypatch.setattr(auth_repository, "_add_refresh_session", blocking_add)

    def reset_worker():
        reset_started.set()
        return confirm_password_reset(token, "new-password-456")

    with ThreadPoolExecutor(max_workers=2) as executor:
        rotate_future = executor.submit(rotate_refresh_session, original_refresh, 30)
        assert entered_issue.wait(timeout=5)
        reset_future = executor.submit(reset_worker)
        assert reset_started.wait(timeout=5)
        assert not reset_future.done()
        release_issue.set()
        rotated = rotate_future.result(timeout=5)
        assert reset_future.result(timeout=5) == {"status": "password_reset"}

    assert rotated is not None
    new_refresh, _user, access = rotated
    assert rotate_refresh_session(new_refresh, 30) is None
    client = TestClient(app)
    assert client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {access}"},
    ).status_code == 401
