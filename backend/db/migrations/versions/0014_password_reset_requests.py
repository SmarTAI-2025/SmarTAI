"""Add password reset requests and immediate auth invalidation marker.

Revision ID: 0014_password_reset_requests
Revises: 0013_email_verification_requests
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0014_password_reset_requests"
down_revision = "0013_email_verification_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("auth_invalid_before", sa.Float(), nullable=True))
    op.create_table(
        "password_reset_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("resend_available_at", sa.Float(), nullable=False),
        sa.Column("superseded_at", sa.Float(), nullable=True),
        sa.Column("consumed_at", sa.Float(), nullable=True),
        sa.Column("delivery_status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("last_delivery_error_code", sa.String(length=128), nullable=True),
        sa.Column("source_ip", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "delivery_status IN ('pending', 'sent', 'failed')",
            name="ck_password_reset_delivery_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_index(
        "ix_password_reset_requests_user_id",
        "password_reset_requests",
        ["user_id"],
    )
    op.create_index(
        "ix_password_reset_requests_created_at",
        "password_reset_requests",
        ["created_at"],
    )
    op.create_index(
        "ix_password_reset_requests_expires_at",
        "password_reset_requests",
        ["expires_at"],
    )
    op.create_index(
        "ix_password_reset_requests_token_digest",
        "password_reset_requests",
        ["token_digest"],
        unique=True,
    )
    op.create_table(
        "password_reset_rate_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("email_digest", sa.String(length=64), nullable=False),
        sa.Column("source_ip_digest", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_password_reset_rate_events_email_created",
        "password_reset_rate_events",
        ["email_digest", "created_at"],
    )
    op.create_index(
        "ix_password_reset_rate_events_ip_created",
        "password_reset_rate_events",
        ["source_ip_digest", "created_at"],
    )
    op.create_index(
        "ix_password_reset_rate_events_created_at",
        "password_reset_rate_events",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_password_reset_rate_events_created_at",
        table_name="password_reset_rate_events",
    )
    op.drop_index(
        "ix_password_reset_rate_events_ip_created",
        table_name="password_reset_rate_events",
    )
    op.drop_index(
        "ix_password_reset_rate_events_email_created",
        table_name="password_reset_rate_events",
    )
    op.drop_table("password_reset_rate_events")
    op.drop_index(
        "ix_password_reset_requests_token_digest",
        table_name="password_reset_requests",
    )
    op.drop_index(
        "ix_password_reset_requests_expires_at",
        table_name="password_reset_requests",
    )
    op.drop_index(
        "ix_password_reset_requests_created_at",
        table_name="password_reset_requests",
    )
    op.drop_index(
        "ix_password_reset_requests_user_id",
        table_name="password_reset_requests",
    )
    op.drop_table("password_reset_requests")
    op.drop_column("users", "auth_invalid_before")
