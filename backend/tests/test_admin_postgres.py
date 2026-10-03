"""Run only against the explicitly provided disposable PostgreSQL database."""
import os
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import create_engine, text
from backend.tests.test_postgres_integration import pg_database

pytestmark = pytest.mark.skipif(not os.environ.get("SMARTAI_TEST_POSTGRES_URL"), reason="Requires disposable PostgreSQL")


@pytest.mark.parametrize("previous", ["base", "0018_knowledge_ingestion", "0019_admin_usage_events", "0022_account_closures", "0023_business_configuration"])
def test_existing_postgres_branches_upgrade_with_legacy_identity(pg_database, monkeypatch, previous):
    from alembic import command
    from alembic.config import Config
    from backend.db.session import get_engine, admin_schema_ready
    from backend.auth import hash_password
    from fastapi.testclient import TestClient
    from backend.main import app
    engine = get_engine()
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    config = Config("alembic.ini")
    if previous != "base":
        command.upgrade(config, previous)
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO users (id,username,email,role,password_hash,is_active,created_at,updated_at) VALUES ('legacy','legacy','legacy@example.edu','teacher',:password,true,1,1)"), {"password": hash_password("legacy-test-password")})
    command.upgrade(config, "head")
    assert admin_schema_ready()
    if previous != "base":
        client = TestClient(app)
        result = client.post("/auth/login", json={"username": "legacy", "password": "legacy-test-password"})
        assert result.status_code == 200, result.text
        assert result.json()["user"]["is_read_only"] is False
        from backend.auth import create_token
        token = create_token("legacy", "teacher")
        assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_two_admins_cannot_concurrently_remove_all_management(pg_database):
    from backend.tests.test_admin_account_lifecycle import accounts, headers
    from backend.state import get_user_store
    from backend.models import User
    from backend.auth import hash_password
    from backend.db.session import session_scope
    from backend.db.models import UserRecord
    from sqlalchemy import select
    client, _, token_a, _ = accounts()
    get_user_store()["manager-b"] = User(id="manager-b", username="manager-b", email="b@example.edu", role="admin", password_hash=hash_password("second-admin-password"))
    token_b = client.post("/auth/login", json={"username": "manager-b", "password": "second-admin-password"}).json()["token"]
    def restrict(token, target):
        return client.patch(f"/admin/users/{target}/access", json={"access": "login_blocked", "reason": "concurrency_test"}, headers=headers(token)).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(restrict, token_a, "manager-b")
        b = pool.submit(restrict, token_b, "manager")
        statuses = [a.result(timeout=15), b.result(timeout=15)]
    assert statuses.count(200) == 1, statuses
    assert all(status in {200,401,403,409} for status in statuses)
    with session_scope() as session:
        assert len(list(session.scalars(select(UserRecord.id).where(UserRecord.role == "admin", UserRecord.is_active.is_(True), UserRecord.is_read_only.is_(False))))) == 1


def test_business_config_concurrency_live_quota_and_private_permissions(pg_database, monkeypatch, tmp_path):
    from backend.tests.test_admin_account_lifecycle import accounts, headers
    from backend.tests.test_private_admin_app import private_app
    from backend.db.knowledge_storage_repository import knowledge_storage_usage
    _, user, admin, teacher = accounts()
    client = private_app(monkeypatch, tmp_path)
    from backend.auth import create_token
    admin = client.post("/api/auth/login", json={"username": "manager", "password": "admin-test-password"}).json()["token"]
    path = "/api/admin/business-config"
    assert client.get(path, headers=headers(teacher)).status_code == 401
    assert client.get(path, headers=headers(create_token(user.id, "teacher", session_scope="private-admin"))).status_code == 403
    def save(value):
        return client.patch(path, json={"changes": {"knowledge_storage_quota_bytes": value},
            "expected_version": 0, "reason": "isolated_postgres_concurrency"}, headers=headers(admin)).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, [10, 20])) == [200, 409]
    view = client.get(path, headers=headers(admin)).json()
    global_limit = view["fields"]["knowledge_storage_quota_bytes"]["effective"]
    assert knowledge_storage_usage(user.id).limit_bytes == global_limit
    result = client.patch(path + "/users/" + user.id, json={
        "changes": {"knowledge_storage_quota_bytes": 0}, "expected_version": 0,
        "expected_global_version": view["version"], "reason": "isolated_user_quota"}, headers=headers(admin))
    assert result.status_code == 200, result.text
    assert knowledge_storage_usage(user.id).limit_bytes == 0
    audit = client.get("/api/admin/audit", headers=headers(admin)).json()
    assert all(item["actor_name"] == "manager" for item in audit)
    assert {item["action"] for item in audit} >= {"business_configuration_changed", "user_storage_configuration_changed"}
    assert next(item for item in audit if item["action"] == "user_storage_configuration_changed")["after_state"]["overrides"] == {"knowledge_storage_quota_bytes": 0}


def test_model_daily_admission_serializes_postgres_sessions(pg_database, monkeypatch):
    from backend.db.session import session_scope
    from backend.db.models import UserRecord
    from backend.db.business_config_models import BusinessConfigRecord
    from backend.services.model_quota import admit_model_call, ModelQuotaError, model_usage
    import time
    with session_scope() as session:
        session.add(UserRecord(id="quota-owner",username="quota-owner",role="teacher",password_hash="fake",is_active=True))
        session.add(BusinessConfigRecord(id="global",overrides={"shared_pool_daily_request_limit":7},version=1,updated_at=time.time()))
    def call(_):
        try:admit_model_call("quota-owner","shared");return True
        except ModelQuotaError:return False
    with ThreadPoolExecutor(max_workers=12) as pool:assert sum(pool.map(call,range(32)))==7
    with session_scope() as session:assert model_usage(session,"quota-owner")["shared"]["requests"]==7
