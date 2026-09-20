"""Add one-time email verification registration requests.

Revision ID: 0013_email_verification_requests
Revises: 0012_provider_routing_pref
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0013_email_verification_requests"
down_revision = "0012_provider_routing_pref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # All auth lookup/write paths use the same canonical email form. Normalize
    # pre-mail local rows before public registration starts relying on the
    # existing unique constraint as its final concurrency guard.
    for table_name in ("users", "invite_codes"):
        op.execute(
            f"UPDATE {table_name} SET email = CASE "
            "WHEN lower(trim(email)) LIKE '%.' "
            "THEN substr(lower(trim(email)), 1, length(lower(trim(email))) - 1) "
            "ELSE lower(trim(email)) END WHERE email IS NOT NULL"
        )
    op.create_table(
        "email_verification_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("normalized_username", sa.String(length=128), nullable=False),
        sa.Column("normalized_email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("resend_available_at", sa.Float(), nullable=False),
        sa.Column("superseded_at", sa.Float(), nullable=True),
        sa.Column("verified_at", sa.Float(), nullable=True),
        sa.Column("delivery_status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("last_delivery_error_code", sa.String(length=128), nullable=True),
        sa.Column("source_ip", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "delivery_status IN ('pending', 'sent', 'failed')",
            name="ck_email_verification_delivery_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_index(
        "ix_email_verification_requests_email",
        "email_verification_requests",
        ["normalized_email"],
    )
    op.create_index(
        "ix_email_verification_requests_created_at",
        "email_verification_requests",
        ["created_at"],
    )
    op.create_index(
        "ix_email_verification_requests_expires_at",
        "email_verification_requests",
        ["expires_at"],
    )
    op.create_index(
        "ix_email_verification_requests_token_digest",
        "email_verification_requests",
        ["token_digest"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_verification_requests_token_digest",
        table_name="email_verification_requests",
    )
    op.drop_index(
        "ix_email_verification_requests_expires_at",
        table_name="email_verification_requests",
    )
    op.drop_index(
        "ix_email_verification_requests_created_at",
        table_name="email_verification_requests",
    )
    op.drop_index(
        "ix_email_verification_requests_email",
        table_name="email_verification_requests",
    )
    op.drop_table("email_verification_requests")
