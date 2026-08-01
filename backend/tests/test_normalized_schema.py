"""Normalized education schema baseline (Task 1).

Asserts that a fresh database contains exactly the retained auth/provider/
knowledge tables plus the normalized education tables, and that the structural
invariants from the approved design are enforced in metadata (not just by
application code): no ``users.course_ids`` compatibility mirror, a non-null
assignment ``version`` for optimistic locking, the course/enrollment and
assignment/student uniqueness rules, immutable submission revision numbering,
and the single-active-grading-run partial unique index.
"""
from __future__ import annotations

from sqlalchemy import inspect

from backend.db.base import Base
from backend.db.session import configure_database, create_schema


EXPECTED_EDUCATION_TABLES = {
    "courses",
    "course_enrollments",
    "assignments",
    "assignment_questions",
    "assignment_knowledge_documents",
    "submissions",
    "submission_revisions",
    "submission_answers",
    "grading_runs",
    "grading_run_submissions",
    "grading_run_events",
    "grade_results",
    "teacher_reviews",
    "stored_files",
}

RETAINED_TABLES = {
    "users",
    "invite_codes",
    "refresh_sessions",
    "provider_configs",
    "knowledge_documents",
    "knowledge_chunks",
}

PRESENTATION_TABLES = {
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
}

LEGACY_TABLES = {"tasks", "grading_jobs", "task_knowledge_documents"}


def _inspector():
    configure_database()
    create_schema()
    return inspect(configure_database())


def test_fresh_database_contains_normalized_and_retained_tables():
    tables = set(_inspector().get_table_names())
    missing = (EXPECTED_EDUCATION_TABLES | RETAINED_TABLES | PRESENTATION_TABLES) - tables
    assert not missing, f"missing tables: {sorted(missing)}"


def test_legacy_tables_are_absent():
    tables = set(_inspector().get_table_names())
    leftover = LEGACY_TABLES & tables
    assert not leftover, f"legacy tables still present: {sorted(leftover)}"


def test_users_table_has_no_course_ids_column():
    columns = {col["name"] for col in _inspector().get_columns("users")}
    assert "course_ids" not in columns, (
        "users.course_ids compatibility mirror must be removed; course_enrollments "
        "is the single source of membership truth"
    )


def test_assignments_version_is_non_null():
    columns = {col["name"]: col for col in _inspector().get_columns("assignments")}
    assert "version" in columns, "assignments must carry an optimistic-lock version column"
    assert columns["version"]["nullable"] is False, (
        "assignments.version must be NOT NULL so optimistic updates can rely on it"
    )


def test_course_enrollment_unique_constraint():
    uniques = _collect_unique_constraints("course_enrollments")
    assert ("course_id", "student_id") in uniques, (
        "course_enrollments must enforce (course_id, student_id) uniqueness in SQL"
    )


def test_assignment_student_unique_constraint():
    # The design models "one current submission per (assignment, student)" at the
    # submissions table; assignments themselves do not duplicate per student, but
    # the (assignment_id, student_id) uniqueness lives on submissions.
    uniques = _collect_unique_constraints("submissions")
    assert ("assignment_id", "student_id") in uniques, (
        "submissions must enforce (assignment_id, student_id) uniqueness so a "
        "student has exactly one current submission per assignment"
    )


def test_submission_revision_unique_constraint():
    uniques = _collect_unique_constraints("submission_revisions")
    assert ("submission_id", "revision_number") in uniques, (
        "submission_revisions must enforce (submission_id, revision_number) "
        "uniqueness so immutable revision numbering cannot collide"
    )


def test_submission_answer_unique_constraint():
    uniques = _collect_unique_constraints("submission_answers")
    assert ("revision_id", "question_id") in uniques, (
        "submission_answers must enforce (revision_id, question_id) uniqueness"
    )


def test_teacher_review_sequence_is_unique_per_result():
    uniques = _collect_unique_constraints("teacher_reviews")
    assert ("grade_result_id", "review_sequence") in uniques, (
        "teacher review ordering must remain unambiguous under concurrent writes"
    )


def test_grading_run_single_active_partial_unique_index():
    """The active-run partial unique index must exist for both SQLite and PostgreSQL.

    A plain unique index would forbid any second run ever, but a *partial* unique
    index scoped to non-terminal statuses enforces "at most one active run per
    assignment" while still allowing historical runs. We assert its presence in
    the dialect metadata rather than only at runtime, since SQLite does not store
    partial-index WHERE clauses introspectably on older versions.
    """
    from backend.db.models import GradingRunRecord

    table = GradingRunRecord.__table__
    index_names = {idx.name for idx in table.indexes}
    assert any(
        name and "grading_run" in name and "active" in name for name in index_names
    ), (
        "grading_runs must declare a named partial unique index on (assignment_id) "
        f"for active runs; found indexes: {sorted(index_names)}"
    )
    # At least one such index must be unique (partial unique semantics).
    active_unique = [
        idx for idx in table.indexes
        if idx.name and "grading_run" in idx.name and "active" in idx.name and idx.unique
    ]
    assert active_unique, (
        "the active-run index must be UNIQUE so the DB rejects two concurrent "
        "active runs for the same assignment"
    )


def _collect_unique_constraints(table_name: str) -> set[tuple[str, ...]]:
    inspector = _inspector()
    constraints = set()
    for constraint in inspector.get_unique_constraints(table_name):
        cols = tuple(constraint.get("column_names", ()))
        if cols:
            constraints.add(cols)
    return constraints
