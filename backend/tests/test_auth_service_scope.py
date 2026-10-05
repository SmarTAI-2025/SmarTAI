"""Public and private credentials remain independent on the same identity DB."""
import hashlib
import time
import uuid

import jwt
import pytest

from backend.auth import decode_token
from backend.config import settings
from backend.db.models import RefreshSessionRecord
from backend.db.session import session_scope
from backend.tests.test_admin_account_lifecycle import accounts, headers
from backend.tests.test_private_admin_app import private_app


def admin_login(client, path="/api/auth/login"):
    response = client.post(path, json={"username": "manager", "password": "admin-test-password"})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def put_cookie(client, name, raw):
    client.cookies.clear()
    client.cookies.set(name, raw, domain="testserver.local", path="/")


def test_access_tokens_reject_cross_service_use_without_changing_role_checks(monkeypatch, tmp_path):
    public, _, public_admin, teacher = accounts()
    private = private_app(monkeypatch, tmp_path)
    private_admin = admin_login(private)
    assert decode_token(public_admin)["session_scope"] == "public"
    assert decode_token(private_admin)["session_scope"] == "private-admin"
    assert public.get("/auth/me", headers=headers(public_admin)).status_code == 200
    assert private.get("/api/admin/users", headers=headers(private_admin)).status_code == 200
    assert public.get("/auth/me", headers=headers(private_admin)).status_code == 401
    for token in (public_admin, teacher):
        assert private.get("/api/auth/me", headers=headers(token)).status_code == 401
        assert private.get("/api/admin/users", headers=headers(token)).status_code == 401
        assert private.post("/api/auth/activity", headers=headers(token)).status_code == 401
    assert public.post("/auth/activity", headers=headers(private_admin)).status_code == 401
    private.cookies.clear()
    assert private.post("/api/auth/activity", headers=headers(private_admin)).status_code == 200
    assert private.post("/api/auth/login", json={"username": "teacher", "password": "teacher-test-password"}).status_code == 403


def test_wrong_scope_refresh_cannot_rotate_or_revoke_the_other_session(monkeypatch, tmp_path):
    public, _, _, _ = accounts()
    public_admin = admin_login(public, "/auth/login")
    public_raw = public.cookies.get(settings.refresh_cookie_name)
    private = private_app(monkeypatch, tmp_path)
    private_admin = admin_login(private)
    private_raw = private.cookies.get("smartai_admin_refresh")
    assert public_raw.startswith("public.") and private_raw.startswith("private-admin.")
    put_cookie(private, "smartai_admin_refresh", public_raw)
    assert private.post("/api/auth/refresh").status_code == 401
    assert private.post("/api/auth/logout", headers=headers(private_admin)).status_code == 200
    refreshed_public = public.post("/auth/refresh")
    assert refreshed_public.status_code == 200
    public_admin = refreshed_public.json()["token"]
    # Logout revokes its own bearer session even when its cookie is missing or
    # replaced by a different service's cookie. The public session survived.
    put_cookie(private, "smartai_admin_refresh", private_raw)
    assert private.post("/api/auth/refresh").status_code == 401
    private_admin = admin_login(private)
    private_raw = private.cookies.get("smartai_admin_refresh")
    put_cookie(public, settings.refresh_cookie_name, private_raw)
    assert public.post("/auth/refresh").status_code == 401
    assert public.post("/auth/logout", headers=headers(public_admin)).status_code == 200
    # Prefix alteration cannot select the persisted hash of the private raw.
    put_cookie(public, settings.refresh_cookie_name, private_raw.replace("private-admin.", "public.", 1))
    assert public.post("/auth/refresh").status_code == 401
    put_cookie(private, "smartai_admin_refresh", private_raw)
    refreshed = private.post("/api/auth/refresh")
    assert refreshed.status_code == 200
    assert decode_token(refreshed.json()["token"])["session_scope"] == "private-admin"
    assert private.cookies.get("smartai_admin_refresh").startswith("private-admin.")


@pytest.mark.parametrize("role", ["teacher", "admin"])
def test_legacy_access_only_preserves_public_teacher_sessions(monkeypatch, tmp_path, role):
    public, _, admin, teacher = accounts()
    payload = decode_token(teacher if role == "teacher" else admin)
    payload.pop("session_scope")
    legacy = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    private = private_app(monkeypatch, tmp_path)
    assert public.get("/auth/me", headers=headers(legacy)).status_code == (200 if role == "teacher" else 401)
    assert private.get("/api/auth/me", headers=headers(legacy)).status_code == 401


@pytest.mark.parametrize("role", ["teacher", "admin"])
def test_legacy_refresh_only_preserves_public_teacher_sessions(monkeypatch, tmp_path, role):
    public, user, _, _ = accounts()
    raw = "legacy-" + uuid.uuid4().hex
    with session_scope() as session:
        session.add(RefreshSessionRecord(id=uuid.uuid4().hex, user_id=user.id if role == "teacher" else "manager", token_hash=hashlib.sha256(raw.encode()).hexdigest(), created_at=time.time(), last_used_at=time.time(), expires_at=time.time() + 86400))
    private = private_app(monkeypatch, tmp_path)
    put_cookie(private, "smartai_admin_refresh", raw)
    assert private.post("/api/auth/refresh").status_code == 401
    put_cookie(public, settings.refresh_cookie_name, raw)
    refreshed = public.post("/auth/refresh")
    assert refreshed.status_code == (200 if role == "teacher" else 401)
    if role == "teacher":
        assert decode_token(refreshed.json()["token"])["session_scope"] == "public"
        assert public.cookies.get(settings.refresh_cookie_name).startswith("public.")


def test_password_change_invalidates_both_scopes(monkeypatch, tmp_path):
    public, _, _, _ = accounts()
    public_admin = admin_login(public, "/auth/login")
    private = private_app(monkeypatch, tmp_path)
    private_admin = admin_login(private)
    private_raw = private.cookies.get("smartai_admin_refresh")
    response = private.post("/api/auth/password-change", headers=headers(private_admin), json={"current_password": "admin-test-password", "new_password": "replacement-test-password"})
    assert response.status_code == 200, response.text
    assert public.get("/auth/me", headers=headers(public_admin)).status_code == 401
    assert private.get("/api/auth/me", headers=headers(private_admin)).status_code == 401
    assert public.post("/auth/refresh").status_code == 401
    put_cookie(private, "smartai_admin_refresh", private_raw)
    assert private.post("/api/auth/refresh").status_code == 401
