"""Integrate source outcomes with teacher-facing submission review.

Revision ID: 0007_source_outcome_diagnostics
Revises: 0006_operation_checkpoints
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0007_source_outcome_diagnostics"
down_revision = "0006_operation_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_context().as_sql:
        _upgrade_canonical()
        return

    inspector = sa.inspect(op.get_bind())
    outcome_columns = {
        column["name"]
        for column in inspector.get_columns("workflow_source_outcomes")
    }
    if "failure_phase" not in outcome_columns:
        with op.batch_alter_table("workflow_source_outcomes") as batch_op:
            batch_op.add_column(
                sa.Column("failure_phase", sa.String(length=64), nullable=True)
            )

    presentation_columns = {
        column["name"]
        for column in inspector.get_columns("assignment_student_presentations")
    }
    presentation_fks = inspector.get_foreign_keys(
        "assignment_student_presentations"
    )
    presentation_indexes = inspector.get_indexes(
        "assignment_student_presentations"
    )
    has_source_fk = any(
        fk.get("constrained_columns") == ["source_id"]
        and fk.get("referred_table") == "workflow_source_items"
        for fk in presentation_fks
    )
    has_unique_source_index = any(
        index.get("column_names") == ["source_id"] and index.get("unique")
        for index in presentation_indexes
    )
    needs_source_column = "source_id" not in presentation_columns
    if needs_source_column or not has_source_fk or not has_unique_source_index:
        with op.batch_alter_table("assignment_student_presentations") as batch_op:
            if needs_source_column:
                batch_op.add_column(
                    sa.Column("source_id", sa.String(length=64), nullable=True)
                )
            if not has_source_fk:
                batch_op.create_foreign_key(
                    "fk_assignment_student_presentations_source",
                    "workflow_source_items",
                    ["source_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
            if not has_unique_source_index:
                batch_op.create_index(
                    "ix_assignment_student_presentations_source_id",
                    ["source_id"],
                    unique=True,
                )


def _upgrade_canonical() -> None:
    with op.batch_alter_table("workflow_source_outcomes") as batch_op:
        batch_op.add_column(sa.Column("failure_phase", sa.String(length=64), nullable=True))

    with op.batch_alter_table("assignment_student_presentations") as batch_op:
        batch_op.add_column(sa.Column("source_id", sa.String(length=64), nullable=True))
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


def downgrade() -> None:
    with op.batch_alter_table("assignment_student_presentations") as batch_op:
        batch_op.drop_index("ix_assignment_student_presentations_source_id")
        batch_op.drop_constraint(
            "fk_assignment_student_presentations_source",
            type_="foreignkey",
        )
        batch_op.drop_column("source_id")

    with op.batch_alter_table("workflow_source_outcomes") as batch_op:
        batch_op.drop_column("failure_phase")
