"""Migration round-trip smoke (Task 1).

Verifies the single normalized baseline is reversible on SQLite: starting from
an empty DB, ``upgrade head → downgrade base → upgrade head`` must all succeed.
This catches batch-alter downgrade issues (constraint names, dropped columns)
that an upgrade-only check misses, and proves the clean baseline creates every
retained + education table from an empty database.
"""
from __future__ import annotations

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
        "course_material_groups",
        "course_materials",
        "tags",
        "assignment_tags",
        "assignment_workflows",
        "task_create_idempotency",
        "workflow_operations",
        "assignment_student_presentations",
        "submission_answer_presentations",
        "grading_run_setups",
        "result_artifact_manifests",
    } <= tables
    # Legacy tables must not reappear after the roundtrip.
    assert not ({"tasks", "grading_jobs", "task_knowledge_documents"} & tables)


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
            "'[]', '[]', 1, 'legacy_low_confidence', 'needs_review', 1, 1)"
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
            "SELECT initial_requires_review, initial_review_reason "
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
    assert result.initial_review_reason == "legacy_low_confidence"
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
