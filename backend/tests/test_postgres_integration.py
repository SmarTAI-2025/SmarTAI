"""PostgreSQL integration for the normalized education invariants (Task 14).

Runs only when ``SMARTAI_TEST_POSTGRES_URL`` points at a real PostgreSQL
database; otherwise the suite is skipped. Locally we run the SQLite suite, so
this file records that real PostgreSQL execution happens in GitHub Actions
rather than pretending it ran here. When enabled, it verifies the rules that
differ between SQLite and PostgreSQL against a live database:

* the single-active-run partial unique index rejects a second active run;
* an optimistic assignment UPDATE with a stale version is a 409 conflict;
* the lease claim predicate is owner-scoped;
* role-scoped assignment/submission reads hide other owners.
"""
from __future__ import annotations

import os
import uuid

import pytest

PG_URL = os.environ.get("SMARTAI_TEST_POSTGRES_URL")


pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="Set SMARTAI_TEST_POSTGRES_URL to run PostgreSQL integration (GitHub Actions).",
)


@pytest.fixture
def pg_database():
    from alembic import command
    from alembic.config import Config
    from backend.config import settings
    from backend.db.session import configure_database
    from sqlalchemy import create_engine

    old_heavy = settings.database_heavy
    settings.database_heavy = True
    configure_database(PG_URL)
    engine = create_engine(PG_URL)
    config = Config("alembic.ini")
    config.set_main_option("script_location", "backend/db/migrations")
    old_url = os.environ.get("SMARTAI_DATABASE_URL")
    old_heavy_env = os.environ.get("SMARTAI_DATABASE_HEAVY")
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


def _seed_user(role: str) -> str:
    from backend.db.models import UserRecord
    from backend.db.session import session_scope
    uid = f"pg_{role}_{uuid.uuid4().hex[:8]}"
    with session_scope() as session:
        session.add(UserRecord(id=uid, username=uid, role=role, password_hash="x", is_active=True))
    return uid


def test_postgres_single_active_run(pg_database):
    from backend.db import course_repository, assignment_repository, grading_repository
    from backend.domain import education
    from backend.domain.errors import DuplicateActiveRun

    teacher = _seed_user("teacher")
    student = _seed_user("student")
    course = course_repository.create_course(teacher_id=teacher, name="C")
    course_repository.enroll(course_id=course.id, student_id=student)
    asg = assignment_repository.create_assignment(teacher_id=teacher, course_id=course.id, name="A")
    assignment_repository.add_question(
        assignment_id=asg.id, teacher_id=teacher, q_id="q1", order_index=0, type="short", stem="?",
    )
    assignment_repository.publish(assignment_id=asg.id, teacher_id=teacher, expected_version=1)

    grading_repository.create_run(assignment_id=asg.id, teacher_id=teacher, total_submissions=1)
    with pytest.raises(DuplicateActiveRun):
        grading_repository.create_run(assignment_id=asg.id, teacher_id=teacher, total_submissions=1)


def test_postgres_optimistic_update_conflict(pg_database):
    from backend.db import assignment_repository, course_repository
    from backend.domain.errors import VersionConflict

    teacher = _seed_user("teacher")
    course = course_repository.create_course(teacher_id=teacher, name="C")
    asg = assignment_repository.create_assignment(teacher_id=teacher, course_id=course.id, name="A")
    # Bump version out-of-band, then a stale client update must conflict.
    assignment_repository.rename_assignment(assignment_id=asg.id, teacher_id=teacher, expected_version=1, name="v2")
    with pytest.raises(VersionConflict):
        assignment_repository.rename_assignment(assignment_id=asg.id, teacher_id=teacher, expected_version=1, name="stale")


def test_postgres_lease_claim_is_owner_scoped(pg_database):
    from backend.db import course_repository, assignment_repository, grading_repository
    from backend.domain.errors import LeaseLost

    teacher = _seed_user("teacher")
    student = _seed_user("student")
    course = course_repository.create_course(teacher_id=teacher, name="C")
    course_repository.enroll(course_id=course.id, student_id=student)
    asg = assignment_repository.create_assignment(teacher_id=teacher, course_id=course.id, name="A")
    assignment_repository.add_question(
        assignment_id=asg.id, teacher_id=teacher, q_id="q1", order_index=0, type="short", stem="?",
    )
    assignment_repository.publish(assignment_id=asg.id, teacher_id=teacher, expected_version=1)

    run = grading_repository.create_run(assignment_id=asg.id, teacher_id=teacher, total_submissions=1)
    grading_repository.claim_lease(run_id=run.id, worker_id="w1", lease_seconds=60)
    with pytest.raises(LeaseLost):
        grading_repository.claim_lease(run_id=run.id, worker_id="w2", lease_seconds=60)


def test_postgres_role_scoped_reads_hide_other_owner(pg_database):
    from backend.db import assignment_repository, course_repository
    from backend.domain.errors import NotFound

    teacher = _seed_user("teacher")
    other = _seed_user("teacher")
    course = course_repository.create_course(teacher_id=teacher, name="C")
    asg = assignment_repository.create_assignment(teacher_id=teacher, course_id=course.id, name="Secret")
    with pytest.raises(NotFound):
        assignment_repository.get_assignment(assignment_id=asg.id, actor_id=other)


def test_postgres_source_outcome_persistence_and_owner_isolation(
    pg_database,
    tmp_path,
):
    from backend.db import (
        assignment_repository,
        course_repository,
        source_outcome_repository,
        workflow_repository,
    )
    from backend.db.file_repository import save_file
    from backend.domain.errors import NotFound
    from backend.storage.local import LocalStorage

    teacher = _seed_user("teacher")
    other = _seed_user("teacher")
    course = course_repository.create_course(teacher_id=teacher, name="C")
    assignment = assignment_repository.create_assignment(
        teacher_id=teacher,
        course_id=course.id,
        name="A",
    )
    workflow_repository.ensure_workflow(
        assignment_id=assignment.id,
        owner_id=teacher,
    )
    operation, _ = workflow_repository.create_operation(
        assignment_id=assignment.id,
        owner_id=teacher,
        operation_type="submission_recognition",
        input_hash=uuid.uuid4().hex,
    )
    stored = save_file(
        storage=LocalStorage(tmp_path / "pg-source-files"),
        owner_id=teacher,
        kind="submission_source",
        original_name="answers.pdf",
        content=b"answers",
        content_type="application/pdf",
        assignment_id=assignment.id,
    )
    source, created = source_outcome_repository.register_source(
        owner_id=teacher,
        assignment_id=assignment.id,
        operation_id=operation.id,
        expected_attempt=operation.attempt,
        order_index=0,
        stored_file_id=stored.id,
    )
    source_outcome_repository.record_outcome(
        source_id=source.id,
        owner_id=teacher,
        status="parse_failed",
        student_candidate=None,
        matched_answer_count=0,
        unknown_question_ids=[],
        stable_error_code="submission_parse_failed",
        retryable=True,
    )
    summary = source_outcome_repository.summarize_sources(
        operation_id=operation.id,
        owner_id=teacher,
        attempt=operation.attempt,
    )

    assert created is True
    assert summary.uploaded_count == 1
    assert summary.failed_count == 1
    assert summary.is_complete is True
    with pytest.raises(NotFound):
        source_outcome_repository.get_source(source.id, owner_id=other)
    with pytest.raises(NotFound):
        source_outcome_repository.summarize_sources(
            operation_id=operation.id,
            owner_id=other,
            attempt=operation.attempt,
        )
