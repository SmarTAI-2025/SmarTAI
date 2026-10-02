"""Disposable fixtures only. No real email, providers or object-storage network."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import MetaData, create_engine, create_mock_engine, inspect, text
from sqlalchemy.engine import make_url

from backend.db.base import Base
from backend.services import admin_reset as reset


def _fixture_metadata():
    # PostgreSQL emits AddConstraint for cyclic FKs. SQLAlchemy deliberately
    # mutates their _create_rule, which would make later SQLite create_all on
    # shared Base.metadata silently omit those FKs. DDL owns a fresh copy.
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    return metadata


@pytest.fixture(params=["sqlite", "postgresql"] if os.environ.get("SMARTAI_RESET_TEST_POSTGRES_URL") else ["sqlite"])
def scope(tmp_path, request):
    database_url = f"sqlite:///{tmp_path / 'business.sqlite'}"
    if request.param == "postgresql":
        database_url = os.environ["SMARTAI_RESET_TEST_POSTGRES_URL"]
        url = make_url(database_url)
        if url.host != "127.0.0.1" or url.database != "smartai_reset_disposable":
            raise RuntimeError("reset tests require their dedicated disposable PostgreSQL database")
    engine = create_engine(database_url)
    if request.param == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    _fixture_metadata().create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('test-reset-head')"))
        connection.execute(text("INSERT INTO users (id, username, email, role, password_hash, is_active, auth_version, created_at, updated_at) VALUES ('test-owner', 'reusable-name', 'reusable@example.invalid', 'admin', 'fake-hash', true, 0, 1, 1)"))
        connection.execute(text("INSERT INTO blocked_registration_emails (normalized_email, created_at) VALUES ('blocked@example.invalid', 1)"))
    from backend.db.models import RefreshSessionRecord, EmailVerificationRequestRecord, PasswordResetRequestRecord, AccountClosureRecord, AdminAuditLogRecord
    with engine.begin() as connection:
        connection.execute(RefreshSessionRecord.__table__.insert().values(id="session", user_id="test-owner", token_hash="a" * 64, expires_at=9))
        connection.execute(EmailVerificationRequestRecord.__table__.insert().values(id="verification", normalized_username="reusable-name", normalized_email="reusable@example.invalid", password_hash="fake-hash", token_digest="b" * 64, expires_at=9, resend_available_at=2))
        connection.execute(PasswordResetRequestRecord.__table__.insert().values(id="reset", user_id="test-owner", token_digest="c" * 64, expires_at=9, resend_available_at=2))
        connection.execute(AccountClosureRecord.__table__.insert().values(id="closure", user_id="test-owner", mode="delete", created_at=1))
        connection.execute(AdminAuditLogRecord.__table__.insert().values(id="audit", actor_id="test-owner", target_user_id="test-owner", action="fixture", reason="fixture", idempotency_key="fixture", request_hash="d" * 64))
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / reset.SENTINEL).write_text(json.dumps(reset.SENTINEL_CONTENT))
    (uploads / "orphan.txt").write_text("disposable orphan")
    nested = uploads / "users" / "test-owner"
    nested.mkdir(parents=True)
    (nested / "book.pdf").write_bytes(b"fixture bytes")
    value = reset.ResetScope(engine=engine, runtime_environment="test", maintenance_dir=tmp_path / "maintenance", local_roots=(uploads,), enabled=True)
    yield value
    engine.dispose()


def run(scope, preview=None):
    preview = preview or reset.preview_reset(scope)
    return reset.execute_reset(scope, fingerprint=preview["fingerprint"], confirmation=preview["confirmation"], services_stopped=True)


def rows(scope):
    with scope.engine.connect() as connection:
        return connection.execute(text("SELECT count(*) FROM users")).scalar_one()


def test_preview_is_read_only_includes_dynamic_tables_and_no_identity(scope):
    preview = reset.preview_reset(scope)
    assert preview["tables"]["users"] == 1
    assert preview["tables"]["blocked_registration_emails"] == 1
    assert {"workflow_operations", "workflow_source_items", "account_closures"} <= set(preview["tables"])
    assert preview["storage"]["files"] == 2
    assert not scope.maintenance_dir.exists()
    output = json.dumps(preview)
    assert "reusable" not in output and "fake-hash" not in output and "test-owner" not in output
    assert rows(scope) == 1


def test_reset_clears_every_table_orphan_and_releases_identity(scope):
    before = reset.preview_reset(scope)
    tables = inspect(scope.engine).get_table_names()
    receipt = run(scope, before)
    assert receipt["status"] == "completed"
    assert set(inspect(scope.engine).get_table_names()) == set(tables)
    with scope.engine.begin() as connection:
        for table in Base.metadata.tables.values():
            assert connection.execute(text(f'SELECT count(*) FROM "{table.name}"')).scalar_one() == 0
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "test-reset-head"
        connection.execute(text("INSERT INTO users (id, username, email, role, password_hash, is_active, auth_version, created_at, updated_at) VALUES ('new-user', 'reusable-name', 'reusable@example.invalid', 'teacher', 'new-fake-hash', true, 0, 1, 1)"))
    assert list(scope.local_roots[0].iterdir()) == [scope.local_roots[0] / reset.SENTINEL]
    assert not (scope.maintenance_dir / "active-reset.json").exists()
    serialized = json.dumps(receipt)
    assert "reusable" not in serialized and "test-owner" not in serialized
    # Repeating an old successful command must not erase the newly registered user.
    assert run(scope, before)["replayed"] is True
    assert rows(scope) == 1


def test_confirmation_cancel_and_stale_preview_delete_nothing(scope):
    preview = reset.preview_reset(scope)
    with pytest.raises(reset.ResetError, match="confirmation_mismatch"):
        reset.execute_reset(scope, fingerprint=preview["fingerprint"], confirmation="cancel", services_stopped=True)
    with pytest.raises(reset.ResetError, match="stop_all_services"):
        reset.execute_reset(scope, fingerprint=preview["fingerprint"], confirmation=preview["confirmation"], services_stopped=False)
    (scope.local_roots[0] / "new-file.txt").write_text("added after preview")
    with pytest.raises(reset.ResetError, match="preview_stale"):
        run(scope, preview)
    assert rows(scope) == 1
    assert (scope.local_roots[0] / "orphan.txt").exists()
    assert not (scope.maintenance_dir / "active-reset.json").exists()


def test_storage_failure_retains_database_and_resumes_original_plan(scope, monkeypatch):
    preview = reset.preview_reset(scope)
    original = Path.unlink
    deleted = []
    def fail_after_one(path, *args, **kwargs):
        if path.is_relative_to(scope.local_roots[0]):
            if deleted:
                raise OSError("simulated private path/credentials must not surface")
            deleted.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_after_one)
    with pytest.raises(reset.ResetError, match="reset_failed_keep_services_stopped"):
        run(scope, preview)
    assert rows(scope) == 1
    assert len(deleted) == 1
    journal = (scope.maintenance_dir / "active-reset.json").read_text()
    assert "simulated" not in journal and "reusable" not in journal
    with pytest.raises(reset.ResetError, match="incomplete"):
        with reset.maintenance_service_guard(scope):
            pass
    monkeypatch.setattr(Path, "unlink", original)
    assert run(scope, preview)["status"] == "completed"
    assert rows(scope) == 0


def test_database_failure_keeps_rows_and_resume_completes(scope, monkeypatch):
    preview = reset.preview_reset(scope)
    original = reset._purge_database
    def fail(connection):
        connection.execute(text("DELETE FROM users"))
        raise RuntimeError("rollback")
    monkeypatch.setattr(reset, "_purge_database", fail)
    with pytest.raises(reset.ResetError):
        run(scope, preview)
    assert rows(scope) == 1
    monkeypatch.setattr(reset, "_purge_database", original)
    assert run(scope, preview)["status"] == "completed"


def test_crash_after_commit_recovers_receipt(scope, monkeypatch):
    preview = reset.preview_reset(scope)
    original = reset._write_json
    def fail_receipt(path, value):
        if path.name.startswith("receipt-"):
            raise OSError("receipt disk temporarily full")
        original(path, value)
    monkeypatch.setattr(reset, "_write_json", fail_receipt)
    with pytest.raises(reset.ResetError):
        run(scope, preview)
    assert rows(scope) == 0
    monkeypatch.setattr(reset, "_write_json", original)
    assert run(scope, preview)["status"] == "completed"


def test_shared_service_and_exclusive_executor_locks_prevent_overlap(scope):
    preview = reset.preview_reset(scope)
    with reset.maintenance_service_guard(scope):
        with pytest.raises(reset.ResetError, match="services_or_executor_running"):
            run(scope, preview)
    with reset._process_lock(scope):
        with pytest.raises(reset.ResetError, match="services_or_executor_running"):
            run(scope, preview)
        with pytest.raises(reset.ResetError, match="services_or_executor_running"):
            with reset.maintenance_service_guard(scope):
                pass
    assert rows(scope) == 1


@pytest.mark.parametrize("mutation", ["symlink_file", "symlink_directory", "database_overlap", "missing_marker", "config", "config_directory", "hardlink", "home", "repository", "control_overlap"])
def test_unsafe_storage_rejected_without_deletion(scope, tmp_path, mutation):
    uploads = scope.local_roots[0]
    if mutation == "symlink_file":
        (uploads / "external").symlink_to(tmp_path / "business.sqlite")
    elif mutation == "symlink_directory":
        (uploads / "external").symlink_to(tmp_path, target_is_directory=True)
    elif mutation == "database_overlap":
        scope = replace(scope, local_roots=(tmp_path,))
    elif mutation == "missing_marker":
        (uploads / reset.SENTINEL).unlink()
    elif mutation == "config":
        (uploads / ".env").write_text("not actual secrets")
    elif mutation == "config_directory":
        (uploads / ".git").mkdir()
    elif mutation == "hardlink":
        (uploads / "hardlink.txt").hardlink_to(uploads / "orphan.txt")
    elif mutation == "home":
        scope = replace(scope, local_roots=(Path.home(),))
    elif mutation == "repository":
        scope = replace(scope, local_roots=(reset._REPO,))
    else:
        scope = replace(scope, maintenance_dir=uploads / "control")
    with pytest.raises(reset.ResetError):
        reset.preview_reset(scope)
    assert rows(scope) == 1
    assert (uploads / "orphan.txt").exists()


@pytest.mark.parametrize("environment, enabled", [("production", True), ("test", False)])
def test_disabled_environment(scope, environment, enabled):
    with pytest.raises(reset.ResetError, match="disabled"):
        reset.preview_reset(replace(scope, runtime_environment=environment, enabled=enabled))


def test_unknown_table_refuses_reset(scope):
    with scope.engine.begin() as connection:
        connection.execute(text("CREATE TABLE unrelated_configuration (id INTEGER)"))
    with pytest.raises(reset.ResetError, match="schema_mismatch"):
        reset.preview_reset(scope)
    assert rows(scope) == 1


class FakeS3Error(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self):
        self.versions = [{"Key": "orphan/file", "VersionId": "one", "Size": 20}, {"Key": "orphan/file", "VersionId": "two", "Size": 25}]
        self.markers = [{"Key": "deleted/file", "VersionId": "marker"}]
        self.uploads = [{"Key": "unfinished/file", "UploadId": "upload"}]
        self.fail = False
    def get_bucket_tagging(self, **kwargs):
        return {"TagSet": [{"Key": "smartai-reset-scope", "Value": "disposable"}]}
    def get_bucket_versioning(self, **kwargs):
        return {"Status": "Enabled"}
    def get_bucket_replication(self, **kwargs):
        raise FakeS3Error("ReplicationConfigurationNotFoundError")
    def get_object_lock_configuration(self, **kwargs):
        raise FakeS3Error("ObjectLockConfigurationNotFoundError")
    def list_object_versions(self, **kwargs):
        return {"IsTruncated": False, "Versions": self.versions.copy(), "DeleteMarkers": self.markers.copy()}
    def list_objects_v2(self, **kwargs):
        return {"IsTruncated": False, "Contents": [{"Key": v["Key"]} for v in self.versions]}
    def list_multipart_uploads(self, **kwargs):
        return {"IsTruncated": False, "Uploads": self.uploads.copy()}
    def delete_object(self, **kwargs):
        if self.fail:
            raise FakeS3Error("AccessDenied")
        self.versions = [v for v in self.versions if v["VersionId"] != kwargs["VersionId"]]
        self.markers = [v for v in self.markers if v["VersionId"] != kwargs["VersionId"]]
        return {}
    def abort_multipart_upload(self, **kwargs):
        self.uploads = [v for v in self.uploads if v["UploadId"] != kwargs["UploadId"]]
        return {}


def test_s3_all_versions_markers_and_multipart_and_failure_resume(scope):
    client = FakeS3()
    store = reset.S3ResetStore(client, bucket="disposable-test", endpoint_identity="fake-no-network")
    scope = replace(scope, object_store=store)
    preview = reset.preview_reset(scope)
    assert preview["storage"]["versions_and_markers"] == 3
    assert preview["storage"]["multipart_uploads"] == 1
    client.fail = True
    with pytest.raises(reset.ResetError, match="object_delete_failed"):
        run(scope, preview)
    assert rows(scope) == 1
    client.fail = False
    assert run(scope, preview)["status"] == "completed"
    assert not client.versions and not client.markers and not client.uploads


def test_s3_unproven_listing_or_replication_refuses(scope):
    client = FakeS3()
    scope = replace(scope, object_store=reset.S3ResetStore(client, bucket="fake", endpoint_identity="fake"))
    client.get_bucket_replication = Mock(return_value={"ReplicationConfiguration": {}})
    with pytest.raises(reset.ResetError, match="replication"):
        reset.preview_reset(scope)
    client.get_bucket_replication = Mock(side_effect=FakeS3Error("ReplicationConfigurationNotFoundError"))
    client.list_object_versions = Mock(return_value={"IsTruncated": True, "NextKeyMarker": "same"})
    with pytest.raises(reset.ResetError, match="listing_invalid"):
        reset.preview_reset(scope)
    assert rows(scope) == 1


def test_populated_workflow_and_cyclic_knowledge_graph(scope):
    from backend.db.models import CourseRecord, AssignmentRecord, StoredFileRecord, KnowledgeDocumentRecord
    from backend.db.workflow_repository import WorkflowOperationRecord
    from backend.db.source_outcome_repository import WorkflowSourceItemRecord, WorkflowSourceOutcomeRecord
    with scope.engine.begin() as connection:
        connection.execute(CourseRecord.__table__.insert().values(id="course", teacher_id="test-owner", name="fixture"))
        connection.execute(AssignmentRecord.__table__.insert().values(id="task", teacher_id="test-owner", course_id="course", name="fixture"))
        connection.execute(StoredFileRecord.__table__.insert().values(id="file", owner_id="test-owner", kind="source", original_name="book.pdf", storage_key="users/test-owner/book.pdf", size_bytes=13, sha256="0" * 64))
        connection.execute(KnowledgeDocumentRecord.__table__.insert().values(id="knowledge", owner_id="test-owner", stored_file_id="file", title="fixture", original_name="book.pdf", size_bytes=13, sha256="0" * 64))
        connection.execute(text("UPDATE stored_files SET knowledge_document_id='knowledge' WHERE id='file'"))
        connection.execute(WorkflowOperationRecord.__table__.insert().values(id="operation", assignment_id="task", owner_id="test-owner", operation_type="question_preparation", input_hash="1" * 64))
        connection.execute(WorkflowSourceItemRecord.__table__.insert().values(id="source", owner_id="test-owner", assignment_id="task", operation_id="operation", attempt=1, order_index=0, stored_file_id="file"))
        connection.execute(WorkflowSourceOutcomeRecord.__table__.insert().values(source_id="source", status="parse_failed", matched_answer_count=0, unknown_question_ids=[], retryable=True, artifact_file_id="file"))
    preview = reset.preview_reset(scope)
    assert preview["tables"]["workflow_operations"] == 1
    assert run(scope, preview)["status"] == "completed"
    assert not any(reset.preview_reset(scope)["tables"].values())


def test_missing_old_storage_backend_refuses_before_delete(scope):
    from backend.db.models import StoredFileRecord
    with scope.engine.begin() as connection:
        connection.execute(StoredFileRecord.__table__.insert().values(id="other", owner_id="test-owner", kind="source", original_name="fake", storage_backend="object", storage_key="fake", size_bytes=13, sha256="0" * 64))
    with pytest.raises(reset.ResetError, match="storage_backend_not_in_scope"):
        reset.preview_reset(scope)
    assert rows(scope) == 1


def test_new_same_size_dataset_receives_new_reset_identity(scope):
    run(scope)
    empty_first = reset.preview_reset(scope)
    run(scope, empty_first)
    empty_second = reset.preview_reset(scope)
    assert empty_first["fingerprint"] != empty_second["fingerprint"]
    assert "replayed" not in run(scope, empty_second)


def test_sqlite_orphan_rows_without_fk_are_deleted(scope):
    if scope.engine.dialect.name != "sqlite":
        pytest.skip("SQLite legacy invalid-FK fixture; PostgreSQL graph tested separately")
    from backend.db.models import CourseRecord
    with scope.engine.begin() as connection:
        connection.execute(CourseRecord.__table__.insert().values(id="orphan", teacher_id="missing", name="fixture"))
    assert run(scope)["status"] == "completed"
    assert not any(reset.preview_reset(scope)["tables"].values())


def test_separate_process_service_lock_blocks_execution(scope):
    import subprocess
    import sys
    code = '''
from pathlib import Path
import sys
from sqlalchemy import create_engine
from backend.services.admin_reset import ResetScope, maintenance_service_guard
scope = ResetScope(create_engine(sys.argv[1]), "test", Path(sys.argv[2]), enabled=True)
with maintenance_service_guard(scope):
    print("locked", flush=True)
    sys.stdin.readline()
'''
    process = subprocess.Popen([sys.executable, "-c", code, str(scope.engine.url), str(scope.maintenance_dir)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(reset.ResetError, match="services_or_executor_running"):
            run(scope)
        assert rows(scope) == 1
    finally:
        process.communicate("stop\n", timeout=10)
    assert process.returncode == 0


def test_postgres_guard_also_blocks_a_different_control_directory(scope, tmp_path):
    if scope.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL advisory lock supplements the mandatory shared control directory")
    other = replace(scope, maintenance_dir=tmp_path / "other-maintenance")
    with reset.maintenance_service_guard(scope):
        with pytest.raises(reset.ResetError, match="services_or_executor_running"):
            run(other)
    assert rows(scope) == 1


def test_object_scope_without_client_can_guard_but_cannot_preview(scope):
    object_scope = replace(scope, local_roots=(), object_store=None, object_client_required=True)
    with reset.maintenance_service_guard(object_scope):
        pass
    with pytest.raises(reset.ResetError, match="object_client_required"):
        reset.preview_reset(object_scope)


def test_new_files_after_failure_abort_recovery_without_deleting_them(scope, monkeypatch):
    preview = reset.preview_reset(scope)
    original = reset._purge_database
    monkeypatch.setattr(reset, "_purge_database", Mock(side_effect=RuntimeError("fixture failure")))
    with pytest.raises(reset.ResetError):
        run(scope, preview)
    extra = scope.local_roots[0] / "unexpected-new-file"
    extra.write_text("must survive failed resume")
    monkeypatch.setattr(reset, "_purge_database", original)
    with pytest.raises(reset.ResetError, match="new_storage_during_recovery"):
        run(scope, preview)
    assert rows(scope) == 1 and extra.exists()


def test_successful_receipt_recovers_lingering_active_marker(scope):
    preview = reset.preview_reset(scope)
    receipt = run(scope, preview)
    (scope.maintenance_dir / "active-reset.json").write_text(json.dumps({"fingerprint": preview["fingerprint"]}))
    assert run(scope, preview)["operation_id"] == receipt["operation_id"]
    assert not (scope.maintenance_dir / "active-reset.json").exists()


def test_s3_null_version_and_multi_page_inventory(scope):
    client = FakeS3()
    client.versions.append({"Key": "unversioned", "VersionId": "null", "Size": 1})
    def pages(**kwargs):
        if kwargs.get("KeyMarker"):
            return {"IsTruncated": False, "Versions": client.versions[1:], "DeleteMarkers": client.markers}
        if client.versions:
            return {"IsTruncated": True, "Versions": client.versions[:1], "NextKeyMarker": "next", "NextVersionIdMarker": "next-version"}
        return {"IsTruncated": False}
    client.list_object_versions = pages
    object_scope = replace(scope, object_store=reset.S3ResetStore(client, bucket="fake", endpoint_identity="fake"))
    assert reset.preview_reset(object_scope)["storage"]["versions_and_markers"] == 4
    assert run(object_scope)["status"] == "completed"
    assert not client.versions and not client.markers and not client.uploads


def test_cli_defaults_to_preview_and_cancel_never_executes(scope, monkeypatch, capsys):
    from backend.config import settings
    from scripts.reset_business_data import main
    monkeypatch.setattr(settings, "runtime_environment", "test")
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setenv("SMARTAI_ADMIN_RESET_ENABLED", "true")
    monkeypatch.setattr(reset, "scope_from_settings", lambda **_: scope)
    assert main([]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["execution"] == "offline_cli_only"
    assert not scope.maintenance_dir.exists()
    assert main(["--execute", "--fingerprint", preview["fingerprint"], "--confirm", "cancel", "--services-stopped"]) == 2
    assert "confirmation_mismatch" in capsys.readouterr().err
    assert rows(scope) == 1


def test_dedicated_temporary_files_and_old_roots_are_part_of_scope(scope, tmp_path):
    temporary = tmp_path / "runtime-temp"
    temporary.mkdir()
    (temporary / reset.SENTINEL).write_text(json.dumps(reset.SENTINEL_CONTENT))
    (temporary / "crashed-interpreter.py").write_text("synthetic student snippet")
    with pytest.raises(reset.ResetError, match="dedicated_temporary_root_required"):
        reset.preview_reset(replace(scope, temporary_root=temporary, require_temporary_root=True))
    extended = replace(scope, local_roots=(*scope.local_roots, temporary), temporary_root=temporary, require_temporary_root=True)
    assert reset.preview_reset(extended)["storage"]["files"] == 3
    assert run(extended)["status"] == "completed"
    assert list(temporary.iterdir()) == [temporary / reset.SENTINEL]


def test_settings_adapter_keeps_object_guard_and_requires_explicit_temp_scope(scope, tmp_path, monkeypatch):
    from backend.config import settings
    from backend.db import session
    temporary = tmp_path / "runtime-temp"
    temporary.mkdir()
    (temporary / reset.SENTINEL).write_text(json.dumps(reset.SENTINEL_CONTENT))
    monkeypatch.setattr(session, "get_engine", lambda: scope.engine)
    monkeypatch.setattr(settings, "runtime_environment", "test")
    monkeypatch.setattr(settings, "storage_backend", "object")
    monkeypatch.setenv("SMARTAI_ADMIN_MAINTENANCE_DIR", str(scope.maintenance_dir))
    monkeypatch.setenv("SMARTAI_ADMIN_RESET_ENABLED", "true")
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setenv("SMARTAI_ADMIN_RESET_EXTRA_STORAGE_ROOTS", json.dumps([str(temporary)]))
    guarded = reset.scope_from_settings()
    with reset.maintenance_service_guard(guarded):
        pass
    with pytest.raises(reset.ResetError, match="object_client_required"):
        reset.preview_reset(guarded)
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "storage_root", str(scope.local_roots[0]))
    assert reset.preview_reset(reset.scope_from_settings())["storage"]["files"] == 2
    monkeypatch.delenv("TMPDIR")
    with pytest.raises(reset.ResetError, match="dedicated_temporary_root_required"):
        reset.preview_reset(reset.scope_from_settings())


def test_fixture_postgres_ddl_preserves_shared_sqlite_foreign_keys():
    # Exercise PostgreSQL's circular-FK ALTER path without a database/server.
    rules = {fk: fk._create_rule for table in Base.metadata.tables.values() for fk in table.foreign_key_constraints}
    postgres = create_mock_engine("postgresql://", lambda *args, **kwargs: None)
    _fixture_metadata().create_all(postgres, checkfirst=False)
    assert all(fk._create_rule is original for fk, original in rules.items())
    sqlite_engine = create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(sqlite_engine)
        inspector = inspect(sqlite_engine)
        for table in Base.metadata.tables.values():
            expected = {tuple(column.name for column in fk.columns) for fk in table.foreign_key_constraints}
            actual = {tuple(fk["constrained_columns"]) for fk in inspector.get_foreign_keys(table.name)}
            assert actual == expected, table.name
    finally:
        sqlite_engine.dispose()
