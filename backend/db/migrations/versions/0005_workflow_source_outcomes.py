"""durable workflow source items and per-file outcomes

Revision ID: 0005_workflow_source_outcomes
Revises: 0004_structured_review_reasons
"""
from alembic import op
import sqlalchemy as sa


revision = "0005_workflow_source_outcomes"
down_revision = "0004_structured_review_reasons"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_source_items",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("assignment_id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("stored_file_id", sa.String(length=64), nullable=False),
        sa.Column("retry_of_source_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "attempt > 0", name="ck_workflow_source_items_attempt_positive"
        ),
        sa.CheckConstraint(
            "order_index >= 0",
            name="ck_workflow_source_items_order_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"],
            name="fk_workflow_source_items_owner", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assignment_id"], ["assignments.id"],
            name="fk_workflow_source_items_assignment", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["workflow_operations.id"],
            name="fk_workflow_source_items_operation", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["stored_file_id"], ["stored_files.id"],
            name="fk_workflow_source_items_stored_file", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retry_of_source_id"], ["workflow_source_items.id"],
            name="fk_workflow_source_items_retry_source", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id", "attempt", "order_index",
            name="uq_workflow_source_items_operation_attempt_order",
        ),
        sa.UniqueConstraint(
            "operation_id", "attempt", "stored_file_id",
            name="uq_workflow_source_items_operation_attempt_file",
        ),
    )
    op.create_index(
        "ix_workflow_source_items_owner_id",
        "workflow_source_items",
        ["owner_id"],
    )
    op.create_index(
        "ix_workflow_source_items_stored_file_id",
        "workflow_source_items",
        ["stored_file_id"],
    )
    op.create_index(
        "ix_workflow_source_items_retry_of_source_id",
        "workflow_source_items",
        ["retry_of_source_id"],
    )
    op.create_index(
        "ix_workflow_source_items_assignment_operation_attempt_order",
        "workflow_source_items",
        ["assignment_id", "operation_id", "attempt", "order_index"],
    )

    op.create_table(
        "workflow_source_outcomes",
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("student_candidate", sa.String(length=255), nullable=True),
        sa.Column("matched_answer_count", sa.Integer(), nullable=False),
        sa.Column("unknown_question_ids", sa.JSON(), nullable=False),
        sa.Column("stable_error_code", sa.String(length=128), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("artifact_file_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "status IN ('parsed', 'parse_failed', 'identity_conflict', "
            "'no_matching_answer')",
            name="ck_workflow_source_outcomes_status",
        ),
        sa.CheckConstraint(
            "matched_answer_count >= 0",
            name="ck_workflow_source_outcomes_matched_count_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["workflow_source_items.id"],
            name="fk_workflow_source_outcomes_source", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_file_id"], ["stored_files.id"],
            name="fk_workflow_source_outcomes_artifact_file", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("source_id"),
    )
    op.create_index(
        "ix_workflow_source_outcomes_status",
        "workflow_source_outcomes",
        ["status"],
    )
    op.create_index(
        "ix_workflow_source_outcomes_artifact_file_id",
        "workflow_source_outcomes",
        ["artifact_file_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_source_outcomes_artifact_file_id",
        table_name="workflow_source_outcomes",
    )
    op.drop_index(
        "ix_workflow_source_outcomes_status",
        table_name="workflow_source_outcomes",
    )
    op.drop_table("workflow_source_outcomes")

    op.drop_index(
        "ix_workflow_source_items_assignment_operation_attempt_order",
        table_name="workflow_source_items",
    )
    op.drop_index(
        "ix_workflow_source_items_retry_of_source_id",
        table_name="workflow_source_items",
    )
    op.drop_index(
        "ix_workflow_source_items_stored_file_id",
        table_name="workflow_source_items",
    )
    op.drop_index(
        "ix_workflow_source_items_owner_id",
        table_name="workflow_source_items",
    )
    op.drop_table("workflow_source_items")
