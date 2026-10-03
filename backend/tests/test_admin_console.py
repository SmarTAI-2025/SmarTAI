from fastapi.testclient import TestClient

from backend.auth import hash_password
from backend.db.auth_repository import register_with_invite
from backend.db.course_repository import create_course
from backend.main import app
from backend.models import User
from backend.state import get_user_store


def _admin_client() -> tuple[TestClient, str]:
    get_user_store()["admin-console"] = User(
        id="admin-console", username="console-admin", role="admin", password_hash=hash_password("admin-pass")
    )
    client = TestClient(app)
    response = client.post("/auth/login", json={"username": "console-admin", "password": "admin-pass"})
    assert response.status_code == 200, response.text
    return client, response.json()["token"]


def _invite_user(client: TestClient, token: str, role: str, username: str) -> str:
    invite = client.post("/admin/invites", headers={"Authorization": f"Bearer {token}"}, json={"role": role})
    assert invite.status_code == 200
    register_with_invite(
        username=username,
        email=f"{username}@example.edu",
        role=role,
        password_hash=hash_password("user-pass"),
        invite_code=invite.json()["invite_code"],
    )
    login = client.post("/auth/login", json={"username": username, "password": "user-pass"})
    assert login.status_code == 200
    return login.json()["user"]["id"]


def test_suspend_then_restore_keeps_old_access_token_invalid():
    client, token = _admin_client()
    user_id = _invite_user(client, token, "teacher", "suspend-me")
    login = client.post("/auth/login", json={"username": "suspend-me", "password": "user-pass"})
    old_token = login.json()["token"]
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "suspend-1"}
    assert client.patch(f"/admin/users/{user_id}/status", headers=headers, json={"is_active": False, "reason": "risk_review"}).status_code == 200
    assert client.patch(f"/admin/users/{user_id}/status", headers={**headers, "Idempotency-Key": "restore-1"}, json={"is_active": True, "reason": "risk_review"}).status_code == 200
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {old_token}"}).status_code == 401


def test_admin_cannot_deactivate_self_or_last_admin():
    client, token = _admin_client()
    response = client.patch("/admin/users/admin-console/status", headers={"Authorization": f"Bearer {token}"}, json={"is_active": False, "reason": "offboarding"})
    assert response.status_code == 409


def test_course_owner_can_be_suspended_with_audited_reason():
    client, token = _admin_client()
    teacher_id = _invite_user(client, token, "teacher", "course-owner")
    create_course(teacher_id=teacher_id, name="Keep this course")
    response = client.patch(
        f"/admin/users/{teacher_id}/status",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "owner-suspend"},
        json={"is_active": False, "reason": "abuse_report", "note": "Preserve teaching data"},
    )
    assert response.status_code == 200, response.text
    audits = client.get("/admin/audit", headers={"Authorization": f"Bearer {token}"})
    assert audits.status_code == 200
    assert any(item["reason"] == "abuse_report" for item in audits.json())


def test_user_search_and_pagination_are_explicit():
    client, token = _admin_client()
    _invite_user(client, token, "student", "searchable-user")
    response = client.get("/admin/users", params={"search": "searchable", "page": 1, "page_size": 1}, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["items"][0]["username"] == "searchable-user"
    assert response.json()["total"] == 1


def test_overview_marks_uncollected_usage_instead_of_zero():
    client, token = _admin_client()
    response = client.get("/admin/overview", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["usage"]["status"] == "not_collected"


def test_public_production_app_does_not_mount_admin_routes(monkeypatch):
    from backend.config import settings
    from backend.main import create_app

    monkeypatch.setattr(settings, "runtime_environment", "production")
    production_app = create_app()
    assert not any(
        getattr(route, "path", "").startswith("/admin")
        for route in production_app.routes
    )
