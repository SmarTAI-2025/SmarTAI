"""Add owner default-provider preference and freeze question recognition.

Revision ID: 0012_provider_routing_pref
Revises: 0011_ocr_provider_credentials
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0012_provider_routing_pref"
down_revision = "0011_ocr_provider_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_preferences",
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("default_provider_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["default_provider_id"],
            ["provider_configs.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("owner_id"),
    )
    op.create_index(
        "ix_provider_preferences_default_provider_id",
        "provider_preferences",
        ["default_provider_id"],
        unique=False,
    )
    with op.batch_alter_table("assignment_workflows") as batch_op:
        batch_op.add_column(
            sa.Column(
                "question_recognition_provider_id",
                sa.String(length=240),
                nullable=True,
            )
        )

    # Existing owners receive the first enabled record by stable creation
    # order. Future changes never silently replace this explicit preference.
    # Keep the backfill declarative so Alembic can also render the complete
    # PostgreSQL offline migration. The anti-join picks exactly one stable
    # oldest enabled configuration per owner on both SQLite and PostgreSQL.
    op.execute(sa.text(
        "INSERT INTO provider_preferences "
        "(owner_id, default_provider_id, created_at, updated_at) "
        "SELECT candidate.owner_id, candidate.id, "
        "candidate.created_at, candidate.created_at "
        "FROM provider_configs AS candidate "
        "WHERE candidate.enabled = true AND NOT EXISTS ("
        "SELECT 1 FROM provider_configs AS earlier "
        "WHERE earlier.owner_id = candidate.owner_id "
        "AND earlier.enabled = true AND ("
        "earlier.created_at < candidate.created_at OR ("
        "earlier.created_at = candidate.created_at "
        "AND earlier.id < candidate.id)))"
    ))


def downgrade() -> None:
    with op.batch_alter_table("assignment_workflows") as batch_op:
        batch_op.drop_column("question_recognition_provider_id")
    op.drop_index(
        "ix_provider_preferences_default_provider_id",
        table_name="provider_preferences",
    )
    op.drop_table("provider_preferences")
