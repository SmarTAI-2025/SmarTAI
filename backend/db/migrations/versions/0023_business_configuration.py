"""Versioned business configuration and per-owner storage overrides."""
from alembic import op
import sqlalchemy as sa

revision = "0023_business_configuration"
down_revision = "0022_account_closures"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("business_configuration",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("overrides", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("registration_rules_managed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("id = 'global'", name="ck_business_configuration_singleton"),
        sa.CheckConstraint("version >= 1", name="ck_business_configuration_version"))
    op.create_table("user_storage_configuration",
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("overrides", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_user_storage_configuration_version"))


def downgrade():
    op.drop_table("user_storage_configuration")
    op.drop_table("business_configuration")
