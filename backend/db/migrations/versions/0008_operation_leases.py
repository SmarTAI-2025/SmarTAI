"""Add operation lease fencing columns.

Revision ID: 0008_operation_leases
Revises: 0007_source_outcome_diagnostics
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0008_operation_leases"
down_revision = "0007_source_outcome_diagnostics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workflow_operations") as batch_op:
        batch_op.add_column(sa.Column("lease_owner", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("lease_token", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("lease_expires_at", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("lease_heartbeat_at", sa.Float(), nullable=True))
        batch_op.create_check_constraint(
            "ck_workflow_operations_lease_consistency",
            "(lease_owner IS NULL AND lease_token IS NULL"
            " AND lease_expires_at IS NULL AND lease_heartbeat_at IS NULL)"
            " OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL"
            " AND lease_expires_at IS NOT NULL AND lease_heartbeat_at IS NOT NULL)",
        )
        batch_op.create_index(
            "ix_workflow_operations_claimable",
            ["status", "lease_expires_at"],
        )


def downgrade() -> None:
    with op.batch_alter_table("workflow_operations") as batch_op:
        batch_op.drop_index("ix_workflow_operations_claimable")
        batch_op.drop_constraint(
            "ck_workflow_operations_lease_consistency",
            type_="check",
        )
        batch_op.drop_column("lease_heartbeat_at")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("lease_token")
        batch_op.drop_column("lease_owner")
