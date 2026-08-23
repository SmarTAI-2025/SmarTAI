"""Add owner-scoped specialized OCR BYOK credentials.

Revision ID: 0009_ocr_provider_credentials
Revises: 0008_operation_leases
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_ocr_provider_credentials"
down_revision = "0008_operation_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ocr_provider_credentials",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("provider_type", sa.String(length=64), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("api_key_nonce", sa.String(length=128), nullable=False),
        sa.Column("api_key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("encrypted_secret_key", sa.Text(), nullable=False),
        sa.Column("secret_key_nonce", sa.String(length=128), nullable=False),
        sa.Column("secret_key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "verification_status",
            sa.String(length=32),
            nullable=False,
            server_default="unverified",
        ),
        sa.Column("last_checked_at", sa.Float(), nullable=True),
        sa.Column("verification_error_code", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "provider_type = 'baidu_unlimited_ocr'",
            name="ck_ocr_provider_credentials_provider_type",
        ),
        sa.CheckConstraint(
            "verification_status IN "
            "('unverified', 'credentials_verified', 'failed')",
            name="ck_ocr_provider_credentials_verification_status",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id",
            "provider_type",
            name="uq_ocr_provider_credentials_owner_provider",
        ),
    )
    op.create_index(
        "ix_ocr_provider_credentials_owner_id",
        "ocr_provider_credentials",
        ["owner_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ocr_provider_credentials_owner_id",
        table_name="ocr_provider_credentials",
    )
    op.drop_table("ocr_provider_credentials")
