from __future__ import annotations


from fastapi.testclient import TestClient

from backend.auth import hash_password
from backend.db.auth_repository import register_without_invite
from backend.models import User
from backend.state import get_user_store
from backend.main import app


def test_current_overview_exposes_collected_product_events() -> None:
    get_user_store()["metrics-admin"] = User(
        id="metrics-admin", username="metrics-admin", role="admin", password_hash=hash_password("admin-pass")
    )
    teacher = register_without_invite(
        username="metrics-teacher", email="metrics@example.edu", password_hash=hash_password("teacher-pass")
    )
    client = TestClient(app)
    admin_login = client.post("/auth/login", json={"username": "metrics-admin", "password": "admin-pass"})
    teacher_login = client.post("/auth/login", json={"username": "metrics-teacher", "password": "teacher-pass"})
    assert admin_login.status_code == 200
    assert teacher_login.status_code == 200
    headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}

    result = client.get("/admin/overview", headers=headers)
    assert result.status_code == 200, result.text
    body = result.json()["usage"]
    assert body["status"] == "available"
    assert body["totals"]["active_users"] >= 1
    assert body["totals"]["new_users"] >= 1
    assert body["totals"]["login_successes"] >= 1
    assert body["metric_status"]["active_users"] == "available"
