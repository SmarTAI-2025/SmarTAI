"""durable assignment workflow presentation metadata

Revision ID: 0003_assignment_workflow_facade
Revises: 0002_course_library_tags
Create Date: 2026-07-30

The normalized assignment/question/submission/grading tables remain the source
of truth.  These tables contain only presentation workflow state, bounded
operation staging, idempotency keys, review/display metadata, immutable grading
setup snapshots, and deterministic artifact manifests required by the Figma
teacher workflow.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "0003_assignment_workflow_facade"
down_revision: Union[str, None] = "0002_course_library_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("assignment_knowledge_documents") as batch_op:
        batch_op.add_column(
            sa.Column(
                "source_kind", sa.String(length=32), nullable=False,
                server_default="upload",
            )
        )
        batch_op.add_column(
            sa.Column("library_material_id", sa.String(length=64), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_assignment_knowledge_documents_library_material_id",
            "course_materials",
            ["library_material_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.add_column(
        "provider_configs",
        sa.Column(
            "verification_status", sa.String(length=32), nullable=False,
            server_default="unverified",
        ),
    )
    op.add_column(
        "provider_configs", sa.Column("last_checked_at", sa.Float(), nullable=True)
    )
    op.add_column(
        "provider_configs",
        sa.Column("verification_error_code", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "grade_results",
        sa.Column(
            "initial_requires_review", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "grade_results",
        sa.Column("initial_review_reason", sa.String(length=128), nullable=True),
    )
    # Preserve the original review-queue facts before application code can
    # resolve the mutable requires_review/review_reason presentation fields.
    op.execute(sa.text(
        "UPDATE grade_results "
        "SET initial_requires_review = requires_review, "
        "initial_review_reason = review_reason"
    ))
    op.add_column(
        "teacher_reviews",
        sa.Column(
            # Rows created before this migration came only from the normalized
            # confirmation endpoint, so they are already committed teacher
            # decisions.  New ORM-created draft rows explicitly write False.
            "confirmed", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.add_column(
        "teacher_reviews",
        sa.Column(
            "review_sequence", sa.Integer(), nullable=False,
            server_default=sa.text("1"),
        ),
    )
    # Legacy rows had only wall-clock ordering. Assign each result a stable
    # 1-based order, breaking equal timestamps by the immutable review id.
    op.execute(sa.text("""
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY grade_result_id
                       ORDER BY created_at, id
                   ) AS review_sequence
            FROM teacher_reviews
        )
        UPDATE teacher_reviews
        SET review_sequence = (
            SELECT ranked.review_sequence
            FROM ranked
            WHERE ranked.id = teacher_reviews.id
        )
    """))
    with op.batch_alter_table("teacher_reviews") as batch_op:
        batch_op.alter_column(
            "confirmed",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=sa.false(),
        )
        batch_op.create_unique_constraint(
            "uq_teacher_reviews_result_sequence",
            ["grade_result_id", "review_sequence"],
        )

    op.create_table(
        "assignment_workflows",
        sa.Column("assignment_id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("semester_id", sa.String(length=64), nullable=True),
        sa.Column("presentation_status", sa.String(length=32), nullable=False),
        sa.Column("workflow_revision", sa.Integer(), nullable=False),
        sa.Column("active_operation", sa.String(length=64), nullable=True),
        sa.Column("active_job_id", sa.String(length=64), nullable=True),
        sa.Column("extract_job_id", sa.String(length=64), nullable=True),
        sa.Column("parse_job_id", sa.String(length=64), nullable=True),
        sa.Column("grading_job_id", sa.String(length=64), nullable=True),
        sa.Column("last_failed_job_id", sa.String(length=64), nullable=True),
        sa.Column("problem_file_name", sa.String(length=512), nullable=True),
        sa.Column("submission_file_name", sa.String(length=512), nullable=True),
        sa.Column("pending_submission_file_name", sa.String(length=512), nullable=True),
        sa.Column("submission_identity_mode", sa.String(length=32), nullable=False),
        sa.Column("submission_roster_name", sa.String(length=512), nullable=True),
        sa.Column("submission_recognition_provider_id", sa.String(length=240), nullable=True),
        sa.Column("reference_file_name", sa.String(length=512), nullable=True),
        sa.Column("test_cases_file_name", sa.String(length=512), nullable=True),
        sa.Column("grading_setup", sa.JSON(), nullable=True),
        sa.Column("grading_setup_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("grading_setup_updated_at", sa.Float(), nullable=True),
        sa.Column("final_result_version", sa.Integer(), nullable=False),
        sa.Column("final_result_updated_at", sa.Float(), nullable=True),
        sa.Column("analysis_status", sa.String(length=32), nullable=False),
        sa.Column("analysis_result_version", sa.Integer(), nullable=True),
        sa.Column("analysis_generated_at", sa.Float(), nullable=True),
        sa.Column("analysis_error_code", sa.String(length=128), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("assignment_id"),
    )
    for column in (
        "owner_id", "semester_id", "presentation_status", "active_job_id"
    ):
        op.create_index(
            f"ix_assignment_workflows_{column}", "assignment_workflows", [column]
        )

    op.create_table(
        "task_create_idempotency",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("assignment_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "idempotency_key",
            name="uq_task_create_idempotency_owner_key",
        ),
    )
    op.create_index(
        "ix_task_create_idempotency_owner_id", "task_create_idempotency", ["owner_id"]
    )
    op.create_index(
        "ix_task_create_idempotency_assignment_id", "task_create_idempotency", ["assignment_id"]
    )

    op.create_table(
        "workflow_operations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("assignment_id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "attempt", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.Column("expires_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "assignment_id", "operation_type", "input_hash",
            name="uq_workflow_operations_assignment_type_hash",
        ),
    )
    op.create_index(
        "ix_workflow_operations_assignment_id", "workflow_operations", ["assignment_id"]
    )
    op.create_index(
        "ix_workflow_operations_owner_id", "workflow_operations", ["owner_id"]
    )
    op.create_index(
        "ix_workflow_operations_expires_at", "workflow_operations", ["expires_at"]
    )
    op.create_index(
        "ix_workflow_operations_assignment_status",
        "workflow_operations", ["assignment_id", "status"],
    )

    op.create_table(
        "assignment_student_presentations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("assignment_id", sa.String(length=64), nullable=False),
        sa.Column("student_id", sa.String(length=64), nullable=False),
        sa.Column("display_student_id", sa.String(length=160), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("source_filename", sa.String(length=512), nullable=True),
        sa.Column("identity_match_method", sa.String(length=32), nullable=True),
        sa.Column("identity_status", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "assignment_id", "student_id",
            name="uq_assignment_student_presentations_assignment_student",
        ),
    )
    op.create_index(
        "ix_assignment_student_presentations_assignment_id",
        "assignment_student_presentations", ["assignment_id"],
    )
    op.create_index(
        "ix_assignment_student_presentations_student_id",
        "assignment_student_presentations", ["student_id"],
    )

    op.create_table(
        "submission_answer_presentations",
        sa.Column("answer_id", sa.String(length=64), nullable=False),
        sa.Column("review_status", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["answer_id"], ["submission_answers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("answer_id"),
    )

    op.create_table(
        "grading_run_setups",
        sa.Column("grading_run_id", sa.String(length=64), nullable=False),
        sa.Column("assignment_id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("setup", sa.JSON(), nullable=False),
        sa.Column("input_manifest", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["grading_run_id"], ["grading_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("grading_run_id"),
    )
    op.create_index(
        "ix_grading_run_setups_assignment_id", "grading_run_setups", ["assignment_id"]
    )
    op.create_index(
        "ix_grading_run_setups_owner_id", "grading_run_setups", ["owner_id"]
    )

    op.create_table(
        "result_artifact_manifests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("assignment_id", sa.String(length=64), nullable=False),
        sa.Column("grading_run_id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("result_version", sa.Integer(), nullable=False),
        sa.Column("result_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("generated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["grading_run_id"], ["grading_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "assignment_id", "result_version",
            name="uq_result_artifact_manifests_assignment_version",
        ),
    )
    op.create_index(
        "ix_result_artifact_manifests_assignment_id",
        "result_artifact_manifests", ["assignment_id"],
    )
    op.create_index(
        "ix_result_artifact_manifests_grading_run_id",
        "result_artifact_manifests", ["grading_run_id"],
    )
    op.create_index(
        "ix_result_artifact_manifests_owner_id",
        "result_artifact_manifests", ["owner_id"],
    )


def downgrade() -> None:
    for column in ("owner_id", "grading_run_id", "assignment_id"):
        op.drop_index(
            f"ix_result_artifact_manifests_{column}",
            table_name="result_artifact_manifests",
        )
    op.drop_table("result_artifact_manifests")

    op.drop_index("ix_grading_run_setups_owner_id", table_name="grading_run_setups")
    op.drop_index("ix_grading_run_setups_assignment_id", table_name="grading_run_setups")
    op.drop_table("grading_run_setups")

    op.drop_table("submission_answer_presentations")

    op.drop_index(
        "ix_assignment_student_presentations_student_id",
        table_name="assignment_student_presentations",
    )
    op.drop_index(
        "ix_assignment_student_presentations_assignment_id",
        table_name="assignment_student_presentations",
    )
    op.drop_table("assignment_student_presentations")

    op.drop_index(
        "ix_workflow_operations_assignment_status", table_name="workflow_operations"
    )
    op.drop_index("ix_workflow_operations_expires_at", table_name="workflow_operations")
    op.drop_index("ix_workflow_operations_owner_id", table_name="workflow_operations")
    op.drop_index("ix_workflow_operations_assignment_id", table_name="workflow_operations")
    op.drop_table("workflow_operations")

    op.drop_index(
        "ix_task_create_idempotency_assignment_id", table_name="task_create_idempotency"
    )
    op.drop_index(
        "ix_task_create_idempotency_owner_id", table_name="task_create_idempotency"
    )
    op.drop_table("task_create_idempotency")

    for column in ("active_job_id", "presentation_status", "semester_id", "owner_id"):
        op.drop_index(
            f"ix_assignment_workflows_{column}", table_name="assignment_workflows"
        )
    op.drop_table("assignment_workflows")

    with op.batch_alter_table("assignment_knowledge_documents") as batch_op:
        batch_op.drop_constraint(
            "fk_assignment_knowledge_documents_library_material_id",
            type_="foreignkey",
        )
        batch_op.drop_column("library_material_id")
        batch_op.drop_column("source_kind")

    with op.batch_alter_table("teacher_reviews") as batch_op:
        batch_op.drop_constraint(
            "uq_teacher_reviews_result_sequence", type_="unique"
        )
        batch_op.drop_column("review_sequence")
        batch_op.drop_column("confirmed")
    op.drop_column("grade_results", "initial_review_reason")
    op.drop_column("grade_results", "initial_requires_review")
    op.drop_column("provider_configs", "verification_error_code")
    op.drop_column("provider_configs", "last_checked_at")
    op.drop_column("provider_configs", "verification_status")
