from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


REPO_ROOT = Path(__file__).resolve().parents[2]


def _config(db_url: str, monkeypatch) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", str(REPO_ROOT / "backend/db/migrations")
    )
    monkeypatch.setenv("SMARTAI_DATABASE_URL", db_url)
    monkeypatch.setenv("SMARTAI_DATABASE_HEAVY", "OFF")
    return config


def test_knowledge_storage_migration_backfills_retained_bytes_and_roundtrips(
    tmp_path, monkeypatch,
):
    db_url = f"sqlite:///{(tmp_path / 'knowledge-migration.db').as_posix()}"
    config = _config(db_url, monkeypatch)
    command.upgrade(config, "0015_source_file_lifecycle")
    engine = create_engine(db_url)
    digest = "a" * 64
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users "
            "(id, username, role, password_hash, is_active, created_at, updated_at) "
            "VALUES ('owner', 'owner', 'teacher', 'x', 1, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO knowledge_documents "
            "(id, owner_id, stored_file_id, title, original_name, content_type, "
            "size_bytes, sha256, status, parser_version, chunk_count, created_at, updated_at) "
            "VALUES ('legacy-doc', 'owner', NULL, 'Legacy', 'legacy.pdf', "
            "'application/pdf', 7, :digest, 'ready', 'v1', 0, 1, 2)"
        ), {"digest": digest})
        connection.execute(text(
            "INSERT INTO stored_files "
            "(id, owner_id, kind, original_name, storage_backend, storage_key, "
            "content_type, size_bytes, sha256, source_quota_bytes, "
            "availability_status, lifecycle_revision, cleanup_attempt_count, "
            "knowledge_document_id, created_at) "
            "VALUES ('legacy-file', 'owner', 'personal_knowledge', 'legacy.pdf', "
            "'local', 'users/owner/knowledge/legacy.pdf', 'application/pdf', 7, "
            ":digest, 0, 'available', 0, 0, 'legacy-doc', 1)"
        ), {"digest": digest})
        connection.execute(text(
            "UPDATE knowledge_documents SET stored_file_id='legacy-file' "
            "WHERE id='legacy-doc'"
        ))

    command.upgrade(config, "0016_knowledge_storage_quota")
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT owner_id, document_id, stored_file_id, retention_policy, "
            "state, size_bytes, sha256, storage_key "
            "FROM knowledge_storage_records"
        )).one()
    assert row == (
        "owner", "legacy-doc", "legacy-file", "retained", "available", 7,
        digest, "users/owner/knowledge/legacy.pdf",
    )

    indexes = {
        item["name"]: item
        for item in inspect(engine).get_indexes("knowledge_storage_records")
    }
    assert indexes["ix_knowledge_storage_records_document_id"]["unique"] == 1
    assert indexes["ix_knowledge_storage_records_stored_file_id"]["unique"] == 1
    assert indexes["ix_knowledge_storage_records_cleanup_operation_id"]["unique"] == 1
    inspector = inspect(engine)
    assert "knowledge_storage_orphan_guards" in inspector.get_table_names()
    assert inspector.get_foreign_keys("knowledge_storage_orphan_guards") == []
    ledger_columns = {
        column["name"] for column in inspector.get_columns("knowledge_storage_records")
    }
    assert {
        "writer_claim_token",
        "writer_claimed_at",
        "writer_heartbeat_at",
        "writer_lease_expires_at",
    } <= ledger_columns

    command.downgrade(config, "0015_source_file_lifecycle")
    downgraded_tables = inspect(engine).get_table_names()
    assert "knowledge_storage_records" not in downgraded_tables
    assert "knowledge_storage_orphan_guards" not in downgraded_tables
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT stored_file_id FROM knowledge_documents WHERE id='legacy-doc'"
        )).scalar_one() == "legacy-file"
    command.upgrade(config, "head")
    assert "knowledge_storage_records" in inspect(engine).get_table_names()


def test_knowledge_storage_model_uses_unique_indexes_without_duplicate_constraints():
    from backend.db.models import (
        KnowledgeStorageOrphanGuardRecord,
        KnowledgeStorageRecord,
    )

    table = KnowledgeStorageRecord.__table__
    unique_indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in table.indexes
        if index.unique
    }
    unique_constraints = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert unique_indexes == {
        "ix_knowledge_storage_records_cleanup_operation_id": (
            "cleanup_operation_id",
        ),
        "ix_knowledge_storage_records_document_id": ("document_id",),
        "ix_knowledge_storage_records_stored_file_id": ("stored_file_id",),
    }
    assert unique_constraints == {
        ("owner_id", "sha256"),
        ("storage_key",),
    }
    guard_table = KnowledgeStorageOrphanGuardRecord.__table__
    assert tuple(guard_table.foreign_keys) == ()
    assert {
        tuple(column.name for column in constraint.columns)
        for constraint in guard_table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    } == {("storage_key",)}
