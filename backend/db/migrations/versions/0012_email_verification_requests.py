"""Add one-time email verification registration requests.

Revision ID: 0012_email_verification_requests
Revises: 0011_ocr_provider_credentials
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0012_email_verification_requests"
down_revision = "0011_ocr_provider_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
        "ix_email_verification_requests_created_at",
        table_name="email_verification_requests",
    )
    op.drop_index(
        "ix_email_verification_requests_email",
        table_name="email_verification_requests",
    )
    op.drop_table("email_verification_requests")
