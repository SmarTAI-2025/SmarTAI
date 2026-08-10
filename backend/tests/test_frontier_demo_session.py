from __future__ import annotations

from fastapi.testclient import TestClient

from backend.api import auth as auth_api
from backend.auth import decode_token
from backend.config import settings
from backend.main import app


def _enable_frontier_demo(monkeypatch) -> None:
    monkeypatch.setattr(settings, "frontier_demo_enabled", True)
    monkeypatch.setattr(settings, "frontier_demo_session_minutes", 20)
    monkeypatch.setattr(settings, "frontier_demo_daily_session_limit", 4)
    monkeypatch.setattr(settings, "frontier_demo_session_cooldown_seconds", 0.0)
    monkeypatch.setattr(settings, "shared_pool_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "server-side-test-key")
    monkeypatch.setattr(
        auth_api,
        "_frontier_demo_session_issuer",
        auth_api._FrontierDemoSessionIssuer(),
    )


def test_frontier_demo_session_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "frontier_demo_enabled", False)

    response = TestClient(app).post("/auth/frontier-demo-session")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "frontier_demo_disabled"


def test_frontier_demo_session_requires_backend_shared_gemini(monkeypatch):
    monkeypatch.setattr(settings, "frontier_demo_enabled", True)
    monkeypatch.setattr(settings, "shared_pool_enabled", False)
    monkeypatch.setattr(settings, "gemini_api_key", "")

    response = TestClient(app).post("/auth/frontier-demo-session")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "frontier_demo_provider_unavailable"


def test_frontier_demo_session_reuses_tasks_but_not_other_teacher_surfaces(
    monkeypatch,
):
    _enable_frontier_demo(monkeypatch)
    monkeypatch.setattr(settings, "require_auth", True)
    client = TestClient(app)

    issued = client.post("/auth/frontier-demo-session")

    assert issued.status_code == 200, issued.text
    body = issued.json()
    assert issued.headers["cache-control"] == "no-store"
    assert body["user"]["role"] == "teacher"
    assert body["expires_in"] == 20 * 60
    assert body["synthetic_data_only"] is True
    assert "auth_scope" not in body["user"]
    assert "password" not in issued.text.casefold()
    assert "api_key" not in issued.text.casefold()
    assert "server-side-test-key" not in issued.text
    assert client.cookies.get("smartai_refresh") is None

    payload = decode_token(body["token"])
    assert payload is not None
    assert payload["sub"] == body["user"]["id"]
    assert payload["scope"] == "frontier_demo"
    assert 1_190 <= payload["exp"] - payload["iat"] <= 1_200

    headers = {"Authorization": f"Bearer {body['token']}"}
    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["id"] == body["user"]["id"]

    task = client.post(
        "/tasks/",
        headers={**headers, "Idempotency-Key": "frontier-passwordless-test"},
        json={"name": "Synthetic Frontier demo"},
    )
    assert task.status_code == 200, task.text
    assert task.json()["owner_id"] == body["user"]["id"]

    preflight = client.post(
        f"/tasks/{task.json()['task_id']}/question-preparation/sources/preflight",
        headers=headers,
        data={
            "inline_text": "Q1. Synthetic calculus question",
            "structure_mode": "organized",
            "role": "problem",
            "save_to_library": "false",
        },
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["status"] == "ready"
    assert preflight.json()["source_token"]

    experts = client.get("/experts/available", headers=headers)
    courses = client.get("/courses", headers=headers)
    assert experts.status_code == 403
    assert courses.status_code == 403


def test_frontier_demo_sessions_are_unique_and_have_no_login_password(monkeypatch):
    _enable_frontier_demo(monkeypatch)
    client = TestClient(app)

    first = client.post("/auth/frontier-demo-session")
    second = client.post("/auth/frontier-demo-session")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["token"] != second.json()["token"]
    assert first.json()["user"]["id"] != second.json()["user"]["id"]
    login = client.post(
        "/auth/login",
        json={
            "username": first.json()["user"]["username"],
            "password": "anything",
        },
    )
    assert login.status_code == 401


def test_frontier_demo_session_issuance_has_cooldown(monkeypatch):
    _enable_frontier_demo(monkeypatch)
    monkeypatch.setattr(settings, "frontier_demo_session_cooldown_seconds", 60.0)
    client = TestClient(app)

    assert client.post("/auth/frontier-demo-session").status_code == 200
    limited = client.post("/auth/frontier-demo-session")

    assert limited.status_code == 429
    assert limited.json()["detail"]["code"] == "frontier_demo_session_cooldown"
    assert int(limited.headers["retry-after"]) >= 1


def test_frontier_demo_session_issuance_has_daily_limit(monkeypatch):
    _enable_frontier_demo(monkeypatch)
    monkeypatch.setattr(settings, "frontier_demo_daily_session_limit", 1)
    client = TestClient(app)

    assert client.post("/auth/frontier-demo-session").status_code == 200
    limited = client.post("/auth/frontier-demo-session")

    assert limited.status_code == 429
    assert limited.json()["detail"]["code"] == "frontier_demo_daily_limit_reached"


def test_frontier_demo_non_positive_daily_limit_is_unlimited(monkeypatch):
    _enable_frontier_demo(monkeypatch)
    monkeypatch.setattr(settings, "frontier_demo_daily_session_limit", 0)
    client = TestClient(app)

    responses = [client.post("/auth/frontier-demo-session") for _ in range(6)]

    assert {response.status_code for response in responses} == {200}
