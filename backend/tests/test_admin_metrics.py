from __future__ import annotations

import time

from fastapi.testclient import TestClient

from backend.auth import hash_password
from backend.db.auth_repository import register_without_invite
from backend.models import User
from backend.state import get_user_store
from backend.main import app


def test_metrics_query_exposes_product_events_and_catalog() -> None:
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

    catalog = client.get("/admin/metrics/catalog", headers=headers)
    assert catalog.status_code == 200
    assert {item["key"] for item in catalog.json()["metrics"]} >= {"active_users", "new_users", "login_successes"}

    result = client.post(
        "/admin/metrics/query",
        headers=headers,
        json={
            "start": time.time() - 60,
            "end": time.time() + 60,
            "granularity": "day",
            "metrics": ["active_users", "new_users", "login_successes"],
        },
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "available"
    assert body["totals"]["active_users"] >= 1
    assert body["totals"]["new_users"] >= 1
    assert body["totals"]["login_successes"] >= 1
    assert body["metric_status"]["active_users"] == "available"
