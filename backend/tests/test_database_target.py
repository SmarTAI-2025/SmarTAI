"""Runtime and Alembic must operate on the same isolated database target."""
from __future__ import annotations

import os
import traceback
import uuid
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from backend.config import Settings
from backend.db import session as db_session


REPO_ROOT = Path(__file__).resolve().parents[2]
PG_URL = os.environ.get("SMARTAI_TEST_POSTGRES_URL")


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Override the suite's DB reset; this module owns every database it uses."""
    for key in tuple(os.environ):
        if key.startswith("SMARTAI_DATABASE"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)  # no developer .env or relative database paths
    monkeypatch.setattr(db_session, "settings", Settings(_env_file=None))
    # Restore the prior engine/cache without disposing another test's engine.
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_database_url", "")
    monkeypatch.setattr(db_session, "_session_factory", None)
    yield
    if db_session._engine is not None:
        db_session._engine.dispose()


def _config(**attributes) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"), output_buffer=StringIO())
    cfg.set_main_option("script_location", str(REPO_ROOT / "backend/db/migrations"))
    cfg.attributes.update(attributes)
    return cfg


def _load(monkeypatch, **environment) -> Settings:
    for name, value in environment.items():
        monkeypatch.setenv("SMARTAI_" + name, value)
    config = Settings(_env_file=None)
    monkeypatch.setattr(db_session, "settings", config)
    return config


def _prove_same_database(target: str, other: str, cfg: Config) -> None:
    sentinel = create_engine(other)
    try:
        with sentinel.begin() as connection:
            connection.execute(text("CREATE TABLE untouched (value INTEGER)"))
            connection.execute(text("INSERT INTO untouched VALUES (17)"))
        # Exercise the real process-wide engine and session factory, before and
        # after actual Alembic DDL, without Base.metadata.create_all().
        runtime = db_session.get_engine()
        with runtime.begin() as connection:
            connection.execute(text("CREATE TABLE runtime_marker (value INTEGER)"))
            connection.execute(text("INSERT INTO runtime_marker VALUES (23)"))
        command.upgrade(cfg, "head")
        with db_session.session_scope() as session:
            assert session.execute(text("SELECT value FROM runtime_marker")).scalar_one() == 23
            assert session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            session.execute(text(
                "INSERT INTO users (id, username, role, password_hash, is_active, created_at, updated_at) "
                "VALUES ('target-test', 'target-test', 'teacher', 'fake-hash', true, 1, 1)"
            ))
        independent = create_engine(target)
        try:
            with independent.connect() as connection:
                assert connection.execute(text("SELECT COUNT(*) FROM users")).scalar_one() == 1
        finally:
            independent.dispose()
        assert set(inspect(sentinel).get_table_names()) == {"untouched"}
        with sentinel.connect() as connection:
            assert connection.execute(text("SELECT value FROM untouched")).scalar_one() == 17
    finally:
        sentinel.dispose()


@pytest.mark.parametrize("case", ["default", "light", "legacy", "same", "conflict", "opposite-legacy"])
def test_sqlite_runtime_and_migration_share_target(tmp_path, monkeypatch, case):
    target = "sqlite:///data/smartai.db" if case == "default" else f"sqlite:///{tmp_path / 'target%25.db'}"
    other = f"sqlite:///{tmp_path / 'other.db'}"
    environment = {}
    if case != "default":
        environment["DATABASE_HEAVY"] = "OFF"
    if case in {"light", "same", "conflict", "opposite-legacy"}:
        environment["DATABASE_URL_LIGHT"] = target
    if case in {"legacy", "same"}:
        environment["DATABASE_URL"] = target
    elif case == "conflict":
        environment["DATABASE_URL"] = other
    elif case == "opposite-legacy":
        environment["DATABASE_URL"] = "postgresql+psycopg://unused.invalid/unused"
    _load(monkeypatch, **environment)
    cfg = _config()
    # A stale ini URL must never become a second production selection rule.
    cfg.set_main_option("sqlalchemy.url", other)
    _prove_same_database(target, other, cfg)


@pytest.mark.parametrize("case", ["heavy", "legacy", "same", "conflict"])
def test_postgres_runtime_and_migration_share_target(monkeypatch, case):
    if not PG_URL:
        pytest.skip("SMARTAI_TEST_POSTGRES_URL is required for isolated PostgreSQL execution")
    admin = create_engine(PG_URL)
    schemas = ["db_target_" + uuid.uuid4().hex for _ in range(2)]
    urls = [make_url(PG_URL).update_query_dict({"options": f"-csearch_path={schema}"})
            .render_as_string(hide_password=False) for schema in schemas]
    target, other = urls
    try:
        with admin.begin() as connection:
            for schema in schemas:
                connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        environment = {"DATABASE_HEAVY": "ON"}
        if case != "legacy":
            environment["DATABASE_URL_HEAVY"] = target
        if case in {"legacy", "same"}:
            environment["DATABASE_URL"] = target
        elif case == "conflict":
            environment["DATABASE_URL"] = other
        _load(monkeypatch, **environment)
        _prove_same_database(target, other, _config())
    finally:
        if db_session._engine is not None:
            db_session._engine.dispose()
        with admin.begin() as connection:
            for schema in schemas:
                connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()


@pytest.mark.parametrize("environment, message", [
    ({"DATABASE_HEAVY": "ON"}, "missing"),
    ({"DATABASE_HEAVY": "ON", "DATABASE_URL_HEAVY": "sqlite:///unused.db"}, "PostgreSQL"),
    ({"DATABASE_URL_LIGHT": "postgresql://u:fake-password@unused.invalid/db"}, "SQLite"),
    ({"DATABASE_HEAVY": "ON", "DATABASE_URL": "sqlite:///unused.db"}, "PostgreSQL"),
    ({"DATABASE_URL": "postgresql://u:fake-password@unused.invalid/db"}, "SQLite"),
    ({"DATABASE_URL_LIGHT": "not-a-url-fake-password"}, "Invalid database URL"),
    ({"DATABASE_URL_LIGHT": "sqlite://u:fake-password@unused.invalid/db"}, "Invalid SQLite"),
    ({"DATABASE_HEAVY": "ON", "DATABASE_URL_HEAVY": "postgresql://u:fake-password@host:bad/db"}, "Invalid database URL"),
    ({"DATABASE_URL_LIGHT": "sqlitefake:///unused.db"}, "SQLite"),
])
def test_invalid_targets_fail_safely_before_connecting(monkeypatch, capsys, caplog, environment, message):
    _load(monkeypatch, **environment)
    for operation in (db_session.get_engine, lambda: command.upgrade(_config(), "head")):
        with pytest.raises(RuntimeError, match=message) as caught:
            operation()
        rendered = "".join(traceback.format_exception(caught.value))
        assert "fake-password" not in rendered
        assert "postgresql://u:" not in rendered
        assert db_session._engine is None
    output = capsys.readouterr()
    assert "fake-password" not in output.out + output.err + caplog.text


def test_explicit_alembic_target_overrides_cached_config_without_mutating_it(tmp_path, monkeypatch):
    target = f"sqlite:///{tmp_path / 'explicit.db'}"
    settings = _load(monkeypatch, DATABASE_HEAVY="ON", DATABASE_URL_HEAVY="postgresql://unused.invalid/db")
    cfg = _config(database_url=target, database_heavy=False)
    command.upgrade(cfg, "head")
    engine = create_engine(target)
    try:
        assert "users" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
    assert settings.database_heavy is True
    assert settings.database_url == "postgresql://unused.invalid/db"
    assert db_session._engine is None


def test_empty_explicit_target_does_not_fall_back_to_application_database(tmp_path, monkeypatch):
    configured_path = tmp_path / "must-not-open.db"
    _load(monkeypatch, DATABASE_URL_LIGHT=f"sqlite:///{configured_path}")
    with pytest.raises(RuntimeError, match="missing"):
        db_session.configure_database("")
    with pytest.raises(RuntimeError, match="missing"):
        command.upgrade(_config(database_url="", database_heavy=False), "head")
    assert not configured_path.exists()


def test_offline_uses_shared_target_and_explicit_mode(monkeypatch):
    _load(monkeypatch, DATABASE_HEAVY="ON", DATABASE_URL_HEAVY="postgresql://unused.invalid/db",
          DATABASE_URL="sqlite:///stale.db")
    cfg = _config()
    command.upgrade(cfg, "head", sql=True)
    assert "CREATE TABLE users" in cfg.output_buffer.getvalue()
    cfg = _config(database_url="sqlite:///explicit.db", database_heavy=False)
    command.upgrade(cfg, "0001_normalized_learning", sql=True)
    assert "CREATE TABLE users" in cfg.output_buffer.getvalue()


def test_legacy_environment_override_of_dotenv_pair_is_preserved(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("SMARTAI_DATABASE_URL_LIGHT=sqlite:///from-file.db\n")
    monkeypatch.setenv("SMARTAI_DATABASE_URL", "sqlite:///from-process.db")
    assert Settings(_env_file=env_file).database_url == "sqlite:///from-process.db"
    monkeypatch.setenv("SMARTAI_DATABASE_URL_LIGHT", "sqlite:///explicit-light.db")
    assert Settings(_env_file=env_file).database_url == "sqlite:///explicit-light.db"


def test_settings_snapshot_is_shared_until_explicitly_reloaded(tmp_path, monkeypatch):
    target = f"sqlite:///{tmp_path / 'original.db'}"
    other = f"sqlite:///{tmp_path / 'late-env.db'}"
    _load(monkeypatch, DATABASE_URL=target)
    monkeypatch.setenv("SMARTAI_DATABASE_URL", other)
    _prove_same_database(target, other, _config())
    monkeypatch.delenv("SMARTAI_DATABASE_URL")
    assert Settings(_env_file=None).database_url == "sqlite:///data/smartai.db"
    assert Settings(_env_file=None).database_heavy is False
