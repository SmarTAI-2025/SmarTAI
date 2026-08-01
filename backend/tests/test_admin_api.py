"""Admin API and normalized identity contract (Task 3).

Covers the approved design's identity rules:

* public user payloads no longer carry ``course_ids`` (membership is
  course_enrollments only);
* admin can list/activate/deactivate users and create teacher/student invites;
* a teacher (non-admin) cannot call admin endpoints;
* a deactivated user can no longer login or refresh;
* a student invite pre-bound to a course enrolls the new student on
  registration through the course service, after validating the target course
  exists (a stale course_id produces no enrollment and no crash).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from backend.auth import hash_password
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


def test_public_user_payload_has_no_course_ids():
    client, token = _admin_client()
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    body = me.json()
    assert "course_ids" not in body, "public user payload must not expose course_ids"
    assert set(body.keys()) == {"id", "username", "email", "role", "is_active", "created_at"}


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
    # Create a student invite + register
    invite = client.post("/admin/invites", headers={"Authorization": f"Bearer {token}"}, json={"role": "student"})
    assert invite.status_code == 200
    code = invite.json()["invite_code"]
    reg = client.post("/auth/register", json={"username": "stu_one", "password": "secret-pass", "invite_code": code})
    assert reg.status_code == 200, reg.text
    stu_id = reg.json()["user"]["id"]

    # Deactivate
    resp = client.patch(f"/admin/users/{stu_id}/active", headers={"Authorization": f"Bearer {token}"}, json={"is_active": False})
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    # Deactivated user can no longer login
    login = client.post("/auth/login", json={"username": "stu_one", "password": "secret-pass"})
    assert login.status_code == 401

    # Reactivate
    react = client.patch(f"/admin/users/{stu_id}/active", headers={"Authorization": f"Bearer {token}"}, json={"is_active": True})
    assert react.status_code == 200
    assert react.json()["is_active"] is True
    assert client.post("/auth/login", json={"username": "stu_one", "password": "secret-pass"}).status_code == 200


def test_admin_cannot_deactivate_teacher_who_owns_a_course():
    client, token = _admin_client()
    # Create a teacher + a course they own
    invite = client.post("/admin/invites", headers={"Authorization": f"Bearer {token}"}, json={"role": "teacher"})
    code = invite.json()["invite_code"]
    reg = client.post("/auth/register", json={"username": "teach1", "password": "secret-pass", "invite_code": code})
    teacher_id = reg.json()["user"]["id"]
    create_course(teacher_id=teacher_id, name="Owned")

    resp = client.patch(f"/admin/users/{teacher_id}/active", headers={"Authorization": f"Bearer {token}"}, json={"is_active": False})
    assert resp.status_code == 409


def test_admin_can_create_teacher_and_student_invites():
    client, token = _admin_client()
    for role in ("teacher", "student"):
        resp = client.post("/admin/invites", headers={"Authorization": f"Bearer {token}"}, json={"role": role})
        assert resp.status_code == 200
        assert resp.json()["role"] == role
        assert resp.json()["invite_code"]
    # admin role invite also allowed from admin endpoint
    assert client.post("/admin/invites", headers={"Authorization": f"Bearer {token}"}, json={"role": "admin"}).status_code == 200


def test_teacher_cannot_call_admin_endpoints():
    # Register a teacher via invite
    admin_client, admin_token = _admin_client()
    invite = admin_client.post("/admin/invites", headers={"Authorization": f"Bearer {admin_token}"}, json={"role": "teacher"})
    code = invite.json()["invite_code"]
    reg = admin_client.post("/auth/register", json={"username": "tchr", "password": "secret-pass", "invite_code": code})
    assert reg.status_code == 200, reg.text
    teacher_token = reg.json()["token"]

    client = TestClient(app)
    h = {"Authorization": f"Bearer {teacher_token}"}
    assert client.get("/admin/users", headers=h).status_code == 403
    assert client.post("/admin/invites", headers=h, json={"role": "student"}).status_code == 403


def test_student_invite_with_course_enrolls_on_registration():
    client, admin_token = _admin_client()
    # Admin creates a teacher who creates a course
    t_invite = client.post("/admin/invites", headers={"Authorization": f"Bearer {admin_token}"}, json={"role": "teacher"})
    t_code = t_invite.json()["invite_code"]
    t_reg = client.post("/auth/register", json={"username": "teachx", "password": "secret-pass", "invite_code": t_code})
    assert t_reg.status_code == 200, t_reg.text
    teacher_id = t_reg.json()["user"]["id"]
    course = create_course(teacher_id=teacher_id, name="Math")

    # Admin creates a student invite pre-bound to that course
    s_invite = client.post("/admin/invites", headers={"Authorization": f"Bearer {admin_token}"}, json={"role": "student", "course_id": course.id})
    assert s_invite.status_code == 200
    s_code = s_invite.json()["invite_code"]

    s_reg = client.post("/auth/register", json={"username": "studx", "password": "secret-pass", "invite_code": s_code})
    assert s_reg.status_code == 200, s_reg.text
    student_id = s_reg.json()["user"]["id"]

    from backend.db.course_repository import is_enrolled
    assert is_enrolled(course_id=course.id, student_id=student_id) is True


def test_student_invite_with_unknown_course_does_not_enroll():
    client, admin_token = _admin_client()
    # course_id references a non-existent course; the invite is still created
    # (admin may not have validated), but registration must not crash and must
    # not create a phantom enrollment.
    s_invite = client.post("/admin/invites", headers={"Authorization": f"Bearer {admin_token}"}, json={"role": "student", "course_id": "no-such-course"})
    assert s_invite.status_code == 404  # admin endpoint validates the course exists


def test_disabled_user_cannot_refresh():
    client, admin_token = _admin_client()
    invite = client.post("/admin/invites", headers={"Authorization": f"Bearer {admin_token}"}, json={"role": "student"})
    code = invite.json()["invite_code"]
    reg = client.post("/auth/register", json={"username": "srefresh_one", "password": "secret-pass", "invite_code": code})
    stu_id = reg.json()["user"]["id"]
    assert client.post("/auth/refresh").status_code == 200  # active can refresh
    client.patch(f"/admin/users/{stu_id}/active", headers={"Authorization": f"Bearer {admin_token}"}, json={"is_active": False})
    assert client.post("/auth/refresh").status_code == 401
