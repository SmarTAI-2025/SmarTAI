"""Current administrator identity, suspension and authorization contracts."""
from __future__ import annotations

from fastapi.testclient import TestClient

from backend.auth import hash_password
from backend.db.auth_repository import register_without_invite
from backend.db.course_repository import create_course
from backend.main import app
from backend.models import User
from backend.state import get_user_store


def _seed_admin() -> str:
    get_user_store()["admin-1"] = User(
        id="admin-1", username="admin", role="admin", password_hash=hash_password("admin-pass")
    )
    return "admin-1"


def _admin_client() -> tuple[TestClient, str]:
    _seed_admin()
    client = TestClient(app)
    resp = client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
    assert resp.status_code == 200, resp.text
    return client, resp.json()["token"]


def _seed_and_login(client: TestClient, *, username: str, role: str):
    """Seed an isolated account, then exercise the current login endpoint."""
    register_without_invite(
        username=username,
        email=f"{username}@example.edu",
        role=role,
        password_hash=hash_password("secret-pass"),
    )
    response = client.post("/auth/login", json={"username": username, "password": "secret-pass"})
    assert response.status_code == 200, response.text
    return response


def test_public_user_payload_has_no_course_ids():
    client, token = _admin_client()
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    body = me.json()
    assert "course_ids" not in body, "public user payload must not expose course_ids"
    assert set(body.keys()) == {"id", "username", "email", "role", "is_active", "is_read_only", "created_at"}


def test_admin_can_list_users_and_filter():
    client, token = _admin_client()
    resp = client.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    users = resp.json()
    assert any(u["username"] == "admin" for u in users)
    # role filter returns only admins
    admins = client.get("/admin/users?role=admin", headers={"Authorization": f"Bearer {token}"}).json()
    assert all(u["role"] == "admin" for u in admins)


def test_admin_can_activate_and_deactivate_user():
    client, token = _admin_client()
    reg = _seed_and_login(client, username="stu_one", role="student")
    stu_id = reg.json()["user"]["id"]

    # Deactivate
    resp = client.patch(f"/admin/users/{stu_id}/status", headers={"Authorization": f"Bearer {token}"}, json={"is_active": False})
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    # Deactivated user can no longer login
    login = client.post("/auth/login", json={"username": "stu_one", "password": "secret-pass"})
    assert login.status_code == 401

    # Reactivate
    react = client.patch(f"/admin/users/{stu_id}/status", headers={"Authorization": f"Bearer {token}"}, json={"is_active": True})
    assert react.status_code == 200
    assert react.json()["is_active"] is True
    assert client.post("/auth/login", json={"username": "stu_one", "password": "secret-pass"}).status_code == 200


def test_admin_can_block_login_without_deleting_owned_course():
    client, token = _admin_client()
    # Create a teacher + a course they own
    reg = _seed_and_login(client, username="teach1", role="teacher")
    teacher_id = reg.json()["user"]["id"]
    create_course(teacher_id=teacher_id, name="Owned")

    resp = client.patch(f"/admin/users/{teacher_id}/status", headers={"Authorization": f"Bearer {token}"}, json={"is_active": False})
    assert resp.status_code == 200
    from backend.db.models import CourseRecord
    from backend.db.session import session_scope
    from sqlalchemy import select
    with session_scope() as session:
        assert session.scalar(select(CourseRecord.id).where(CourseRecord.teacher_id == teacher_id)) is not None


def test_teacher_cannot_call_admin_endpoints():
    admin_client, _ = _admin_client()
    reg = _seed_and_login(admin_client, username="tchr", role="teacher")
    teacher_token = reg.json()["token"]

    client = TestClient(app)
    h = {"Authorization": f"Bearer {teacher_token}"}
    assert client.get("/admin/users", headers=h).status_code == 403
    assert client.get("/admin/audit", headers=h).status_code == 403


def test_disabled_user_cannot_refresh():
    client, admin_token = _admin_client()
    reg = _seed_and_login(client, username="srefresh_one", role="student")
    stu_id = reg.json()["user"]["id"]
    assert client.post("/auth/refresh").status_code == 200  # active can refresh
    client.patch(f"/admin/users/{stu_id}/status", headers={"Authorization": f"Bearer {admin_token}"}, json={"is_active": False})
    assert client.post("/auth/refresh").status_code == 401
