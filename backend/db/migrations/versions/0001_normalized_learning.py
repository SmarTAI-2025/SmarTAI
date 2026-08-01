"""normalized education domain baseline

Revision ID: 0001_normalized_learning
Revises:
Create Date: 2026-07-20

Clean development-era baseline for the normalized course → assignment →
submission → grading → review → release workflow. Replaces the legacy
0001–0004 chain and the old tasks/grading_jobs JSON-payload aggregates.
Local development data has no migration value, so this revision builds every
retained and education table from an empty database and its downgrade drops
them all — there is no legacy archive or double-write path.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "0001_normalized_learning"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _defer_cyclic_foreign_keys() -> bool:
    """Use ALTER TABLE for PostgreSQL's forward/cyclic references.

    SQLite accepts references to tables created later, but cannot add a
    foreign key with a standalone ALTER TABLE. Keeping those constraints
    inline on SQLite preserves the existing local schema behavior.
    """
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    # ── Identity ───────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("role IN ('teacher', 'student', 'admin')", name="ck_users_role"),
    )
    op.create_index(op.f("ix_users_username"), "users", ["username"], unique=True)
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    op.create_table(
        "courses",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("code", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("teacher_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["teacher_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_courses_teacher_id", "courses", ["teacher_id"])

    op.create_table(
        "invite_codes",
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("course_id", sa.String(length=64), nullable=True),
        sa.Column("invited_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("used_at", sa.Float(), nullable=True),
        sa.Column("used_by", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["used_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("code"),
        sa.CheckConstraint("role IN ('teacher', 'student', 'admin')", name="ck_invite_codes_role"),
    )
    op.create_index(op.f("ix_invite_codes_expires_at"), "invite_codes", ["expires_at"], unique=False)
    op.create_index(op.f("ix_invite_codes_invited_by"), "invite_codes", ["invited_by"], unique=False)
    op.create_index(op.f("ix_invite_codes_course_id"), "invite_codes", ["course_id"], unique=False)

    op.create_table(
        "refresh_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("last_used_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("revoked_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_refresh_sessions_expires_at"), "refresh_sessions", ["expires_at"], unique=False)
    op.create_index(op.f("ix_refresh_sessions_token_hash"), "refresh_sessions", ["token_hash"], unique=True)
    op.create_index(op.f("ix_refresh_sessions_user_id"), "refresh_sessions", ["user_id"], unique=False)

    op.create_table(
        "provider_configs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("provider_type", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("base_url", sa.String(length=1024), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("nonce", sa.String(length=128), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("max_concurrent", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("rpm", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "provider_type", "model",
            name="uq_provider_configs_owner_provider_model",
        ),
    )
    op.create_index("ix_provider_configs_owner_id", "provider_configs", ["owner_id"])

    op.create_table(
        "assignments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("course_id", sa.String(length=64), nullable=False),
        sa.Column("teacher_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
        sa.Column("due_at", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("published_at", sa.Float(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["teacher_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('draft', 'ready', 'published', 'closed', 'archived')",
            name="ck_assignments_status",
        ),
    )
    op.create_index("ix_assignments_course_id", "assignments", ["course_id"])
    op.create_index("ix_assignments_teacher_id", "assignments", ["teacher_id"])
    op.create_index("ix_assignments_status", "assignments", ["status"])

    op.create_table(
        "assignment_questions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("assignment_id", sa.String(length=64), nullable=False),
        sa.Column("q_id", sa.String(length=64), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("number", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("stem", sa.Text(), nullable=False, server_default=""),
        sa.Column("criterion", sa.Text(), nullable=False, server_default=""),
        sa.Column("max_score", sa.Float(), nullable=False, server_default="10.0"),
        sa.Column("reference_answer", sa.Text(), nullable=True),
        sa.Column("test_cases", sa.JSON(), nullable=True),
        sa.Column("source", sa.JSON(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "assignment_id", "q_id", "version",
            name="uq_assignment_questions_assignment_qid_version",
        ),
    )
    op.create_index("ix_assignment_questions_assignment_id", "assignment_questions", ["assignment_id"])

    op.create_table(
        "stored_files",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("original_name", sa.String(length=512), nullable=False),
        sa.Column("storage_backend", sa.String(length=64), nullable=False, server_default="local"),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("assignment_id", sa.String(length=64), nullable=True),
        sa.Column("submission_revision_id", sa.String(length=64), nullable=True),
        sa.Column("knowledge_document_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        *([] if _defer_cyclic_foreign_keys() else [
            sa.ForeignKeyConstraint(
                ["submission_revision_id"], ["submission_revisions.id"],
                name="fk_stored_files_submission_revision",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["knowledge_document_id"], ["knowledge_documents.id"],
                name="fk_stored_files_knowledge_document",
                ondelete="SET NULL",
            ),
        ]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index("ix_stored_files_owner_id", "stored_files", ["owner_id"])
    op.create_index("ix_stored_files_assignment_id", "stored_files", ["assignment_id"])
    op.create_index("ix_stored_files_submission_revision_id", "stored_files", ["submission_revision_id"])
    op.create_index("ix_stored_files_knowledge_document_id", "stored_files", ["knowledge_document_id"])

    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("stored_file_id", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("original_name", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="processing"),
        sa.Column("parser_version", sa.String(length=64), nullable=False, server_default="v1"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        *([] if _defer_cyclic_foreign_keys() else [sa.ForeignKeyConstraint(
            ["stored_file_id"], ["stored_files.id"],
            name="fk_knowledge_documents_stored_file",
            ondelete="SET NULL",
        )]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "sha256", name="uq_knowledge_documents_owner_sha256"),
        sa.UniqueConstraint("stored_file_id"),
    )
    op.create_index("ix_knowledge_documents_owner_id", "knowledge_documents", ["owner_id"])
    op.create_index("ix_knowledge_documents_status", "knowledge_documents", ["status"])

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("chunk_metadata", sa.JSON(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunks_document_index"),
    )
    op.create_index("ix_knowledge_chunks_document_id", "knowledge_chunks", ["document_id"])

    op.create_table(
        "assignment_knowledge_documents",
        sa.Column("assignment_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("selected_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("assignment_id", "document_id"),
        sa.UniqueConstraint(
            "assignment_id", "document_id",
            name="uq_assignment_knowledge_documents_assignment_doc",
        ),
    )

    op.create_table(
        "course_enrollments",
        sa.Column("course_id", sa.String(length=64), nullable=False),
        sa.Column("student_id", sa.String(length=64), nullable=False),
        sa.Column("enrolled_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("course_id", "student_id"),
        sa.UniqueConstraint("course_id", "student_id", name="uq_course_enrollments_course_student"),
    )
    op.create_index("ix_course_enrollments_student_id", "course_enrollments", ["student_id"])

    op.create_table(
        "submissions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("assignment_id", sa.String(length=64), nullable=False),
        sa.Column("student_id", sa.String(length=64), nullable=False),
        sa.Column("current_revision_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        *([] if _defer_cyclic_foreign_keys() else [sa.ForeignKeyConstraint(
            ["current_revision_id"], ["submission_revisions.id"],
            name="fk_submissions_current_revision",
            ondelete="SET NULL",
        )]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("assignment_id", "student_id", name="uq_submissions_assignment_student"),
    )
    op.create_index("ix_submissions_assignment_id", "submissions", ["assignment_id"])
    op.create_index("ix_submissions_student_id", "submissions", ["student_id"])
    op.create_index("ix_submissions_current_revision_id", "submissions", ["current_revision_id"])

    op.create_table(
        "submission_revisions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("submission_id", sa.String(length=64), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="online"),
        sa.Column("file_name", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("created_at", sa.Float(), nullable=False),
        *([] if _defer_cyclic_foreign_keys() else [sa.ForeignKeyConstraint(
            ["submission_id"], ["submissions.id"],
            name="fk_submission_revisions_submission",
            ondelete="CASCADE",
        )]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "submission_id", "revision_number",
            name="uq_submission_revisions_submission_number",
        ),
        sa.CheckConstraint(
            "source IN ('online', 'teacher_import')", name="ck_submission_revisions_source"
        ),
    )
    op.create_index("ix_submission_revisions_submission_id", "submission_revisions", ["submission_id"])

    op.create_table(
        "submission_answers",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("revision_id", sa.String(length=64), nullable=False),
        sa.Column("question_id", sa.String(length=64), nullable=False),
        sa.Column("q_id", sa.String(length=64), nullable=False),
        sa.Column("number", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("type", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("flag", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["revision_id"], ["submission_revisions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["question_id"], ["assignment_questions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "revision_id", "question_id", name="uq_submission_answers_revision_question"
        ),
    )
    op.create_index("ix_submission_answers_revision_id", "submission_answers", ["revision_id"])
    op.create_index("ix_submission_answers_question_id", "submission_answers", ["question_id"])

    op.create_table(
        "grading_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("assignment_id", sa.String(length=64), nullable=False),
        sa.Column("teacher_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expiry", sa.Float(), nullable=True),
        sa.Column("last_heartbeat_at", sa.Float(), nullable=True),
        sa.Column("total_submissions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_submissions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_submissions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.Column("released_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["teacher_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'partial_failed', "
            "'failed', 'cancelled')",
            name="ck_grading_runs_status",
        ),
    )
    op.create_index("ix_grading_runs_assignment_id", "grading_runs", ["assignment_id"])
    op.create_index("ix_grading_runs_teacher_id", "grading_runs", ["teacher_id"])
    op.create_index("ix_grading_runs_status", "grading_runs", ["status"])
    op.create_index("ix_grading_runs_lease_owner", "grading_runs", ["lease_owner"])
    op.create_index("ix_grading_runs_lease_expiry", "grading_runs", ["lease_expiry"])
    op.create_index("ix_grading_runs_completed_at", "grading_runs", ["completed_at"])
    op.create_index("ix_grading_runs_released_at", "grading_runs", ["released_at"])
    # Partial unique index: at most one active (queued|running) run per
    # assignment. Both SQLite and PostgreSQL accept a partial-WHERE unique
    # index; this is the DB-enforced "single active run" invariant.
    op.create_index(
        "ux_grading_runs_active_per_assignment",
        "grading_runs",
        ["assignment_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running')"),
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "grading_run_submissions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("grading_run_id", sa.String(length=64), nullable=False),
        sa.Column("submission_revision_id", sa.String(length=64), nullable=False),
        sa.Column("student_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["grading_run_id"], ["grading_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["submission_revision_id"], ["submission_revisions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "grading_run_id", "submission_revision_id",
            name="uq_grading_run_submissions_run_revision",
        ),
    )
    op.create_index(
        "ix_grading_run_submissions_grading_run_id",
        "grading_run_submissions", ["grading_run_id"],
    )
    op.create_index(
        "ix_grading_run_submissions_submission_revision_id",
        "grading_run_submissions", ["submission_revision_id"],
    )
    op.create_index(
        "ix_grading_run_submissions_student_id",
        "grading_run_submissions", ["student_id"],
    )

    op.create_table(
        "grading_run_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("grading_run_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("level", sa.String(length=16), nullable=False, server_default="info"),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["grading_run_id"], ["grading_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_grading_run_events_grading_run_id",
        "grading_run_events", ["grading_run_id"],
    )

    op.create_table(
        "grade_results",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("grading_run_id", sa.String(length=64), nullable=False),
        sa.Column("submission_revision_id", sa.String(length=64), nullable=False),
        sa.Column("question_id", sa.String(length=64), nullable=False),
        sa.Column("student_id", sa.String(length=64), nullable=False),
        sa.Column("q_id", sa.String(length=64), nullable=False),
        sa.Column("ai_score", sa.Float(), nullable=True),
        sa.Column("ai_max_score", sa.Float(), nullable=False, server_default="10.0"),
        sa.Column("ai_comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("ai_steps", sa.JSON(), nullable=False),
        sa.Column("ai_confidence", sa.Float(), nullable=True),
        sa.Column("ai_expert_results", sa.JSON(), nullable=False),
        sa.Column("ai_synthesis_method", sa.String(length=64), nullable=True),
        sa.Column("requires_review", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("review_reason", sa.String(length=128), nullable=True),
        sa.Column("result_status", sa.String(length=32), nullable=False, server_default="graded"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["grading_run_id"], ["grading_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["submission_revision_id"], ["submission_revisions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["question_id"], ["assignment_questions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "grading_run_id", "submission_revision_id", "question_id",
            name="uq_grade_results_run_revision_question",
        ),
        sa.CheckConstraint(
            "result_status IN ('graded', 'failed', 'needs_review')",
            name="ck_grade_results_status",
        ),
    )
    op.create_index("ix_grade_results_grading_run_id", "grade_results", ["grading_run_id"])
    op.create_index(
        "ix_grade_results_submission_revision_id",
        "grade_results", ["submission_revision_id"],
    )
    op.create_index("ix_grade_results_question_id", "grade_results", ["question_id"])
    op.create_index("ix_grade_results_student_id", "grade_results", ["student_id"])

    op.create_table(
        "teacher_reviews",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("grade_result_id", sa.String(length=64), nullable=False),
        sa.Column("teacher_id", sa.String(length=64), nullable=False),
        sa.Column("previous_score", sa.Float(), nullable=True),
        sa.Column("previous_comment", sa.Text(), nullable=True),
        sa.Column("new_score", sa.Float(), nullable=False),
        sa.Column("new_comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["grade_result_id"], ["grade_results.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["teacher_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_teacher_reviews_grade_result_id", "teacher_reviews", ["grade_result_id"])
    op.create_index("ix_teacher_reviews_teacher_id", "teacher_reviews", ["teacher_id"])

    if _defer_cyclic_foreign_keys():
        op.create_foreign_key(
            "fk_stored_files_submission_revision",
            "stored_files",
            "submission_revisions",
            ["submission_revision_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.create_foreign_key(
            "fk_stored_files_knowledge_document",
            "stored_files",
            "knowledge_documents",
            ["knowledge_document_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            "fk_knowledge_documents_stored_file",
            "knowledge_documents",
            "stored_files",
            ["stored_file_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            "fk_submissions_current_revision",
            "submissions",
            "submission_revisions",
            ["current_revision_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            "fk_submission_revisions_submission",
            "submission_revisions",
            "submissions",
            ["submission_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    if _defer_cyclic_foreign_keys():
        op.drop_constraint(
            "fk_stored_files_submission_revision", "stored_files", type_="foreignkey"
        )
        op.drop_constraint(
            "fk_stored_files_knowledge_document", "stored_files", type_="foreignkey"
        )
        op.drop_constraint(
            "fk_knowledge_documents_stored_file", "knowledge_documents", type_="foreignkey"
        )
        op.drop_constraint(
            "fk_submissions_current_revision", "submissions", type_="foreignkey"
        )
        op.drop_constraint(
            "fk_submission_revisions_submission", "submission_revisions", type_="foreignkey"
        )

    op.drop_index("ix_teacher_reviews_teacher_id", table_name="teacher_reviews")
    op.drop_index("ix_teacher_reviews_grade_result_id", table_name="teacher_reviews")
    op.drop_table("teacher_reviews")

    op.drop_index("ix_grade_results_student_id", table_name="grade_results")
    op.drop_index("ix_grade_results_question_id", table_name="grade_results")
    op.drop_index("ix_grade_results_submission_revision_id", table_name="grade_results")
    op.drop_index("ix_grade_results_grading_run_id", table_name="grade_results")
    op.drop_table("grade_results")

    op.drop_index("ix_grading_run_events_grading_run_id", table_name="grading_run_events")
    op.drop_table("grading_run_events")

    op.drop_index(
        "ix_grading_run_submissions_student_id", table_name="grading_run_submissions"
    )
    op.drop_index(
        "ix_grading_run_submissions_submission_revision_id",
        table_name="grading_run_submissions",
    )
    op.drop_index(
        "ix_grading_run_submissions_grading_run_id", table_name="grading_run_submissions"
    )
    op.drop_table("grading_run_submissions")

    op.drop_index("ux_grading_runs_active_per_assignment", table_name="grading_runs")
    op.drop_index("ix_grading_runs_completed_at", table_name="grading_runs")
    op.drop_index("ix_grading_runs_lease_expiry", table_name="grading_runs")
    op.drop_index("ix_grading_runs_lease_owner", table_name="grading_runs")
    op.drop_index("ix_grading_runs_status", table_name="grading_runs")
    op.drop_index("ix_grading_runs_teacher_id", table_name="grading_runs")
    op.drop_index("ix_grading_runs_assignment_id", table_name="grading_runs")
    op.drop_table("grading_runs")

    op.drop_index("ix_submission_answers_question_id", table_name="submission_answers")
    op.drop_index("ix_submission_answers_revision_id", table_name="submission_answers")
    op.drop_table("submission_answers")

    op.drop_index("ix_submission_revisions_submission_id", table_name="submission_revisions")
    op.drop_table("submission_revisions")

    op.drop_index("ix_submissions_current_revision_id", table_name="submissions")
    op.drop_index("ix_submissions_student_id", table_name="submissions")
    op.drop_index("ix_submissions_assignment_id", table_name="submissions")
    op.drop_table("submissions")

    op.drop_index("ix_course_enrollments_student_id", table_name="course_enrollments")
    op.drop_table("course_enrollments")

    op.drop_table("assignment_knowledge_documents")

    op.drop_index("ix_knowledge_chunks_document_id", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")

    op.drop_index("ix_knowledge_documents_status", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_owner_id", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")

    op.drop_index("ix_stored_files_knowledge_document_id", table_name="stored_files")
    op.drop_index("ix_stored_files_submission_revision_id", table_name="stored_files")
    op.drop_index("ix_stored_files_assignment_id", table_name="stored_files")
    op.drop_index("ix_stored_files_owner_id", table_name="stored_files")
    op.drop_table("stored_files")

    op.drop_index("ix_assignment_questions_assignment_id", table_name="assignment_questions")
    op.drop_table("assignment_questions")

    op.drop_index("ix_assignments_status", table_name="assignments")
    op.drop_index("ix_assignments_teacher_id", table_name="assignments")
    op.drop_index("ix_assignments_course_id", table_name="assignments")
    op.drop_table("assignments")

    op.drop_index("ix_provider_configs_owner_id", table_name="provider_configs")
    op.drop_table("provider_configs")

    op.drop_index(op.f("ix_refresh_sessions_user_id"), table_name="refresh_sessions")
    op.drop_index(op.f("ix_refresh_sessions_token_hash"), table_name="refresh_sessions")
    op.drop_index(op.f("ix_refresh_sessions_expires_at"), table_name="refresh_sessions")
    op.drop_table("refresh_sessions")

    op.drop_index(op.f("ix_invite_codes_course_id"), table_name="invite_codes")
    op.drop_index(op.f("ix_invite_codes_invited_by"), table_name="invite_codes")
    op.drop_index(op.f("ix_invite_codes_expires_at"), table_name="invite_codes")
    op.drop_table("invite_codes")

    op.drop_index("ix_courses_teacher_id", table_name="courses")
    op.drop_table("courses")

    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_index(op.f("ix_users_username"), table_name="users")
    op.drop_table("users")
