"""Persistent per-owner, UTC-day model admission ledger."""
from alembic import op
import sqlalchemy as sa
revision = "0024_model_daily_usage"
down_revision = "0023_business_configuration"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("model_daily_usage",
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("scope", sa.String(16), primary_key=True),
        sa.Column("day", sa.String(10), primary_key=True),
        sa.Column("requests", sa.BigInteger(), nullable=False),
        sa.Column("estimated_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("last_admitted_at", sa.Float(), nullable=False),
        sa.CheckConstraint("scope IN ('shared', 'history')", name="ck_model_usage_scope"),
        sa.CheckConstraint("requests >= 0 AND estimated_input_tokens >= 0", name="ck_model_usage_nonnegative"))

def downgrade():
    op.drop_table("model_daily_usage")
