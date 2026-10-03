"""Public auth entry contracts; all mail and identities are isolated fixtures."""
import hashlib
from collections import deque

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.auth import hash_password
from backend.config import settings
from backend.db import auth_repository
from backend.db.models import EmailVerificationRequestRecord, RefreshSessionRecord, UserRecord
from backend.db.session import session_scope
from backend.main import app
from backend.services import email_registration as registration
from backend.services import registration_username_check as probes


@pytest.fixture(autouse=True)
def isolated_probe_budget(monkeypatch):
    monkeypatch.setattr(probes, "_ATTEMPTS", tuple(deque() for _ in range(1024)))
    monkeypatch.setattr(settings, "allowed_email_domains", "ustc.edu.cn")


def seed(username="teacher", email="teacher@ustc.edu.cn", active=True, user_id="auth-entry-user"):
    with session_scope() as session:
        session.add(UserRecord(
            id=user_id, username=username, email=email, role="teacher",
            password_hash=hash_password("test-password"), is_active=active,
        ))


def credentials(mode, identity=None, password="test-password"):
    if mode == "email":
        return {"login_type": "email", "email": identity or "teacher@ustc.edu.cn", "password": password}
    return {"username": identity or "teacher", "password": password}


@pytest.mark.parametrize("mode", ["username", "email"])
@pytest.mark.parametrize("case", ["success", "wrong_password", "missing", "inactive"])
def test_login_modes_share_password_and_status_checks(mode, case):
    if case != "missing":
        seed(active=case != "inactive")
    client = TestClient(app)
    response = client.post("/auth/login", json=credentials(mode, password="wrong" if case == "wrong_password" else "test-password"))
    assert response.status_code == (200 if case == "success" else 401)
    assert bool(client.cookies.get(settings.refresh_cookie_name)) == (case == "success")
    with session_scope() as session:
        assert session.query(RefreshSessionRecord).count() == (1 if case == "success" else 0)


def test_explicit_login_mode_resolves_cross_account_collision_and_at_username():
    seed(username="other@ustc.edu.cn", email=None, user_id="username-owner")
    seed(username="email-owner", email="other@ustc.edu.cn", user_id="email-owner")
    for mode, expected in [("username", "username-owner"), ("email", "email-owner")]:
        response = TestClient(app).post("/auth/login", json=credentials(mode, "other@ustc.edu.cn"))
        assert response.status_code == 200
        assert response.json()["user"]["id"] == expected


def test_login_preserves_username_case_and_accepts_no_email_legacy_user():
    seed(username="Teacher", email=None)
    client = TestClient(app)
    assert client.post("/auth/login", json=credentials("username", "Teacher")).status_code == 200
    assert client.post("/auth/login", json=credentials("username", "teacher")).status_code == 401
    assert client.post("/auth/login", json=credentials("email")).status_code == 401


@pytest.mark.parametrize("email, input_email", [
    ("teacher@ustc.edu.cn", " Teacher@USTC.EDU.CN. "),
    ("teacher@xn--fiqs8s.edu.cn", "Teacher@中国.edu.cn"),
    (" Teacher@USTC.EDU.CN. ", "teacher@ustc.edu.cn"),
])
def test_email_login_reuses_registration_normalization_without_domain_gate(monkeypatch, email, input_email):
    seed(email=email)
    monkeypatch.setattr(settings, "allowed_email_domains", "different.edu.cn")
    response = TestClient(app).post("/auth/login", json=credentials("email", input_email))
    assert response.status_code == 200


def test_ambiguous_legacy_email_never_selects_first_account():
    seed(email="teacher@ustc.edu.cn", user_id="first")
    seed(username="second", email="Teacher@USTC.EDU.CN", user_id="second")
    assert TestClient(app).post("/auth/login", json=credentials("email")).status_code == 401


@pytest.mark.parametrize("payload", [
    {"email": "teacher@ustc.edu.cn"},
    {"login_type": "email", "username": "teacher"},
    {"login_type": "email", "email": "teacher@ustc.edu.cn", "username": "teacher"},
    {"login_type": "auto", "username": "teacher"},
])
def test_ambiguous_login_payload_rejected_and_password_redacted(payload):
    response = TestClient(app).post("/auth/login", json={**payload, "password": "private-test-password"})
    assert response.status_code == 422
    assert "private-test-password" not in response.text


def test_email_is_rechecked_under_existing_auth_lock(monkeypatch):
    seed()
    original = auth_repository._locked_user

    def changed_identity(session, user_id):
        record = original(session, user_id)
        record.email = "changed@ustc.edu.cn"
        return record

    monkeypatch.setattr(auth_repository, "_locked_user", changed_identity)
    assert TestClient(app).post("/auth/login", json=credentials("email")).status_code == 401


@pytest.mark.parametrize("mode", ["username", "email"])
def test_both_modes_reuse_refresh_expiry_logout_and_inactive_guards(mode):
    seed()
    client = TestClient(app)
    assert client.post("/auth/login", json=credentials(mode)).status_code == 200
    first = client.cookies.get(settings.refresh_cookie_name)
    refreshed = client.post("/auth/refresh")
    assert refreshed.status_code == 200
    assert client.cookies.get(settings.refresh_cookie_name) != first
    token = refreshed.json()["token"]
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.post("/auth/refresh").status_code == 401

    assert client.post("/auth/login", json=credentials(mode)).status_code == 200
    digest = hashlib.sha256(client.cookies.get(settings.refresh_cookie_name).encode()).hexdigest()
    with session_scope() as session:
        session.scalar(select(RefreshSessionRecord).where(RefreshSessionRecord.token_hash == digest)).expires_at = 0
    assert client.post("/auth/refresh").status_code == 401
    response = client.post("/auth/login", json=credentials(mode))
    with session_scope() as session:
        session.get(UserRecord, "auth-entry-user").is_active = False
    assert client.post("/auth/refresh").status_code == 401
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {response.json()['token']}"}).status_code == 401


def test_username_check_is_advisory_and_submit_rechecks_before_sending(monkeypatch):
    client = TestClient(app)
    available = client.post("/auth/register/username-check", json={"username": " Teacher "})
    assert available.json() == {"available": True}
    assert available.headers["Cache-Control"] == "no-store"
    seed(username="Teacher")
    assert client.post("/auth/register/username-check", json={"username": " Teacher "}).json() == {"available": False}
    assert client.post("/auth/register/username-check", json={"username": "teacher"}).json() == {"available": True}
    sent = []

    class Sender:
        def send(self, *args):
            sent.append(args)

    monkeypatch.setattr("backend.api.auth.get_email_sender", Sender)
    response = client.post("/auth/register/request", json={"username": "Teacher", "email": "new@ustc.edu.cn", "password": "test-password"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "registration_username_taken"
    assert not sent
    with session_scope() as session:
        assert session.query(EmailVerificationRequestRecord).count() == 0


def test_username_probes_share_rate_limit_with_registration_and_recover(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(probes.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(probes, "_MAX_ATTEMPTS", 2)
    client = TestClient(app)
    for _ in range(2):
        assert client.post("/auth/register/username-check", json={"username": "teacher"}).status_code == 200
    for path, payload in [
        ("username-check", {"username": "teacher"}),
        ("request", {"username": "teacher", "email": "new@ustc.edu.cn", "password": "test-password"}),
    ]:
        response = client.post(f"/auth/register/{path}", json=payload)
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"
    now[0] += 60
    assert client.post("/auth/register/username-check", json={"username": "teacher"}).status_code == 200


@pytest.mark.parametrize("phase", ["resend", "verify", "integrity_race"])
def test_username_taken_after_request_keeps_final_protections(monkeypatch, phase):
    messages = []

    class Sender:
        def send(self, *args):
            messages.append(args)

    flow = registration.request_registration(username="teacher", email="new@ustc.edu.cn", password="test-password", source_ip=None, sender=Sender())
    token = messages[0][2].split("#token=", 1)[1].split()[0]
    if phase == "integrity_race":
        def raced(**kwargs):
            raise IntegrityError("insert", {}, Exception("unique collision"))
        monkeypatch.setattr(registration, "_persist_user", raced)
    else:
        seed()
    if phase == "resend":
        with session_scope() as session:
            session.get(EmailVerificationRequestRecord, flow["request_id"]).resend_available_at = 0
        with pytest.raises(registration.RegistrationError, match="registration_username_taken"):
            registration.resend_registration(request_id=flow["request_id"], source_ip=None, sender=Sender())
    else:
        with pytest.raises(registration.RegistrationError, match="registration_unavailable"):
            registration.verify_registration(token)
        with session_scope() as session:
            assert session.get(EmailVerificationRequestRecord, flow["request_id"]).superseded_at is not None
    assert len(messages) == 1


def test_database_retains_username_unique_constraint():
    seed()
    with pytest.raises(IntegrityError), session_scope() as session:
        session.add(UserRecord(id="collision", username="teacher", email=None, role="teacher", password_hash="unused", is_active=True))
        session.flush()
