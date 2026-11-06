from fastapi.testclient import TestClient
from sqlalchemy import select
import bcrypt
import hashlib
import pytest

from backend.auth import hash_password, verify_password
from backend.db.auth_repository import (
    AuthRepositoryError,
    register_without_invite,
)
from backend.db.models import RefreshSessionRecord, UserRecord
from backend.db.session import session_scope
from backend.main import app
from backend.models import User
from backend.state import get_user_store
from backend.config import settings


def _seed_admin() -> None:
    get_user_store()["admin-1"] = User(
        id="admin-1", username="admin", role="admin", password_hash=hash_password("admin-pass")
    )


def test_refresh_rotates_cookie_and_logout_revokes_it():
    _seed_admin()
    client = TestClient(app)
    login = client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
    assert login.status_code == 200
    first_cookie = client.cookies.get("smartai_refresh")
    assert first_cookie
    refreshed = client.post("/auth/refresh")
    assert refreshed.status_code == 200
    second_cookie = client.cookies.get("smartai_refresh")
    assert second_cookie and second_cookie != first_cookie
    assert client.post("/auth/logout", headers={"Authorization": f"Bearer {refreshed.json()['token']}"}).status_code == 200
    assert client.post("/auth/refresh").status_code == 401


def test_expired_refresh_session_is_rejected():
    _seed_admin()
    client = TestClient(app)
    assert client.post("/auth/login", json={"username": "admin", "password": "admin-pass"}).status_code == 200
    cookie_hash = hashlib.sha256(client.cookies.get("smartai_refresh").encode()).hexdigest()
    with session_scope() as session:
        record = session.scalar(select(RefreshSessionRecord).where(RefreshSessionRecord.token_hash == cookie_hash))
        assert record is not None
        record.expires_at = 0
    assert client.post("/auth/refresh").status_code == 401


def test_unscoped_refresh_cookie_is_rejected_even_if_its_record_exists():
    _seed_admin()
    client = TestClient(app)
    assert client.post("/auth/login", json={"username": "admin", "password": "admin-pass"}).status_code == 200
    current = client.cookies.get("smartai_refresh")
    old_format = current.split(".", 1)[1]
    with session_scope() as session:
        record = session.scalar(select(RefreshSessionRecord).where(
            RefreshSessionRecord.token_hash == hashlib.sha256(current.encode()).hexdigest()))
        record.token_hash = hashlib.sha256(old_format.encode()).hexdigest()
    client.cookies.clear()
    client.cookies.set("smartai_refresh", old_format)
    assert client.post("/auth/refresh").status_code == 401


def test_public_registration_without_invite_requires_email_verification(monkeypatch):
    monkeypatch.setattr(settings, "registration_closed", False)
    client = TestClient(app)

    response = client.post("/auth/register", json={
        "username": "open-teacher",
        "password": "secret-pass",
        "role": "teacher",
    })

    assert response.status_code == 404, response.text
    assert client.cookies.get("smartai_refresh") is None


def test_public_registration_cannot_select_student_role(monkeypatch):
    monkeypatch.setattr(settings, "registration_closed", False)
    client = TestClient(app)

    response = client.post("/auth/register", json={
        "username": "open-student",
        "password": "secret-pass",
        "role": "student",
    })

    assert response.status_code == 404, response.text


def test_public_registration_cannot_select_admin_role(monkeypatch):
    monkeypatch.setattr(settings, "registration_closed", False)
    client = TestClient(app)

    response = client.post("/auth/register", json={
        "username": "open-admin",
        "password": "secret-pass",
        "role": "admin",
    })

    assert response.status_code == 404


def test_closed_registration_without_invite_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "registration_closed", True)
    client = TestClient(app)

    response = client.post("/auth/register", json={
        "username": "closed-registration",
        "password": "secret-pass",
    })

    assert response.status_code == 404


def test_demo_admin_token_is_rejected_and_not_persisted_by_default(monkeypatch):
    monkeypatch.setattr(settings, "require_auth", True)
    monkeypatch.setattr(settings, "allow_demo_tokens", False)
    client = TestClient(app)

    response = client.get(
        "/auth/me",
        headers={"Authorization": "Bearer demo-admin-default"},
    )

    assert response.status_code == 401
    assert "demo_default" not in get_user_store()


def test_demo_token_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(settings, "require_auth", True)
    monkeypatch.setattr(settings, "allow_demo_tokens", True)
    client = TestClient(app)

    response = client.get(
        "/auth/me",
        headers={"Authorization": "Bearer demo-admin-optin"},
    )

    assert response.status_code == 200
    assert response.json()["role"] == "admin"
    assert "demo_optin" in get_user_store()


def test_admin_jwt_cannot_call_teacher_write_endpoints(monkeypatch):
    monkeypatch.setattr(settings, "require_auth", True)
    _seed_admin()
    client = TestClient(app)
    login = client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
    assert login.status_code == 200, login.text

    response = client.post(
        "/courses",
        headers={"Authorization": f"Bearer {login.json()['token']}"},
        json={"name": "admin-must-not-create"},
    )

    assert response.status_code == 403


def test_real_jwt_authentication_is_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "require_auth", True)
    monkeypatch.setattr(settings, "allow_demo_tokens", False)
    get_user_store()["jwt-teacher"] = User(
        id="jwt-teacher",
        username="jwt-teacher",
        role="teacher",
        password_hash=hash_password("jwt-pass"),
    )
    client = TestClient(app)
    login = client.post("/auth/login", json={"username": "jwt-teacher", "password": "jwt-pass"})
    assert login.status_code == 200, login.text

    response = client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {login.json()['token']}"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "jwt-teacher"


def test_password_hash_supports_128_unicode_chars_and_rejects_legacy_bcrypt():
    long_password = "密" * 128
    password_hash = hash_password(long_password)

    assert verify_password(long_password, password_hash)
    assert not verify_password(("密" * 127) + "错", password_hash)

    legacy_hash = bcrypt.hashpw(b"legacy-password", bcrypt.gensalt()).decode("utf-8")
    assert not verify_password("legacy-password", legacy_hash)
    assert not verify_password("wrong-password", legacy_hash)


def test_login_accepts_128_character_unicode_password():
    password = "教" * 128
    get_user_store()["unicode-password"] = User(
        id="unicode-password",
        username="unicode-password",
        role="teacher",
        password_hash=hash_password(password),
    )

    response = TestClient(app).post(
        "/auth/login",
        json={"username": "unicode-password", "password": password},
    )

    assert response.status_code == 200, response.text


def test_repository_canonicalizes_email_and_blocks_case_variant_duplicate():
    first = register_without_invite(
        username="canonical-one",
        email="  Teacher@Example.EDU.  ",
        password_hash=hash_password("secret-pass"),
    )
    assert first.email == "teacher@example.edu"
    with session_scope() as session:
        assert session.get(UserRecord, first.id).email == "teacher@example.edu"

    with pytest.raises(AuthRepositoryError, match="Email already exists"):
        register_without_invite(
            username="canonical-two",
            email="TEACHER@example.edu",
            password_hash=hash_password("secret-pass"),
        )
