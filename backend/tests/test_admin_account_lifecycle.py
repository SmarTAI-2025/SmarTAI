import uuid
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.auth import create_token, hash_password, verify_password
from backend.db.auth_repository import register_without_invite
from backend.db.models import UserRecord
from backend.db.session import session_scope
from backend.main import app
from backend.models import User
from backend.state import get_user_store


def accounts():
    get_user_store()["manager"] = User(id="manager", username="manager", email="manager@example.edu", role="admin", password_hash=hash_password("admin-test-password"))
    user = register_without_invite(username="teacher", email="teacher@example.edu", password_hash=hash_password("teacher-test-password"))
    client = TestClient(app)
    admin = client.post("/auth/login", json={"username": "manager", "password": "admin-test-password"}).json()["token"]
    teacher = client.post("/auth/login", json={"username": "teacher", "password": "teacher-test-password"}).json()["token"]
    return client, user, admin, teacher


def headers(token, key=None):
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key or uuid.uuid4().hex}


def test_read_only_preserves_login_and_historical_tasks_without_permitting_mutation():
    client, user, admin, teacher = accounts()
    created = client.post("/tasks", json={"name": "Historical grading"}, headers=headers(teacher))
    assert created.status_code == 200, created.text
    original = client.get("/tasks", headers=headers(teacher)).json()
    response = client.patch(f"/admin/users/{user.id}/access", json={"access": "read_only", "reason": "review"}, headers=headers(admin))
    assert response.status_code == 200, response.text
    assert response.json()["is_read_only"] is True
    assert client.get("/auth/me", headers=headers(teacher)).status_code == 200
    assert client.get("/tasks", headers=headers(teacher)).json() == original
    denied = client.post("/tasks", json={"name": "Must not create"}, headers=headers(teacher))
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "account_read_only"
    assert client.post("/auth/login", json={"username": "teacher", "password": "teacher-test-password"}).status_code == 200
    client.patch(f"/admin/users/{user.id}/access", json={"access": "normal", "reason": "resolved"}, headers=headers(admin))
    assert client.get("/tasks", headers=headers(teacher)).json() == original


def test_read_only_and_revoke_are_separate_and_access_mutations_are_idempotent():
    client, user, admin, teacher = accounts()
    request_headers = headers(admin, "one-action")
    body = {"access": "read_only", "reason": "review"}
    first = client.patch(f"/admin/users/{user.id}/access", json=body, headers=request_headers)
    assert client.patch(f"/admin/users/{user.id}/access", json=body, headers=request_headers).json() == first.json()
    assert client.patch(f"/admin/users/{user.id}/access", json={**body, "access": "normal"}, headers=request_headers).status_code == 409
    assert client.post(f"/admin/users/{user.id}/sessions/revoke", json={"reason": "compromised"}, headers=headers(admin)).status_code == 200
    assert client.get("/auth/me", headers=headers(teacher)).status_code == 401


def test_self_restriction_demotion_and_nonadmin_authorization():
    client, user, admin, teacher = accounts()
    for access in ("read_only", "login_blocked"):
        assert client.patch("/admin/users/manager/access", json={"access": access}, headers=headers(admin)).status_code == 409
    assert client.patch("/admin/users/manager/role", json={"role": "teacher"}, headers=headers(admin)).status_code == 409
    assert client.patch(f"/admin/users/{user.id}/role", json={"role": "admin"}, headers=headers(teacher)).status_code == 403
    response = client.patch(f"/admin/users/{user.id}/role", json={"role": "admin", "reason": "delegated_management"}, headers=headers(admin))
    assert response.status_code == 200, response.text
    assert client.get("/auth/me", headers=headers(teacher)).status_code == 401


def test_password_change_checks_current_password_and_revokes_all_logins():
    client, user, admin, teacher = accounts()
    wrong = client.post("/auth/password-change", json={"current_password": "wrong", "new_password": "new-test-password"}, headers=headers(admin))
    assert wrong.status_code == 400
    response = client.post("/auth/password-change", json={"current_password": "admin-test-password", "new_password": "new-test-password"}, headers=headers(admin))
    assert response.status_code == 200, response.text
    assert client.get("/auth/me", headers=headers(admin)).status_code == 401
    assert client.post("/auth/login", json={"username": "manager", "password": "admin-test-password"}).status_code == 401
    assert client.post("/auth/login", json={"username": "manager", "password": "new-test-password"}).status_code == 200
    assert client.get("/auth/me", headers=headers(teacher)).status_code == 200


def test_demo_admin_never_enters_private_admin(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "allow_demo_tokens", True)
    client = TestClient(app)
    assert client.get("/admin/users", headers=headers("demo-admin-manager")).status_code == 403
