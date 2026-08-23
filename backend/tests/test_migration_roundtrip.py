"""Migration round-trip smoke (Task 1).

Verifies the single normalized baseline is reversible on SQLite: starting from
an empty DB, ``upgrade head → downgrade base → upgrade head`` must all succeed.
This catches batch-alter downgrade issues (constraint names, dropped columns)
that an upgrade-only check misses, and proves the clean baseline creates every
retained + education table from an empty database.
"""
from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config


REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(db_url: str, monkeypatch) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option(
        "script_location", str(REPO_ROOT / "backend/db/migrations")
    )
    # env.py reads SMARTAI_DATABASE_URL; also force light mode so the SQLite URL
    # passes validate_database_mode(). Use monkeypatch so the env is restored
    # after the test and doesn't leak into other tests' configure_database().
    monkeypatch.setenv("SMARTAI_DATABASE_URL", db_url)
    monkeypatch.setenv("SMARTAI_DATABASE_HEAVY", "OFF")
    return cfg


def test_upgrade_creates_missing_sqlite_parent(tmp_path, monkeypatch):
    database_path = tmp_path / "data" / "nested" / "smartai.db"
    cfg = _alembic_config("sqlite:///data/nested/smartai.db", monkeypatch)
    monkeypatch.chdir(tmp_path)

    assert not database_path.parent.exists()
    command.upgrade(cfg, "head")

    assert database_path.is_file()


def test_migration_downgrade_from_empty_head_then_upgrade(tmp_path, monkeypatch):
    """Upgrade to head, downgrade straight to base, then back to head — full reversibility."""
    db_url = f"sqlite:///{(tmp_path / 'fullround.db').as_posix()}"
    cfg = _alembic_config(db_url, monkeypatch)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


def test_normalized_tables_exist_after_roundtrip(tmp_path, monkeypatch):
    """After a full downgrade→upgrade roundtrip the education tables reappear."""
    db_url = f"sqlite:///{(tmp_path / 'roundtrip.db').as_posix()}"
    cfg = _alembic_config(db_url, monkeypatch)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    from sqlalchemy import inspect
    from backend.db.session import configure_database

    configure_database(db_url)
    tables = set(inspect(configure_database()).get_table_names())
    assert {
        "courses",
        "course_enrollments",
        "assignments",
        "assignment_questions",
        "submissions",
        "submission_revisions",
        "submission_answers",
        "grading_runs",
        "grade_results",
        "teacher_reviews",
        "stored_files",
        "provider_preferences",
        "course_material_groups",
        "course_materials",
        "tags",
        "assignment_tags",
        "assignment_workflows",
        "task_create_idempotency",
        "workflow_operations",
        "workflow_source_items",
        "workflow_source_outcomes",
        "assignment_student_presentations",
        "submission_answer_presentations",
        "grading_run_setups",
        "result_artifact_manifests",
    } <= tables
    # Legacy tables must not reappear after the roundtrip.
    assert not ({"tasks", "grading_jobs", "task_knowledge_documents"} & tables)


def test_provider_routing_migration_backfills_default_and_question_column(
    tmp_path,
    monkeypatch,
):
    from sqlalchemy import create_engine, inspect, text

    db_url = f"sqlite:///{(tmp_path / 'provider-routing.db').as_posix()}"
    cfg = _alembic_config(db_url, monkeypatch)
    command.upgrade(cfg, "0008_operation_leases")
    engine = create_engine(db_url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users "
            "(id, username, role, password_hash, is_active, created_at, updated_at) "
            "VALUES ('owner', 'owner', 'teacher', 'hash', true, 1, 1)"
        ))
        for provider_id, created_at in (("later", 2), ("first", 1)):
            connection.execute(text(
                "INSERT INTO provider_configs "
                "(id, owner_id, provider_type, model, encrypted_api_key, nonce, "
                "key_version, enabled, max_concurrent, rpm, created_at, updated_at) "
                "VALUES (:id, 'owner', 'openai', :model, 'cipher', 'nonce', "
                "1, true, 5, 0, :created_at, :created_at)"
            ), {"id": provider_id, "model": provider_id, "created_at": created_at})

    command.upgrade(cfg, "head")
    inspector = inspect(engine)
    assert "question_recognition_provider_id" in {
        column["name"] for column in inspector.get_columns("assignment_workflows")
    }
    with engine.connect() as connection:
        preference = connection.execute(text(
            "SELECT owner_id, default_provider_id FROM provider_preferences"
        )).one()
    assert tuple(preference) == ("owner", "first")


def test_source_outcome_migration_has_contract_constraints(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect

    db_url = f"sqlite:///{(tmp_path / 'source-contract.db').as_posix()}"
    cfg = _alembic_config(db_url, monkeypatch)
    command.upgrade(cfg, "head")
    inspector = inspect(create_engine(db_url))

    assert {
        column["name"]
        for column in inspector.get_columns("workflow_source_items")
    } == {
        "id",
        "owner_id",
        "assignment_id",
        "operation_id",
        "attempt",
        "order_index",
        "stored_file_id",
        "retry_of_source_id",
        "created_at",
    }
    assert {
        column["name"]
        for column in inspector.get_columns("workflow_source_outcomes")
    } == {
        "source_id",
        "status",
        "student_candidate",
        "matched_answer_count",
        "unknown_question_ids",
        "stable_error_code",
        "failure_phase",
        "retryable",
        "artifact_file_id",
        "created_at",
    }
    source_uniques = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("workflow_source_items")
    }
    assert ("operation_id", "attempt", "order_index") in source_uniques
    assert ("operation_id", "attempt", "stored_file_id") in source_uniques

    source_checks = " ".join(
        item["sqltext"]
        for item in inspector.get_check_constraints("workflow_source_items")
    )
    outcome_checks = " ".join(
        item["sqltext"]
        for item in inspector.get_check_constraints("workflow_source_outcomes")
    )
    assert "attempt > 0" in source_checks
    assert "order_index >= 0" in source_checks
    assert "matched_answer_count >= 0" in outcome_checks
    assert "identity_conflict" in outcome_checks

    source_indexes = {
        tuple(item["column_names"])
        for item in inspector.get_indexes("workflow_source_items")
    }
    assert ("assignment_id", "operation_id", "attempt", "order_index") in source_indexes

    presentation_columns = {
        column["name"]
        for column in inspector.get_columns("assignment_student_presentations")
    }
    assert "source_id" in presentation_columns
    presentation_indexes = {
        (item["name"], tuple(item["column_names"]), item["unique"])
        for item in inspector.get_indexes("assignment_student_presentations")
    }
    assert (
        "ix_assignment_student_presentations_source_id",
        ("source_id",),
        1,
    ) in presentation_indexes
    presentation_foreign_keys = {
        item["name"]: item
        for item in inspector.get_foreign_keys("assignment_student_presentations")
    }
    source_fk = presentation_foreign_keys[
        "fk_assignment_student_presentations_source"
    ]
    assert source_fk["referred_table"] == "workflow_source_items"
    assert source_fk["referred_columns"] == ["id"]
    assert source_fk["options"].get("ondelete") == "SET NULL"


def test_operation_checkpoint_migration_has_contract_columns(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect

    db_url = f"sqlite:///{(tmp_path / 'checkpoint-contract.db').as_posix()}"
    cfg = _alembic_config(db_url, monkeypatch)
    command.upgrade(cfg, "head")
    inspector = inspect(create_engine(db_url))

    columns = {
        column["name"]: column
        for column in inspector.get_columns("workflow_operations")
    }
    assert {
        "checkpoint_revision",
        "checkpoint_stage",
        "checkpoint",
        "artifact_refs",
        "terminal_summary",
    } <= columns.keys()
    assert columns["checkpoint_revision"]["nullable"] is False
    assert columns["checkpoint"]["nullable"] is False
    assert columns["artifact_refs"]["nullable"] is False

    checks = {
        item["name"]: item["sqltext"]
        for item in inspector.get_check_constraints("workflow_operations")
    }
    assert "ck_workflow_operations_checkpoint_revision_nonnegative" in checks
    assert "checkpoint_revision >= 0" in checks[
        "ck_workflow_operations_checkpoint_revision_nonnegative"
    ]


def test_operation_checkpoint_migration_preserves_0005_operation(
    tmp_path, monkeypatch,
):
    from sqlalchemy import create_engine, inspect, text

    db_url = f"sqlite:///{(tmp_path / 'checkpoint-preserve.db').as_posix()}"
    cfg = _alembic_config(db_url, monkeypatch)
    command.upgrade(cfg, "0005_workflow_source_outcomes")
    engine = create_engine(db_url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users "
            "(id, username, role, password_hash, is_active, created_at, updated_at) "
            "VALUES ('owner', 'owner', 'teacher', 'h', 1, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO courses "
            "(id, name, code, description, teacher_id, created_at, updated_at) "
            "VALUES ('course', 'Course', '', '', 'owner', 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO assignments "
            "(id, course_id, teacher_id, name, description, status, created_at, "
            "updated_at, version) VALUES "
            "('assignment', 'course', 'owner', 'Assignment', '', 'draft', 1, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO workflow_operations "
            "(id, assignment_id, owner_id, operation_type, input_hash, attempt, "
            "status, progress, payload, error_code, created_at, updated_at) VALUES "
            "('operation', 'assignment', 'owner', 'submission_recognition', "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "2, 'running', :progress, :payload, NULL, 1, 2)"
        ), {"progress": '{"done":1}', "payload": '{"source":"file"}'})

    command.upgrade(cfg, "0006_operation_checkpoints")
    with engine.begin() as connection:
        assert connection.execute(text(
            "SELECT checkpoint_revision, checkpoint_stage, checkpoint, "
            "artifact_refs, terminal_summary FROM workflow_operations "
            "WHERE id='operation'"
        )).one() == (0, None, "{}", "[]", None)
        connection.execute(text(
            "UPDATE workflow_operations SET checkpoint_revision=1, "
            "checkpoint_stage='parsed', checkpoint=:checkpoint, "
            "artifact_refs=:artifact_refs, terminal_summary=:terminal_summary "
            "WHERE id='operation'"
        ), {
            "checkpoint": '{"done":1}',
            "artifact_refs": '["file"]',
            "terminal_summary": '{"outcome":"done"}',
        })

    command.downgrade(cfg, "0005_workflow_source_outcomes")
    columns = {
        item["name"]
        for item in inspect(engine).get_columns("workflow_operations")
    }
    assert "checkpoint_revision" not in columns
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT id, attempt, status, progress, payload, created_at, updated_at "
            "FROM workflow_operations WHERE id='operation'"
        )).one() == (
            "operation", 2, "running", '{"done":1}', '{"source":"file"}', 1.0, 2.0
        )

    command.upgrade(cfg, "0006_operation_checkpoints")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT checkpoint_revision, checkpoint_stage, checkpoint, "
            "artifact_refs, terminal_summary FROM workflow_operations "
            "WHERE id='operation'"
        )).one() == (0, None, "{}", "[]", None)


def test_source_diagnostic_migration_preserves_0006_rows_across_roundtrip(
    tmp_path, monkeypatch,
):
    from sqlalchemy import create_engine, inspect, text

    db_url = f"sqlite:///{(tmp_path / 'diagnostic-preserve.db').as_posix()}"
    cfg = _alembic_config(db_url, monkeypatch)
    command.upgrade(cfg, "0006_operation_checkpoints")
    engine = create_engine(db_url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users "
            "(id, username, role, password_hash, is_active, created_at, updated_at) "
            "VALUES ('owner', 'owner', 'teacher', 'h', 1, 1, 1), "
            "('student', 'student', 'student', 'h', 1, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO courses "
            "(id, name, code, description, teacher_id, created_at, updated_at) "
            "VALUES ('course', 'Course', '', '', 'owner', 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO assignments "
            "(id, course_id, teacher_id, name, description, status, created_at, "
            "updated_at, version) VALUES "
            "('assignment', 'course', 'owner', 'Assignment', '', 'draft', 1, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO workflow_operations "
            "(id, assignment_id, owner_id, operation_type, input_hash, attempt, "
            "status, progress, payload, error_code, created_at, updated_at) VALUES "
            "('operation', 'assignment', 'owner', 'submission_recognition', "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "1, 'running', '{}', '{}', NULL, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO stored_files "
            "(id, owner_id, kind, original_name, storage_backend, storage_key, "
            "content_type, size_bytes, sha256, assignment_id, created_at) VALUES "
            "('file', 'owner', 'submission_source', 'answers.pdf', 'local', "
            "'assignments/assignment/file/answers.pdf', 'application/pdf', 7, "
            "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
            "'assignment', 1)"
        ))
        connection.execute(text(
            "INSERT INTO workflow_source_items "
            "(id, owner_id, assignment_id, operation_id, attempt, order_index, "
            "stored_file_id, retry_of_source_id, created_at) VALUES "
            "('source', 'owner', 'assignment', 'operation', 1, 0, 'file', NULL, 1)"
        ))
        connection.execute(text(
            "INSERT INTO workflow_source_outcomes "
            "(source_id, status, student_candidate, matched_answer_count, "
            "unknown_question_ids, stable_error_code, retryable, artifact_file_id, "
            "created_at) VALUES "
            "('source', 'parse_failed', NULL, 0, '[]', "
            "'submission_parse_failed', 1, NULL, 1)"
        ))
        connection.execute(text(
            "INSERT INTO assignment_student_presentations "
            "(id, assignment_id, student_id, display_student_id, display_name, "
            "source_filename, identity_match_method, identity_status, is_active, "
            "created_at, updated_at) VALUES "
            "('presentation', 'assignment', 'student', 'student', 'Student', "
            "'answers.pdf', 'exact', 'matched', 1, 1, 1)"
        ))

    command.upgrade(cfg, "0007_source_outcome_diagnostics")
    with engine.begin() as connection:
        assert connection.execute(text(
            "SELECT status, stable_error_code, failure_phase "
            "FROM workflow_source_outcomes WHERE source_id='source'"
        )).one() == (
            "parse_failed",
            "submission_parse_failed",
            None,
        )
        assert connection.execute(text(
            "SELECT student_id, source_filename, source_id "
            "FROM assignment_student_presentations WHERE id='presentation'"
        )).one() == ("student", "answers.pdf", None)
        connection.execute(text(
            "UPDATE workflow_source_outcomes SET failure_phase='recognition' "
            "WHERE source_id='source'"
        ))
        connection.execute(text(
            "UPDATE assignment_student_presentations SET source_id='source' "
            "WHERE id='presentation'"
        ))

    command.downgrade(cfg, "0006_operation_checkpoints")
    inspector = inspect(engine)
    assert "failure_phase" not in {
        column["name"]
        for column in inspector.get_columns("workflow_source_outcomes")
    }
    assert "source_id" not in {
        column["name"]
        for column in inspector.get_columns("assignment_student_presentations")
    }
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT source_id, status, stable_error_code, retryable "
            "FROM workflow_source_outcomes WHERE source_id='source'"
        )).one() == (
            "source",
            "parse_failed",
            "submission_parse_failed",
            1,
        )
        assert connection.execute(text(
            "SELECT id, student_id, source_filename "
            "FROM assignment_student_presentations WHERE id='presentation'"
        )).one() == ("presentation", "student", "answers.pdf")

    command.upgrade(cfg, "0007_source_outcome_diagnostics")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT status, stable_error_code, failure_phase "
            "FROM workflow_source_outcomes WHERE source_id='source'"
        )).one() == (
            "parse_failed",
            "submission_parse_failed",
            None,
        )
        assert connection.execute(text(
            "SELECT student_id, source_filename, source_id "
            "FROM assignment_student_presentations WHERE id='presentation'"
        )).one() == ("student", "answers.pdf", None)


def test_source_diagnostic_upgrade_accepts_legacy_pr26_0005_schema(
    tmp_path,
    monkeypatch,
):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import Column, String, create_engine, inspect

    db_url = f"sqlite:///{(tmp_path / 'legacy-pr26-0005.db').as_posix()}"
    cfg = _alembic_config(db_url, monkeypatch)
    command.upgrade(cfg, "0005_workflow_source_outcomes")
    engine = create_engine(db_url)

    # PR #26's previously published 0005 put these diagnostics directly in
    # 0005. Recreate that exact shape while the database remains stamped 0005.
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        with operations.batch_alter_table("workflow_source_outcomes") as batch_op:
            batch_op.add_column(Column("failure_phase", String(64), nullable=True))
        with operations.batch_alter_table(
            "assignment_student_presentations"
        ) as batch_op:
            batch_op.add_column(Column("source_id", String(64), nullable=True))
            batch_op.create_foreign_key(
                "fk_assignment_student_presentations_source",
                "workflow_source_items",
                ["source_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch_op.create_index(
                "ix_assignment_student_presentations_source_id",
                ["source_id"],
                unique=True,
            )

    command.upgrade(cfg, "head")
    inspector = inspect(engine)
    assert [
        column["name"]
        for column in inspector.get_columns("workflow_source_outcomes")
    ].count("failure_phase") == 1
    assert [
        column["name"]
        for column in inspector.get_columns("assignment_student_presentations")
    ].count("source_id") == 1
    assert any(
        foreign_key.get("constrained_columns") == ["source_id"]
        and foreign_key.get("referred_table") == "workflow_source_items"
        for foreign_key in inspector.get_foreign_keys(
            "assignment_student_presentations"
        )
    )
    assert any(
        index.get("column_names") == ["source_id"] and index.get("unique")
        for index in inspector.get_indexes("assignment_student_presentations")
    )


def test_source_outcome_migration_preserves_prior_rows(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text

    db_url = f"sqlite:///{(tmp_path / 'source-preserve.db').as_posix()}"
    cfg = _alembic_config(db_url, monkeypatch)
    command.upgrade(cfg, "0004_structured_review_reasons")
    engine = create_engine(db_url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users "
            "(id, username, role, password_hash, is_active, created_at, updated_at) "
            "VALUES ('owner', 'owner', 'teacher', 'h', 1, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO courses "
            "(id, name, code, description, teacher_id, created_at, updated_at) "
            "VALUES ('course', 'Course', '', '', 'owner', 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO assignments "
            "(id, course_id, teacher_id, name, description, status, created_at, "
            "updated_at, version) VALUES "
            "('assignment', 'course', 'owner', 'Assignment', '', 'draft', 1, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO assignment_workflows "
            "(assignment_id, owner_id, presentation_status, workflow_revision, "
            "submission_identity_mode, final_result_version, analysis_status, "
            "created_at, updated_at) VALUES "
            "('assignment', 'owner', 'draft', 0, 'auto', 0, 'idle', 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO workflow_operations "
            "(id, assignment_id, owner_id, operation_type, input_hash, attempt, "
            "status, progress, payload, created_at, updated_at) VALUES "
            "('operation', 'assignment', 'owner', 'submission_recognition', "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "1, 'pending', '{}', '{}', 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO stored_files "
            "(id, owner_id, kind, original_name, storage_backend, storage_key, "
            "content_type, size_bytes, sha256, assignment_id, created_at) VALUES "
            "('file', 'owner', 'submission_source', 'answers.pdf', 'local', "
            "'assignments/assignment/file/answers.pdf', 'application/pdf', 7, "
            "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
            "'assignment', 1)"
        ))

    command.upgrade(cfg, "head")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO workflow_source_items "
            "(id, owner_id, assignment_id, operation_id, attempt, order_index, "
            "stored_file_id, created_at) VALUES "
            "('source', 'owner', 'assignment', 'operation', 1, 0, 'file', 2)"
        ))
        connection.execute(text(
            "INSERT INTO workflow_source_outcomes "
            "(source_id, status, matched_answer_count, unknown_question_ids, "
            "retryable, created_at) VALUES "
            "('source', 'parsed', 1, '[]', 0, 2)"
        ))

    command.downgrade(cfg, "0004_structured_review_reasons")
    inspector = inspect(engine)
    assert "workflow_source_items" not in inspector.get_table_names()
    assert "workflow_source_outcomes" not in inspector.get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT id, teacher_id, name FROM assignments WHERE id='assignment'"
        )).one() == ("assignment", "owner", "Assignment")
        assert connection.execute(text(
            "SELECT id, attempt, status FROM workflow_operations WHERE id='operation'"
        )).one() == ("operation", 1, "pending")
        assert connection.execute(text(
            "SELECT id, original_name, sha256 FROM stored_files WHERE id='file'"
        )).one() == (
            "file",
            "answers.pdf",
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )

    command.upgrade(cfg, "head")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM workflow_source_items"
        )).scalar_one() == 0
        assert connection.execute(text(
            "SELECT COUNT(*) FROM workflow_source_outcomes"
        )).scalar_one() == 0


def test_structured_review_migration_preserves_real_zero_and_blocks_legacy_null(
    tmp_path, monkeypatch,
):
    """0004 must distinguish a real soft-review zero from a missing score."""
    from sqlalchemy import create_engine, text

    db_url = f"sqlite:///{(tmp_path / 'review-semantics.db').as_posix()}"
    cfg = _alembic_config(db_url, monkeypatch)
    command.upgrade(cfg, "0003_assignment_workflow_facade")
    engine = create_engine(db_url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users "
            "(id, username, role, password_hash, is_active, created_at, updated_at) "
            "VALUES ('teacher', 'teacher', 'teacher', 'h', 1, 1, 1), "
            "('student', 'student', 'student', 'h', 1, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO courses "
            "(id, name, code, description, teacher_id, created_at, updated_at) "
            "VALUES ('course', 'Course', '', '', 'teacher', 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO assignments "
            "(id, course_id, teacher_id, name, description, status, created_at, "
            "updated_at, version) VALUES "
            "('assignment', 'course', 'teacher', 'Assignment', '', 'published', 1, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO assignment_questions "
            "(id, assignment_id, q_id, order_index, number, type, stem, criterion, "
            "max_score, version, created_at, updated_at) VALUES "
            "('question-null', 'assignment', 'q-null', 0, '1', 'short', '', '', 10, 1, 1, 1), "
            "('question-zero', 'assignment', 'q-zero', 1, '2', 'short', '', '', 10, 1, 1, 1), "
            "('question-failed', 'assignment', 'q-failed', 2, '3', 'short', '', '', 10, 1, 1, 1), "
            "('question-reviewed', 'assignment', 'q-reviewed', 3, '4', 'short', '', '', 10, 1, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO submissions "
            "(id, assignment_id, student_id, current_revision_id, created_at, updated_at) "
            "VALUES ('submission', 'assignment', 'student', NULL, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO submission_revisions "
            "(id, submission_id, revision_number, source, file_name, created_at) "
            "VALUES ('revision', 'submission', 1, 'online', '', 1)"
        ))
        connection.execute(text(
            "UPDATE submissions SET current_revision_id='revision' WHERE id='submission'"
        ))
        connection.execute(text(
            "INSERT INTO grading_runs "
            "(id, assignment_id, teacher_id, status, total_submissions, "
            "completed_submissions, failed_submissions, created_at, completed_at) "
            "VALUES ('run', 'assignment', 'teacher', 'completed', 1, 1, 0, 1, 2)"
        ))
        connection.execute(text(
            "INSERT INTO grade_results "
            "(id, grading_run_id, submission_revision_id, question_id, student_id, "
            "q_id, ai_score, ai_max_score, ai_comment, ai_steps, ai_expert_results, "
            "requires_review, review_reason, result_status, created_at, updated_at) VALUES "
            "('legacy-null', 'run', 'revision', 'question-null', 'student', 'q-null', "
            "NULL, 10, '', '[]', '[]', 1, 'missing_correction', 'needs_review', 1, 1), "
            "('real-zero', 'run', 'revision', 'question-zero', 'student', 'q-zero', "
            "0, 10, '', '[]', '[]', 1, 'low_confidence', 'needs_review', 1, 1), "
            "('failed-sentinel', 'run', 'revision', 'question-failed', 'student', 'q-failed', "
            "0, 10, '', '[]', '[]', 1, 'llm_failed', 'failed', 1, 1), "
            "('reviewed-null', 'run', 'revision', 'question-reviewed', 'student', 'q-reviewed', "
            "NULL, 10, '', '[]', '[]', 1, 'teacher_edit_pending_confirmation', "
            "'needs_review', 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO teacher_reviews "
            "(id, grade_result_id, teacher_id, previous_score, previous_comment, "
            "new_score, new_comment, comment, confirmed, review_sequence, created_at) "
            "VALUES ('saved-review', 'reviewed-null', 'teacher', NULL, '', "
            "5, 'manual score', 'manual score', 0, 1, 2)"
        ))

    command.upgrade(cfg, "head")
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT id, result_status, ai_score FROM grade_results ORDER BY id"
        )).all()
        saved_review_score = connection.execute(text(
            "SELECT new_score FROM teacher_reviews "
            "WHERE grade_result_id = 'reviewed-null'"
        )).scalar_one()
    assert rows == [
        ("failed-sentinel", "failed", None),
        ("legacy-null", "failed", None),
        ("real-zero", "needs_review", 0.0),
        ("reviewed-null", "needs_review", None),
    ]
    assert saved_review_score == 5.0


def test_workflow_migration_preserves_existing_review_and_kb_link(
    tmp_path, monkeypatch,
):
    """0003 must backfill real 0002 rows, not only migrate an empty schema."""
    from sqlalchemy import create_engine, text

    db_url = f"sqlite:///{(tmp_path / 'preserve.db').as_posix()}"
    cfg = _alembic_config(db_url, monkeypatch)
    command.upgrade(cfg, "0002_course_library_tags")
    engine = create_engine(db_url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users "
            "(id, username, role, password_hash, is_active, created_at, updated_at) "
            "VALUES ('teacher', 'teacher', 'teacher', 'h', 1, 1, 1), "
            "('student', 'student', 'student', 'h', 1, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO courses "
            "(id, name, code, description, teacher_id, created_at, updated_at) "
            "VALUES ('course', 'Course', '', '', 'teacher', 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO assignments "
            "(id, course_id, teacher_id, name, description, status, created_at, "
            "updated_at, version) VALUES "
            "('assignment', 'course', 'teacher', 'Assignment', '', 'published', 1, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO assignment_questions "
            "(id, assignment_id, q_id, order_index, number, type, stem, criterion, "
            "max_score, version, created_at, updated_at) VALUES "
            "('question', 'assignment', 'q1', 0, '1', 'short', 'stem', 'rubric', "
            "10, 1, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO submissions "
            "(id, assignment_id, student_id, current_revision_id, created_at, updated_at) "
            "VALUES ('submission', 'assignment', 'student', NULL, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO submission_revisions "
            "(id, submission_id, revision_number, source, file_name, created_at) "
            "VALUES ('revision', 'submission', 1, 'online', '', 1)"
        ))
        connection.execute(text(
            "UPDATE submissions SET current_revision_id='revision' WHERE id='submission'"
        ))
        connection.execute(text(
            "INSERT INTO grading_runs "
            "(id, assignment_id, teacher_id, status, total_submissions, "
            "completed_submissions, failed_submissions, created_at, completed_at) "
            "VALUES ('run', 'assignment', 'teacher', 'completed', 1, 1, 0, 1, 2)"
        ))
        connection.execute(text(
            "INSERT INTO grade_results "
            "(id, grading_run_id, submission_revision_id, question_id, student_id, "
            "q_id, ai_score, ai_max_score, ai_comment, ai_steps, ai_expert_results, "
            "requires_review, review_reason, result_status, created_at, updated_at) VALUES "
            "('result', 'run', 'revision', 'question', 'student', 'q1', 8, 10, '', "
            "'[]', '[]', 1, 'legacy_low_confidence, minority_veto', "
            "'needs_review', 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO teacher_reviews "
            "(id, grade_result_id, teacher_id, previous_score, previous_comment, "
            "new_score, new_comment, comment, created_at) VALUES "
            "('review', 'result', 'teacher', 8, '', 9, 'confirmed', 'confirmed', 2)"
        ))
        connection.execute(text(
            "INSERT INTO teacher_reviews "
            "(id, grade_result_id, teacher_id, previous_score, previous_comment, "
            "new_score, new_comment, comment, created_at) VALUES "
            "('review-z', 'result', 'teacher', 9, 'confirmed', 9.5, 'later', 'later', 2)"
        ))
        connection.execute(text(
            "INSERT INTO knowledge_documents "
            "(id, owner_id, title, original_name, size_bytes, sha256, status, "
            "parser_version, chunk_count, created_at, updated_at) VALUES "
            "('document', 'teacher', 'Doc', 'doc.pdf', 1, "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "'ready', 'v1', 1, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO assignment_knowledge_documents "
            "(assignment_id, document_id, selected_at) "
            "VALUES ('assignment', 'document', 1)"
        ))

    command.upgrade(cfg, "head")
    with engine.connect() as connection:
        reviews = connection.execute(text(
            "SELECT id, confirmed, review_sequence FROM teacher_reviews "
            "WHERE grade_result_id='result' ORDER BY review_sequence"
        )).all()
        result = connection.execute(text(
            "SELECT initial_requires_review, review_reasons, initial_review_reasons "
            "FROM grade_results WHERE id='result'"
        )).one()
        link = connection.execute(text(
            "SELECT source_kind, library_material_id "
            "FROM assignment_knowledge_documents "
            "WHERE assignment_id='assignment' AND document_id='document'"
        )).one()
    assert [(row.id, bool(row.confirmed), row.review_sequence) for row in reviews] == [
        ("review", True, 1),
        ("review-z", True, 2),
    ]
    assert bool(result.initial_requires_review) is True
    assert json.loads(result.review_reasons) == [
        "legacy_low_confidence",
        "minority_veto",
    ]
    assert json.loads(result.initial_review_reasons) == [
        "legacy_low_confidence",
        "minority_veto",
    ]
    assert link.source_kind == "upload"
    assert link.library_material_id is None

    command.downgrade(cfg, "0002_course_library_tags")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM teacher_reviews WHERE grade_result_id='result'"
        )).scalar_one() == 2
        assert connection.execute(text(
            "SELECT COUNT(*) FROM assignment_knowledge_documents "
            "WHERE assignment_id='assignment' AND document_id='document'"
        )).scalar_one() == 1
        assert connection.execute(text(
            "SELECT review_reason FROM grade_results WHERE id='result'"
        )).scalar_one() == "legacy_low_confidence,minority_veto"


def _postgresql_sql(monkeypatch, revision: str) -> str:
    from backend.config import settings

    database_url = "postgresql+psycopg://smartai:smartai@localhost/smartai_test"
    monkeypatch.setenv("SMARTAI_DATABASE_URL", database_url)
    monkeypatch.setenv("SMARTAI_DATABASE_HEAVY", "ON")
    monkeypatch.setattr(settings, "database_heavy", True)
    output = StringIO()
    cfg = Config("alembic.ini", output_buffer=output)
    cfg.set_main_option("script_location", "backend/db/migrations")
    if ":" in revision:
        command.downgrade(cfg, revision, sql=True)
    else:
        command.upgrade(cfg, revision, sql=True)
    return output.getvalue()


def test_postgresql_upgrade_adds_deferred_foreign_keys_after_tables(monkeypatch):
    sql = _postgresql_sql(monkeypatch, "head")
    deferred_constraints = {
        "fk_stored_files_submission_revision": ("stored_files", "submission_revisions"),
        "fk_stored_files_knowledge_document": ("stored_files", "knowledge_documents"),
        "fk_knowledge_documents_stored_file": ("knowledge_documents", "stored_files"),
        "fk_submissions_current_revision": ("submissions", "submission_revisions"),
        "fk_submission_revisions_submission": ("submission_revisions", "submissions"),
    }

    for constraint, (source_table, target_table) in deferred_constraints.items():
        alter_position = sql.index(f"ADD CONSTRAINT {constraint}")
        assert sql.index(f"CREATE TABLE {source_table}") < alter_position
        assert sql.index(f"CREATE TABLE {target_table}") < alter_position

    stored_files_definition = sql.split("CREATE TABLE stored_files", 1)[1].split(");", 1)[0]
    assert "REFERENCES submission_revisions" not in stored_files_definition
    assert "REFERENCES knowledge_documents" not in stored_files_definition


def test_postgresql_upgrade_uses_portable_boolean_defaults(monkeypatch):
    """Boolean column defaults must be portable between SQLite and PostgreSQL.

    PostgreSQL rejects integer literals as defaults for a BOOLEAN column
    (``column "is_active" is of type boolean but default expression is of type
    integer``), so the baseline migration must emit a real boolean default
    (``true``/``false``) rather than ``1``/``0``. SQLite accepts both forms, so
    switching to boolean literals keeps the SQLite round-trip working while
    unblocking the PostgreSQL service-migration job.
    """
    import re

    sql = _postgresql_sql(monkeypatch, "head")

    boolean_columns = {
        "is_active": "true",
        "enabled": "true",
        "requires_review": "false",
    }
    for column, expected_literal in boolean_columns.items():
        pattern = re.compile(
            rf"\b{column}\s+BOOLEAN\s+DEFAULT\s+(\S+)\s+NOT\s+NULL",
            re.IGNORECASE,
        )
        match = pattern.search(sql)
        assert match is not None, (
            f"expected a BOOLEAN DEFAULT for {column!r} in the PostgreSQL DDL"
        )
        default = match.group(1).rstrip(",")
        assert default.lower() == expected_literal, (
            f"{column!r} default must be the boolean literal {expected_literal!r} "
            f"on PostgreSQL, got {default!r}"
        )


def test_postgresql_downgrade_drops_deferred_foreign_keys_before_tables(monkeypatch):
    sql = _postgresql_sql(monkeypatch, "0001_normalized_learning:base")
    constraint_tables = {
        "fk_stored_files_submission_revision": "stored_files",
        "fk_stored_files_knowledge_document": "stored_files",
        "fk_knowledge_documents_stored_file": "knowledge_documents",
        "fk_submissions_current_revision": "submissions",
        "fk_submission_revisions_submission": "submission_revisions",
    }

    for constraint, table_name in constraint_tables.items():
        assert sql.index(f"DROP CONSTRAINT {constraint}") < sql.index(
            f"DROP TABLE {table_name}"
        )


def test_postgresql_source_outcome_ddl_is_portable(monkeypatch):
    sql = _postgresql_sql(monkeypatch, "0005_workflow_source_outcomes")

    assert "CREATE TABLE workflow_source_items" in sql
    assert "CREATE TABLE workflow_source_outcomes" in sql
    assert "retryable BOOLEAN NOT NULL" in sql
    assert "ck_workflow_source_outcomes_status" in sql
    assert "ck_workflow_source_outcomes_matched_count_nonnegative" in sql
    assert sql.index("CREATE TABLE workflow_source_items") < sql.index(
        "CREATE TABLE workflow_source_outcomes"
    )


def test_postgresql_source_outcome_downgrade_drops_child_first(monkeypatch):
    sql = _postgresql_sql(
        monkeypatch,
        "0005_workflow_source_outcomes:0004_structured_review_reasons",
    )

    assert sql.index("DROP TABLE workflow_source_outcomes") < sql.index(
        "DROP TABLE workflow_source_items"
    )


def test_postgresql_operation_checkpoint_ddl_is_portable(monkeypatch):
    from importlib import import_module

    migration = import_module(
        "backend.db.migrations.versions.0006_workflow_operation_checkpoints"
    )
    assert len(migration.revision) <= 32
    sql = _postgresql_sql(monkeypatch, "0006_operation_checkpoints")

    assert "ADD COLUMN checkpoint_revision INTEGER DEFAULT 0 NOT NULL" in sql
    assert "ADD COLUMN checkpoint_stage VARCHAR(64)" in sql
    assert "ADD COLUMN checkpoint JSON DEFAULT '{}' NOT NULL" in sql
    assert "ADD COLUMN artifact_refs JSON DEFAULT '[]' NOT NULL" in sql
    assert "ADD COLUMN terminal_summary JSON" in sql
    assert "ck_workflow_operations_checkpoint_revision_nonnegative" in sql
    assert "checkpoint_revision >= 0" in sql


def test_operation_lease_migration_has_contract_columns(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect

    db_url = f"sqlite:///{(tmp_path / 'lease-contract.db').as_posix()}"
    cfg = _alembic_config(db_url, monkeypatch)
    command.upgrade(cfg, "head")
    inspector = inspect(create_engine(db_url))

    columns = {
        column["name"]: column
        for column in inspector.get_columns("workflow_operations")
    }
    assert {
        "lease_owner",
        "lease_token",
        "lease_expires_at",
        "lease_heartbeat_at",
    } <= columns.keys()
    assert columns["lease_owner"]["nullable"] is True
    assert columns["lease_token"]["nullable"] is True
    assert columns["lease_expires_at"]["nullable"] is True
    assert columns["lease_heartbeat_at"]["nullable"] is True

    checks = {
        item["name"]: item["sqltext"]
        for item in inspector.get_check_constraints("workflow_operations")
    }
    assert "ck_workflow_operations_lease_consistency" in checks
    lease_check = checks["ck_workflow_operations_lease_consistency"]
    # An inactive lease has all four lease fields null; an active lease has
    # owner, token, expiry, and heartbeat all non-null.
    for column in ("lease_owner", "lease_token", "lease_expires_at", "lease_heartbeat_at"):
        assert f"{column} IS NULL" in lease_check
        assert f"{column} IS NOT NULL" in lease_check
    index_columns = {
        tuple(item["column_names"])
        for item in inspector.get_indexes("workflow_operations")
    }
    assert ("status", "lease_expires_at") in index_columns


def test_operation_lease_migration_preserves_0006_operation(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text

    db_url = f"sqlite:///{(tmp_path / 'lease-preserve.db').as_posix()}"
    cfg = _alembic_config(db_url, monkeypatch)
    command.upgrade(cfg, "0006_operation_checkpoints")
    engine = create_engine(db_url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users "
            "(id, username, role, password_hash, is_active, created_at, updated_at) "
            "VALUES ('owner', 'owner', 'teacher', 'h', 1, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO courses "
            "(id, name, code, description, teacher_id, created_at, updated_at) "
            "VALUES ('course', 'Course', '', '', 'owner', 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO assignments "
            "(id, course_id, teacher_id, name, description, status, created_at, "
            "updated_at, version) VALUES "
            "('assignment', 'course', 'owner', 'Assignment', '', 'draft', 1, 1, 1)"
        ))
        connection.execute(text(
            "INSERT INTO workflow_operations "
            "(id, assignment_id, owner_id, operation_type, input_hash, attempt, "
            "status, progress, payload, created_at, updated_at) VALUES "
            "('operation', 'assignment', 'owner', 'submission_recognition', "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "1, 'running', '{}', '{}', 1, 1)"
        ))

    command.upgrade(cfg, "head")
    with engine.begin() as connection:
        assert connection.execute(text(
            "SELECT lease_owner, lease_token, lease_expires_at, lease_heartbeat_at "
            "FROM workflow_operations WHERE id='operation'"
        )).one() == (None, None, None, None)
        connection.execute(text(
            "UPDATE workflow_operations SET lease_owner='worker', "
            "lease_token='token', lease_expires_at=100, lease_heartbeat_at=99 "
            "WHERE id='operation'"
        ))

    command.downgrade(cfg, "0006_operation_checkpoints")
    columns = {
        item["name"]
        for item in inspect(engine).get_columns("workflow_operations")
    }
    assert "lease_owner" not in columns
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT id, attempt, status FROM workflow_operations WHERE id='operation'"
        )).one() == ("operation", 1, "running")

    command.upgrade(cfg, "head")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT lease_owner, lease_token, lease_expires_at, lease_heartbeat_at "
            "FROM workflow_operations WHERE id='operation'"
        )).one() == (None, None, None, None)


def test_postgresql_operation_lease_ddl_is_portable(monkeypatch):
    from importlib import import_module

    migration = import_module(
        "backend.db.migrations.versions.0008_operation_leases"
    )
    assert len(migration.revision) <= 32
    sql = _postgresql_sql(monkeypatch, "0008_operation_leases")

    assert "ADD COLUMN lease_owner VARCHAR(128)" in sql
    assert "ADD COLUMN lease_token VARCHAR(64)" in sql
    assert "ADD COLUMN lease_expires_at FLOAT" in sql
    assert "ADD COLUMN lease_heartbeat_at FLOAT" in sql
    assert "ck_workflow_operations_lease_consistency" in sql
    lease_check_sql = sql[sql.index("ck_workflow_operations_lease_consistency"):]
    for column in ("lease_owner", "lease_token", "lease_expires_at", "lease_heartbeat_at"):
        assert f"{column} IS NULL" in lease_check_sql
        assert f"{column} IS NOT NULL" in lease_check_sql
    assert "CREATE INDEX ix_workflow_operations_claimable" in sql
def test_postgresql_source_diagnostics_ddl_is_portable(monkeypatch):
    from importlib import import_module

    migration = import_module(
        "backend.db.migrations.versions.0007_source_outcome_diagnostics"
    )
    assert len(migration.revision) <= 32
    sql = _postgresql_sql(monkeypatch, "0007_source_outcome_diagnostics")

    assert "ADD COLUMN failure_phase VARCHAR(64)" in sql
    assert "ADD COLUMN source_id VARCHAR(64)" in sql
    assert "fk_assignment_student_presentations_source" in sql
    assert "ON DELETE SET NULL" in sql
    assert "ix_assignment_student_presentations_source_id" in sql
