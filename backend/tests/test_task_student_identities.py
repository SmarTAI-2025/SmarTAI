"""Teacher identities survive without authentication accounts or enrollments."""
from importlib import import_module

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import select, text

from backend.db.models import (
    CourseEnrollmentRecord, CourseRecord, GradeResultRecord,
    GradingRunSubmissionRecord, SubmissionRecord, SubmissionRevisionRecord,
    UserRecord,
)
from backend.db.session import configure_database, session_scope
from backend.db.workflow_repository import AssignmentStudentPresentationRecord
from backend.tests import test_normalized_analytics as fixtures


def test_teacher_import_and_grading_need_no_student_login_or_enrollment():
    from backend.services import task_facade
    from backend.tests.test_workflow_facade_integrity import _seed_figma_grading_task

    owner = "identity-teacher"
    assignment, _, workflow = _seed_figma_grading_task(owner)
    with session_scope() as session:
        assert list(session.scalars(select(UserRecord.id))) == [owner]
        assert list(session.scalars(select(CourseEnrollmentRecord.student_id))) == []
        identity = session.scalar(select(AssignmentStudentPresentationRecord))
        student_id = identity.student_id
        assert identity.display_name == "Student One"
        assert identity.display_student_id == "S001"
        assert session.scalar(select(SubmissionRecord.student_id)) == student_id
    started = task_facade.start_task_grading(
        task_id=assignment.id, owner_id=owner,
        expected_workflow_revision=workflow.workflow_revision,
    )
    with session_scope() as session:
        assert session.scalar(select(GradingRunSubmissionRecord.student_id).where(
            GradingRunSubmissionRecord.grading_run_id == started["job_id"],
        )) == student_id
        assert session.get(UserRecord, student_id) is None


def test_analytics_preserves_names_results_and_owner_isolation_without_users():
    from backend.api.analytics import _load_facts, _load_filter_identifiers
    from sqlalchemy import delete

    owner = fixtures._user("teacher", "identity-owner")
    seeded = fixtures._seed_graded_assignment(owner)
    student = seeded["students"][0]
    with session_scope() as session:
        session.add(AssignmentStudentPresentationRecord(
            id="identity-display", assignment_id=seeded["task_id"],
            student_id=student.id, display_name="Imported Name", display_student_id="S001",
        ))
        session.execute(delete(UserRecord).where(UserRecord.id == student.id))
    facts = _load_facts(seeded["task_id"], owner.id)
    assert facts.student_names[student.id] == "Imported Name"
    assert len(facts.results) == 3
    assert facts.results[0].score == 8.5  # Teacher review survived.
    assert {student.id, "Imported Name", "S001"} <= _load_filter_identifiers(seeded["task_id"], owner.id)
    with pytest.raises(HTTPException) as denied:
        _load_facts(seeded["task_id"], "another-teacher")
    assert denied.value.status_code == 404


def _seed_legacy_imports(monkeypatch):
    original_user = fixtures._user
    owner = original_user("teacher", "migration-owner")

    def imported_user(role, label):
        user = original_user(role, label)
        if label.endswith("s3"):
            return user  # A real student account must remain untouched.
        identity = "imported_" + label.rsplit("s", 1)[-1]
        username = "imported-" + identity[9:] + "-sample"
        with session_scope() as session:
            row = session.get(UserRecord, user.id)
            row.id = identity
            row.username = username
            row.email = None
            row.is_active = False
            row.password_hash = "!disabled-imported-account"
        return user.model_copy(update={"id": identity, "username": username})

    monkeypatch.setattr(fixtures, "_user", imported_user)
    seeded = fixtures._seed_graded_assignment(owner)
    with session_scope() as session:
        course_id = session.scalar(select(CourseRecord.id).where(CourseRecord.teacher_id == owner.id))
        for user in seeded["students"]:
            session.add(AssignmentStudentPresentationRecord(
                id="presentation-" + user.id, assignment_id=seeded["task_id"],
                student_id=user.id, display_student_id=user.id, display_name=user.username,
            ))
            session.add(CourseEnrollmentRecord(course_id=course_id, student_id=user.id))
        for row in session.scalars(select(SubmissionRecord)):
            session.add(GradingRunSubmissionRecord(
                id="run-sub-" + row.id, grading_run_id=seeded["run_id"],
                submission_revision_id=row.current_revision_id, student_id=row.student_id,
            ))
        # A placeholder owning unrelated data is preserved, never cascaded.
        session.add(CourseRecord(
            id="protected-course", teacher_id=seeded["students"][1].id, name="Keep",
        ))
    return seeded


def _business_snapshot(engine):
    tables = (
        "assignments", "assignment_questions", "submissions", "submission_revisions",
        "submission_answers", "grading_runs", "grading_run_submissions", "grade_results",
        "teacher_reviews", "assignment_student_presentations",
    )
    with engine.connect() as connection:
        return {table: connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).all() for table in tables}


def test_upgrade_preserves_existing_work_and_only_removes_safe_placeholders(tmp_path, monkeypatch):
    from backend.config import settings
    url = f"sqlite:///{tmp_path / 'identity-migration.db'}"
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "database_url_light", url)
    monkeypatch.setattr(settings, "database_heavy", False)
    config = Config("alembic.ini")
    config.attributes.update(database_url=url, database_heavy=False)
    command.upgrade(config, "0026_image_capability")
    engine = configure_database(url)
    seeded = _seed_legacy_imports(monkeypatch)
    before = _business_snapshot(engine)
    command.upgrade(config, "head")
    assert _business_snapshot(engine) == before
    removable, protected, real = [student.id for student in seeded["students"]]
    with session_scope() as session:
        assert session.get(UserRecord, removable) is None
        assert session.get(UserRecord, protected) is not None
        assert session.get(UserRecord, real) is not None
        assert not list(session.scalars(select(CourseEnrollmentRecord).where(CourseEnrollmentRecord.student_id == removable)))
        assert session.get(CourseRecord, "protected-course") is not None
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    command.downgrade(config, "0026_image_capability")
    assert _business_snapshot(engine) == before
    with session_scope() as session:
        restored = session.get(UserRecord, removable)
        assert restored.is_active is False
        assert restored.password_hash == "!disabled-imported-account"
    command.upgrade(config, "head")
    assert _business_snapshot(engine) == before


def test_migration_accounts_for_every_remaining_user_foreign_key():
    from backend.db.base import Base
    migration = import_module("backend.db.migrations.versions.0027_task_student_identities")
    references = {
        (table.name, column.name)
        for table in Base.metadata.tables.values() for column in table.columns
        if any(fk.column.table.name == "users" for fk in column.foreign_keys)
    }
    assert references == set(migration.USER_REFERENCES) | {("course_enrollments", "student_id")}
