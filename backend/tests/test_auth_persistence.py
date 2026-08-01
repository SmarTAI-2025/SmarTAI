from fastapi.testclient import TestClient
from sqlalchemy import select
import hashlib

from backend.auth import hash_password
from backend.db.models import RefreshSessionRecord
from backend.db.session import session_scope
from backend.main import app
from backend.models import User
from backend.state import get_user_store
from backend.config import settings


def _seed_admin() -> None:
    get_user_store()["admin-1"] = User(
        id="admin-1", username="admin", role="admin", password_hash=hash_password("admin-pass")
    )


def test_admin_invite_registration_and_one_time_consumption():
    _seed_admin()
    client = TestClient(app)
    admin_login = client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
    assert admin_login.status_code == 200
    token = admin_login.json()["token"]
    invite = client.post("/users/invite", headers={"Authorization": f"Bearer {token}"}, json={"role": "teacher"})
    assert invite.status_code == 200
    code = invite.json()["invite_code"]

    registered = client.post("/auth/register", json={"username": "new-teacher", "password": "secret-pass", "invite_code": code})
    assert registered.status_code == 200
    assert client.cookies.get("smartai_refresh")
    repeated = client.post("/auth/register", json={"username": "other", "password": "secret-pass", "invite_code": code})
    assert repeated.status_code == 400


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


def test_open_registration_without_invite_creates_teacher(monkeypatch):
    monkeypatch.setattr(settings, "registration_closed", False)
    client = TestClient(app)

    response = client.post("/auth/register", json={
        "username": "open-teacher",
        "password": "secret-pass",
        "role": "teacher",
    })

    assert response.status_code == 200, response.text
    assert response.json()["user"]["role"] == "teacher"
    assert client.cookies.get("smartai_refresh")


def test_open_registration_allows_student_role(monkeypatch):
    monkeypatch.setattr(settings, "registration_closed", False)
    client = TestClient(app)

    response = client.post("/auth/register", json={
        "username": "open-student",
        "password": "secret-pass",
        "role": "student",
    })

    assert response.status_code == 200, response.text
    assert response.json()["user"]["role"] == "student"


def test_open_registration_rejects_admin_role(monkeypatch):
    monkeypatch.setattr(settings, "registration_closed", False)
    client = TestClient(app)

    response = client.post("/auth/register", json={
        "username": "open-admin",
        "password": "secret-pass",
        "role": "admin",
    })

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid registration role"


def test_closed_registration_without_invite_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "registration_closed", True)
    client = TestClient(app)

    response = client.post("/auth/register", json={
        "username": "closed-registration",
        "password": "secret-pass",
    })

    assert response.status_code == 403
    assert response.json()["detail"] == "Invitation code required"


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
