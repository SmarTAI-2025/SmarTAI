"""Add bounded checkpoint state to workflow operations.

Revision ID: 0006_operation_checkpoints
Revises: 0005_workflow_source_outcomes
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006_operation_checkpoints"
down_revision = "0005_workflow_source_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workflow_operations") as batch_op:
        batch_op.add_column(sa.Column(
            "checkpoint_revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ))
        batch_op.add_column(sa.Column(
            "checkpoint_stage", sa.String(length=64), nullable=True
        ))
        batch_op.add_column(sa.Column(
            "checkpoint",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ))
        batch_op.add_column(sa.Column(
            "artifact_refs",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ))
        batch_op.add_column(sa.Column(
            "terminal_summary", sa.JSON(), nullable=True
        ))
        batch_op.create_check_constraint(
            "ck_workflow_operations_checkpoint_revision_nonnegative",
            "checkpoint_revision >= 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("workflow_operations") as batch_op:
        batch_op.drop_constraint(
            "ck_workflow_operations_checkpoint_revision_nonnegative",
            type_="check",
        )
        batch_op.drop_column("terminal_summary")
        batch_op.drop_column("artifact_refs")
        batch_op.drop_column("checkpoint")
        batch_op.drop_column("checkpoint_stage")
        batch_op.drop_column("checkpoint_revision")
