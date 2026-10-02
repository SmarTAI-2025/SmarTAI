"""Add durable product usage event ledger for private metrics."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0019_admin_usage_events"
down_revision = "0018_auth_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_usage_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("event_name", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=True),
        sa.Column("occurred_at", sa.Float(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("provider", sa.String(length=128), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("dimensions", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_usage_events_event_name", "admin_usage_events", ["event_name"])
    op.create_index("ix_admin_usage_events_user_id", "admin_usage_events", ["user_id"])
    op.create_index("ix_admin_usage_events_occurred_at", "admin_usage_events", ["occurred_at"])
    op.create_index("ix_admin_usage_events_name_occurred", "admin_usage_events", ["event_name", "occurred_at"])
    op.create_index("ix_admin_usage_events_user_occurred", "admin_usage_events", ["user_id", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_admin_usage_events_user_occurred", table_name="admin_usage_events")
    op.drop_index("ix_admin_usage_events_name_occurred", table_name="admin_usage_events")
    op.drop_index("ix_admin_usage_events_occurred_at", table_name="admin_usage_events")
    op.drop_index("ix_admin_usage_events_user_id", table_name="admin_usage_events")
    op.drop_index("ix_admin_usage_events_event_name", table_name="admin_usage_events")
    op.drop_table("admin_usage_events")
