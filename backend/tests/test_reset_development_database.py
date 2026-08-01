from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from scripts import reset_development_database


def test_reset_refuses_non_sqlite_database(capsys):
    result = reset_development_database.main(
        ["--database", "postgresql+psycopg://smartai:smartai@localhost/smartai"]
    )

    assert result == 2
    assert "SQLite" in capsys.readouterr().err


def test_reset_refuses_sqlite_path_outside_repo_data_without_force(tmp_path, capsys):
    database_path = tmp_path / "outside.db"
    database_path.write_text("do not delete", encoding="utf-8")

    result = reset_development_database.main(["--database", str(database_path)])

    assert result == 2
    assert database_path.read_text(encoding="utf-8") == "do not delete"
    assert "--force-explicit-path" in capsys.readouterr().err


def test_forced_reset_uses_alembic_and_preserves_storage(tmp_path, monkeypatch):
    from backend.config import settings

    monkeypatch.setattr(settings, "database_heavy", True)
    database_path = tmp_path / "reset.db"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE stale_table (id INTEGER PRIMARY KEY)"))
    engine.dispose()
    storage_file = tmp_path / "uploads" / "keep.txt"
    storage_file.parent.mkdir()
    storage_file.write_text("keep", encoding="utf-8")

    result = reset_development_database.main(
        ["--database", str(database_path), "--force-explicit-path"]
    )

    assert result == 0
    tables = set(inspect(create_engine(f"sqlite:///{database_path.as_posix()}")).get_table_names())
    assert {"alembic_version", "users", "assignments", "submissions"} <= tables
    assert "stale_table" not in tables
    assert storage_file.read_text(encoding="utf-8") == "keep"
    assert settings.database_heavy is True
