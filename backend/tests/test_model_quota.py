"""Real isolated DB concurrency; synthetic providers, no paid model calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import subprocess
import sys
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from backend.config import settings
from backend.db.session import session_scope, configure_database
from backend.db.business_config_models import BusinessConfigRecord
from backend.db.models import AdminAuditLogRecord
from backend.services.model_quota import admit_model_call, ModelQuotaError, model_usage
from backend.tests.test_admin_business_config import client, headers, save

def usage():
    with session_scope() as session:
        return model_usage(session, "teacher")

def test_compatibility_and_atomic_concurrent_limits(client, monkeypatch):
    monkeypatch.setattr(settings, "shared_pool_daily_request_limit", 7)
    monkeypatch.setattr(settings, "shared_pool_daily_estimated_token_limit", 15)
    def call(_):
        try:
            admit_model_call("teacher", "shared", estimated_input_tokens=3)
            return True
        except ModelQuotaError:
            return False
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(call, range(36))) == 5
    assert usage()["shared"]["requests"] == 5
    assert usage()["shared"]["estimated_input_tokens"] == 15
    configure_database().dispose()
    with pytest.raises(ModelQuotaError):
        admit_model_call("teacher", "shared", estimated_input_tokens=1)

def test_global_user_override_inheritance_audit_and_reduced_limit(client):
    assert save(client, {"shared_pool_daily_request_limit": 2}).status_code == 200
    admit_model_call("teacher", "shared")
    assert save(client, {"shared_pool_daily_request_limit": 0}, user="teacher").status_code == 200
    with pytest.raises(ModelQuotaError): admit_model_call("teacher", "shared")
    assert usage()["shared"]["requests"] == 1
    assert save(client, {"shared_pool_daily_request_limit": -1}, user="teacher").status_code == 200
    for _ in range(3): admit_model_call("teacher", "shared")
    assert save(client, {"shared_pool_daily_request_limit": None}, user="teacher").status_code == 200
    assert usage()["shared"]["request_limit"] == 2
    with pytest.raises(ModelQuotaError): admit_model_call("teacher", "shared")
    with session_scope() as session:
        audits = list(session.scalars(select(AdminAuditLogRecord)))
        assert len(audits) == 4
        assert any("shared_pool_daily_request_limit" in str(row.after_state) for row in audits)
    path="/admin/business-config"
    payload={"expected_version": 1, "changes": {"history_query_llm_daily_limit": -2}, "reason":"test"}
    assert client.patch(path, json=payload, headers=headers()).status_code == 422
    assert client.patch(path, json=payload, headers=headers("teacher")).status_code == 403

def test_utc_reset_and_persistent_history_cooldown(client, monkeypatch):
    monkeypatch.setattr(settings, "history_query_llm_cooldown_seconds", 3)
    assert save(client, {"history_query_llm_daily_limit": 1}).status_code == 200
    before=datetime(2026,10,3,23,59,59,tzinfo=timezone.utc)
    admit_model_call("teacher", "history", now=before)
    configure_database().dispose()
    with pytest.raises(ModelQuotaError, match="cooldown"):
        admit_model_call("teacher", "history", now=before+timedelta(seconds=1))
    admit_model_call("teacher", "history", now=before+timedelta(seconds=4))
    with pytest.raises(ModelQuotaError, match="daily_limit"):
        admit_model_call("teacher", "history", now=before+timedelta(seconds=9))

def test_failure_sdk_retry_and_cancel_remain_charged(client, monkeypatch):
    from backend.llm.registry import _GuardedSharedProvider, SharedPoolLimitError
    monkeypatch.setattr(settings, "shared_pool_enabled", True)
    assert save(client, {"shared_pool_daily_request_limit": 3}).status_code == 200
    class Fake:
        provider_id="synthetic"; provider_type="openai"; model="fake"; config=None
        calls=0
        async def ainvoke(self, messages):
            self.calls+=3  # Three SDK attempts are still one admitted call.
            if getattr(self, "cancel", False):
                raise asyncio.CancelledError()
            raise RuntimeError("fake failure")
    fake=Fake(); guarded=_GuardedSharedProvider(fake,"teacher")
    for _ in range(2):
        with pytest.raises(RuntimeError, match="fake failure"): asyncio.run(guarded.ainvoke([]))
    fake.cancel = True
    with pytest.raises(asyncio.CancelledError): asyncio.run(guarded.ainvoke([]))
    with pytest.raises(SharedPoolLimitError): asyncio.run(guarded.ainvoke([]))
    assert fake.calls == 9 and usage()["shared"]["requests"] == 3

def test_processes_cannot_replenish_or_bypass_allowance(client):
    assert save(client, {"shared_pool_daily_request_limit": 2}).status_code == 200
    code="""import sys
from backend.services.model_quota import admit_model_call, ModelQuotaError
try: admit_model_call('teacher','shared')
except ModelQuotaError: sys.exit(7)
"""
    env=dict(os.environ, SMARTAI_DATABASE_URL=settings.database_url, SMARTAI_DATABASE_HEAVY="false")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _: subprocess.run([sys.executable,"-c",code], env=env, capture_output=True), range(4)))
    assert sorted(r.returncode for r in results) == [0,0,7,7], [r.stderr.decode() for r in results]
    assert usage()["shared"]["requests"] == 2

def test_legacy_negative_is_zero_and_readonly_running_work_is_not_cancelled(client, monkeypatch):
    from backend.db.models import UserRecord
    monkeypatch.setattr(settings,"shared_pool_daily_request_limit",-1)
    assert usage()["shared"]["request_limit"] == 0
    with pytest.raises(ModelQuotaError): admit_model_call("teacher","shared")
    assert save(client, {"shared_pool_daily_request_limit": -1}, user="teacher").status_code == 200
    with session_scope() as session: session.get(UserRecord,"teacher").is_read_only=True
    admit_model_call("teacher","shared")
    assert usage()["shared"]["requests"] == 1
    with pytest.raises(ModelQuotaError,match="owner_unavailable"): admit_model_call("deleted","shared")

def test_previous_admin_schema_upgrade_preserves_config_and_user(tmp_path, monkeypatch):
    from alembic import command
    from backend.tests.test_migration_roundtrip import _alembic_config
    from sqlalchemy import create_engine, text, inspect
    url=f"sqlite:///{tmp_path / 'prior-admin.db'}"
    cfg=_alembic_config(url,monkeypatch)
    command.upgrade(cfg,"0023_business_configuration")
    engine=create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,auth_version,is_read_only,created_at,updated_at) VALUES('old','old','legacy','teacher',true,3,false,1,1)"))
        connection.execute(text("INSERT INTO business_configuration(id,overrides,version,registration_rules_managed,updated_at) VALUES('global','{}',4,false,1)"))
    command.upgrade(cfg,"head")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT auth_version FROM users WHERE id='old'")).scalar_one()==3
        assert connection.execute(text("SELECT version FROM business_configuration")).scalar_one()==4
        assert connection.execute(text("SELECT count(*) FROM model_daily_usage")).scalar_one()==0
    assert "model_daily_usage" in inspect(engine).get_table_names()
