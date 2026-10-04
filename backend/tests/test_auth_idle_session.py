import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.auth import create_token, decode_token, hash_password
from backend.config import settings
from backend.db.models import RefreshSessionRecord, UserRecord
from backend.db.session import session_scope
from backend.main import app
from backend.models import User
from backend.state import get_user_store


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setattr(settings, "session_idle_minutes", 30)
    monkeypatch.setattr(settings, "require_auth", True)
    get_user_store()["idle-teacher"] = User(id="idle-teacher", username="idle-teacher", role="teacher", password_hash=hash_password("test-password"))
    client = TestClient(app)
    response = client.post("/auth/login", json={"username": "idle-teacher", "password": "test-password"})
    assert response.status_code == 200
    return client, response.json()["token"]


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def age_session(token, idle_seconds):
    with session_scope() as session:
        record = session.scalar(select(RefreshSessionRecord).where(RefreshSessionRecord.token_hash == decode_token(token)["sid"]))
        record.created_at = time.time() - 7200
        record.last_used_at = time.time() - idle_seconds


def test_activity_renews_without_third_party_cookies_and_logout_revokes_bearer(signed_in):
    client, token = signed_in
    client.cookies.clear()
    age_session(token, 29 * 60)
    renewed = client.post("/auth/activity", headers={"Authorization": f"bearer {token}"})
    assert renewed.status_code == 200
    new_token = renewed.json()["token"]
    assert decode_token(new_token)["sid"] == decode_token(token)["sid"]
    assert 1795 <= decode_token(new_token)["exp"] - time.time() <= 1800
    assert client.get("/auth/me", headers=headers(new_token)).status_code == 200
    assert client.post("/auth/logout", headers=headers(new_token)).status_code == 200
    assert client.post("/auth/activity", headers=headers(new_token)).status_code == 401
    assert client.get("/auth/me", headers=headers(new_token)).status_code == 401


def test_idle_session_cannot_be_revived_by_token_cookie_or_first_click(signed_in):
    client, token = signed_in
    age_session(token, 30 * 60 + 1)
    assert client.get("/auth/me", headers=headers(token)).status_code == 401
    assert client.post("/auth/activity", headers=headers(token)).status_code == 401
    assert client.post("/auth/refresh").status_code == 401


def test_polling_and_cookie_rotation_do_not_reset_idle_clock(signed_in):
    client, token = signed_in
    age_session(token, 25 * 60)
    for _ in range(3):
        assert client.get("/auth/me", headers=headers(token)).status_code == 200
    old_cookie = client.cookies.get(settings.refresh_cookie_name)
    refreshed = client.post("/auth/refresh")
    assert refreshed.status_code == 200
    new_token = refreshed.json()["token"]
    assert 290 < decode_token(new_token)["exp"] - time.time() <= 300
    with session_scope() as session:
        record = session.scalar(select(RefreshSessionRecord).where(RefreshSessionRecord.token_hash == decode_token(new_token)["sid"]))
        assert time.time() - record.last_used_at >= 25 * 60
    client.cookies.set(settings.refresh_cookie_name, old_cookie)
    assert client.post("/auth/refresh").status_code == 401


@pytest.mark.parametrize("change", ["inactive", "reset", "revoked"])
def test_activity_cannot_bypass_account_or_session_revocation(signed_in, change):
    client, token = signed_in
    with session_scope() as session:
        user = session.get(UserRecord, "idle-teacher")
        if change == "inactive": user.is_active = False
        elif change == "reset": user.auth_version += 1
        else:
            record = session.scalar(select(RefreshSessionRecord).where(RefreshSessionRecord.token_hash == decode_token(token)["sid"]))
            record.revoked_at = time.time()
    client.cookies.clear()
    assert client.post("/auth/activity", headers=headers(token)).status_code == 401


def test_read_only_account_can_keep_reading_without_mutation_access(signed_in):
    client, token = signed_in
    with session_scope() as session:
        session.get(UserRecord, "idle-teacher").is_read_only = True
    assert client.post("/auth/activity", headers=headers(token)).status_code == 200
    assert client.post("/tasks/", headers=headers(token), json={}).status_code == 403


def test_unexpired_legacy_token_upgrades_without_forcing_another_login(signed_in):
    client, _ = signed_in
    client.cookies.clear()
    legacy = create_token("idle-teacher", "teacher")
    renewed = client.post("/auth/activity", headers=headers(legacy))
    assert renewed.status_code == 200
    assert decode_token(renewed.json()["token"])["sid"]
    expired = create_token("idle-teacher", "teacher", expires_in_minutes=-1)
    assert client.post("/auth/activity", headers=headers(expired)).status_code == 401


def test_legacy_access_lifetime_setting_cannot_shorten_the_new_idle_window(signed_in, monkeypatch):
    client, _ = signed_in
    monkeypatch.setattr(settings, "jwt_expiry_minutes", 1)
    response = client.post("/auth/login", json={"username": "idle-teacher", "password": "test-password"})
    assert response.status_code == 200
    assert 1795 <= decode_token(response.json()["token"])["exp"] - time.time() <= 1800
