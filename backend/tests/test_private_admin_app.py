from fastapi.testclient import TestClient
from backend.tests.test_admin_account_lifecycle import accounts, headers


def private_app(monkeypatch, tmp_path):
    monkeypatch.setenv("SMARTAI_ADMIN_PRIVATE_ENABLED", "true")
    monkeypatch.setenv("SMARTAI_ADMIN_DIST", str(tmp_path))
    (tmp_path / "admin.html").write_text("<html><body>synthetic administrator SPA</body></html>")
    from backend.private_main import create_private_app
    return TestClient(create_private_app())


def test_private_api_spa_auth_boundary_and_independent_cookie(monkeypatch, tmp_path):
    _, user, _, _ = accounts()
    client = private_app(monkeypatch, tmp_path)
    login = client.post("/api/auth/login", json={"username": "manager", "password": "admin-test-password"})
    assert login.status_code == 200
    assert "smartai_admin_refresh=" in login.headers["set-cookie"]
    assert client.post("/api/auth/refresh").status_code == 200
    assert "synthetic administrator SPA" in client.get("/admin/users").text
    assert client.get("/api/admin/users", headers=headers(login.json()["token"])).status_code == 200
    assert client.post("/api/auth/register/request", json={}).status_code == 404
    assert client.post("/api/admin/invites", json={}).status_code == 404
    assert client.get("/api/missing").status_code == 404
    assert client.post("/api/auth/login", json={"username": "teacher", "password": "teacher-test-password"}).status_code == 403


def test_ready_rejects_schema_without_migration_evidence(monkeypatch, tmp_path):
    client = private_app(monkeypatch, tmp_path)
    # The unit fixture creates tables directly; SELECT 1 alone would pass.
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 503


def test_bootstrap_never_overwrites_existing_admin_or_identity(monkeypatch):
    from scripts.create_admin import bootstrap_admin
    from backend.auth import hash_password
    from backend.db.auth_repository import register_without_invite
    import pytest
    monkeypatch.setattr("backend.db.session.admin_schema_ready", lambda: True)
    register_without_invite(username="taken", email="taken@example.edu", password_hash=hash_password("old-test-password"))
    with pytest.raises(ValueError): bootstrap_admin("taken", "other@example.edu", "new-test-password")
    bootstrap_admin("first-manager", "first@example.edu", "new-test-password")
    with pytest.raises(ValueError): bootstrap_admin("second-manager", "second@example.edu", "new-test-password")


def test_private_observations_and_preview_enforce_admin(monkeypatch, tmp_path):
    client, user, admin, teacher = accounts()
    private = private_app(monkeypatch, tmp_path)
    for path in ("/api/admin/monitoring", "/api/admin/maintenance/preview"):
        assert private.get(path).status_code == 401
        assert private.get(path, headers=headers(teacher)).status_code == 403
        assert private.get(path, headers=headers(admin)).status_code == 200
    assert private.post("/api/admin/maintenance/execute", headers=headers(admin)).status_code == 404


def test_service_lifetime_holds_maintenance_lock_through_shutdown(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from backend.services.admin_lifecycle import install_maintenance_lifespan
    from backend.services.admin_reset import ResetError, _process_lock, scope_from_settings
    import pytest
    monkeypatch.setenv("SMARTAI_ADMIN_MAINTENANCE_DIR", str(tmp_path / "control"))
    service = FastAPI()
    phases = []
    def exclusive_must_fail():
        with pytest.raises(ResetError, match="reset_services_or_executor_running"):
            with _process_lock(scope_from_settings()): pass
        phases.append("locked")
    service.router.add_event_handler("startup", exclusive_must_fail)
    service.router.add_event_handler("shutdown", exclusive_must_fail)
    install_maintenance_lifespan(service)
    with TestClient(service): exclusive_must_fail()
    assert phases == ["locked"] * 3
    with _process_lock(scope_from_settings()): pass


def test_existing_private_session_can_sign_out_after_read_only_restriction(monkeypatch, tmp_path):
    _, _, _, _ = accounts()
    client = private_app(monkeypatch, tmp_path)
    result = client.post("/api/auth/login", json={"username": "manager", "password": "admin-test-password"})
    from backend.db.session import session_scope
    from backend.db.models import UserRecord
    with session_scope() as session: session.get(UserRecord, "manager").is_read_only = True
    assert client.post("/api/auth/logout", headers=headers(result.json()["token"])).status_code == 200
