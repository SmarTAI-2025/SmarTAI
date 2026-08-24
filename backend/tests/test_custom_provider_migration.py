from __future__ import annotations

import os
import uuid
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(db_url: str, monkeypatch) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option(
        "script_location", str(REPO_ROOT / "backend/db/migrations")
    )
    monkeypatch.setenv("SMARTAI_DATABASE_URL", db_url)
    monkeypatch.setenv("SMARTAI_DATABASE_HEAVY", "OFF")
    return cfg


def test_migration_preserves_ustc_as_enabled_deepseek(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text

    db_url = f"sqlite:///{(tmp_path / 'relay-migration.db').as_posix()}"
    cfg = _alembic_config(db_url, monkeypatch)
    command.upgrade(cfg, "0007_source_outcome_diagnostics")
    engine = create_engine(db_url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users "
            "(id, username, role, password_hash, is_active, created_at, updated_at) "
            "VALUES ('owner', 'owner', 'teacher', 'h', 1, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO provider_configs "
            "(id, owner_id, provider_type, model, base_url, display_name, "
            "encrypted_api_key, nonce, key_version, enabled, max_concurrent, rpm, "
            "created_at, updated_at, verification_status, last_checked_at) VALUES "
            "('official', 'owner', 'deepseek', 'deepseek-chat', "
            "'https://api.deepseek.com', NULL, 'cipher', 'nonce', 1, 1, 5, 0, "
            "1, 1, 'verified', 1), "
            "('ustc', 'owner', 'deepseek', 'school-model', "
            "'https://api.llm.ustc.edu.cn/v1/', NULL, 'cipher', 'nonce', 1, 1, 5, 0, "
            "1, 1, 'verified', 1)"
        ))

    # Simulate a collaborator who already ran the earlier Draft #36 migration.
    command.upgrade(cfg, "0008_custom_provider_endpoints")
    with engine.connect() as connection:
        legacy_ustc = connection.execute(text(
            "SELECT provider_type, enabled FROM provider_configs WHERE id = 'ustc'"
        )).mappings().one()
    assert legacy_ustc["provider_type"] == "openai_compatible"
    assert legacy_ustc["enabled"] == 0

    command.upgrade(cfg, "head")
    assert "lease_token" in {
        column["name"] for column in inspect(engine).get_columns("workflow_operations")
    }
    with engine.connect() as connection:
        rows = {
            row.id: row
            for row in connection.execute(text(
                "SELECT id, provider_type, base_url, endpoint_identity, enabled, "
                "wire_protocol, verification_status FROM provider_configs"
            )).mappings()
        }

    assert rows["official"]["provider_type"] == "deepseek"
    assert rows["official"]["endpoint_identity"] == "https://api.deepseek.com/v1"
    assert rows["official"]["enabled"] == 1
    assert rows["official"]["verification_status"] == "verified"
    assert rows["official"]["wire_protocol"] == "openai_chat_completions"
    assert rows["ustc"]["provider_type"] == "deepseek"
    assert rows["ustc"]["base_url"] == "https://api.llm.ustc.edu.cn/v1"
    assert rows["ustc"]["endpoint_identity"] == "https://api.llm.ustc.edu.cn/v1"
    assert rows["ustc"]["enabled"] == 1
    # The earlier draft intentionally erased this status. The compatibility
    # migration cannot reconstruct it, but unverified no longer blocks use.
    assert rows["ustc"]["verification_status"] == "unverified"
    assert rows["ustc"]["wire_protocol"] == "openai_chat_completions"


def test_migration_allows_same_vendor_model_at_distinct_endpoints(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text

    db_url = f"sqlite:///{(tmp_path / 'relay-identity.db').as_posix()}"
    cfg = _alembic_config(db_url, monkeypatch)
    command.upgrade(cfg, "head")
    engine = create_engine(db_url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users "
            "(id, username, role, password_hash, is_active, created_at, updated_at) "
            "VALUES ('owner', 'owner', 'teacher', 'h', 1, 1, 1)"
        ))
        for record_id, endpoint in (
            ("relay-a", "https://relay-a.example.com/v1"),
            ("relay-b", "https://relay-b.example.com/v1"),
        ):
            connection.execute(text(
                "INSERT INTO provider_configs "
                "(id, owner_id, provider_type, model, base_url, endpoint_identity, wire_protocol, "
                "encrypted_api_key, nonce, key_version, enabled, max_concurrent, rpm, "
                "created_at, updated_at, verification_status) VALUES "
                "(:id, 'owner', 'deepseek', 'same-model', :endpoint, :endpoint, "
                "'openai_chat_completions', "
                "'cipher', 'nonce', 1, 1, 5, 0, 1, 1, 'unverified')"
            ), {"id": record_id, "endpoint": endpoint})

    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM provider_configs"
        )).scalar_one() == 2


def test_postgresql_relay_endpoint_ddl_is_portable(monkeypatch):
    from backend.config import settings

    database_url = "postgresql+psycopg://smartai:smartai@localhost/smartai_test"
    monkeypatch.setenv("SMARTAI_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTAI_DATABASE_HEAVY", "ON")
    monkeypatch.setattr(settings, "database_heavy", True)
    output = StringIO()
    cfg = Config("alembic.ini", output_buffer=output)
    cfg.set_main_option("script_location", "backend/db/migrations")
    command.upgrade(cfg, "head", sql=True)
    sql = output.getvalue()

    assert "ADD COLUMN endpoint_identity VARCHAR(1024)" in sql
    assert "uq_provider_configs_owner_provider_protocol_endpoint_model" in sql
    assert "ADD COLUMN wire_protocol VARCHAR(64)" in sql
    assert (
        "SET provider_type = 'deepseek', enabled = true WHERE provider_type = "
        "'openai_compatible'"
    ) in sql


PG_URL = os.environ.get("SMARTAI_TEST_POSTGRES_URL")


@pytest.fixture
def relay_provider_pg_database():
    if not PG_URL:
        pytest.skip(
            "Set SMARTAI_TEST_POSTGRES_URL to run PostgreSQL integration (GitHub Actions)."
        )

    from backend.config import settings
    from backend.db.session import configure_database
    from sqlalchemy import create_engine

    old_heavy = settings.database_heavy
    old_url = os.environ.get("SMARTAI_DATABASE_URL")
    old_heavy_env = os.environ.get("SMARTAI_DATABASE_HEAVY")
    settings.database_heavy = True
    configure_database(PG_URL)
    engine = create_engine(PG_URL)
    config = Config("alembic.ini")
    config.set_main_option("script_location", "backend/db/migrations")
    os.environ["SMARTAI_DATABASE_URL"] = PG_URL
    os.environ["SMARTAI_DATABASE_HEAVY"] = "ON"
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA public")
        command.upgrade(config, "head")
        yield
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA public")
        engine.dispose()
        settings.database_heavy = old_heavy
        if old_url is None:
            os.environ.pop("SMARTAI_DATABASE_URL", None)
        else:
            os.environ["SMARTAI_DATABASE_URL"] = old_url
        if old_heavy_env is None:
            os.environ.pop("SMARTAI_DATABASE_HEAVY", None)
        else:
            os.environ["SMARTAI_DATABASE_HEAVY"] = old_heavy_env


def test_postgres_endpoint_identity_and_owner_isolation(relay_provider_pg_database):
    from backend.db.models import UserRecord
    from backend.db.provider_repository import list_provider_configs, upsert_provider_config
    from backend.db.session import session_scope
    from backend.models import ProviderConfig

    owner_ids = [f"pg_teacher_{uuid.uuid4().hex[:8]}" for _ in range(2)]
    with session_scope() as session:
        for owner_id in owner_ids:
            session.add(UserRecord(
                id=owner_id,
                username=owner_id,
                role="teacher",
                password_hash="x",
                is_active=True,
            ))
    teacher, other = owner_ids
    records = [
        upsert_provider_config(
            teacher,
            ProviderConfig(
                provider_type="deepseek",
                api_key=f"owner-secret-{suffix}",
                model="shared-model-name",
                base_url=endpoint,
                endpoint_identity=endpoint,
                enabled=True,
            ),
            master_key="postgres-provider-master-key",
        )
        for suffix, endpoint in (
            ("a", "https://relay-a.example.com/v1"),
            ("b", "https://relay-b.example.com/v1"),
        )
    ]

    assert records[0].id != records[1].id
    assert {
        item.config.endpoint_identity
        for item in list_provider_configs(
            teacher, master_key="postgres-provider-master-key"
        )
    } == {
        "https://relay-a.example.com/v1",
        "https://relay-b.example.com/v1",
    }
    assert list_provider_configs(
        other, master_key="postgres-provider-master-key"
    ) == []
