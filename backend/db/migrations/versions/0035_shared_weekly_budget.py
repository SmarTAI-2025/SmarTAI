"""Add actual-use weekly shared accounting; retain all legacy daily counters."""
from alembic import op
import sqlalchemy as sa

revision = "0035_shared_weekly_budget"
down_revision = "0034_shared_sol_quota"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("shared_model_charges",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("week", sa.String(10), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("reserved_nano_usd", sa.BigInteger(), nullable=False),
        sa.Column("cost_nano_usd", sa.BigInteger()),
        sa.Column("usage", sa.JSON()),
        sa.Column("response_id", sa.String(128)),
        sa.Column("price_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("settled_at", sa.Float()),
        sa.CheckConstraint("state IN ('reserved', 'settled', 'unknown', 'released')", name="ck_shared_charge_state"),
        sa.CheckConstraint("reserved_nano_usd >= 0 AND (cost_nano_usd IS NULL OR cost_nano_usd >= 0)", name="ck_shared_charge_nonnegative"))
    op.create_index("ix_shared_charges_owner_week", "shared_model_charges", ["owner_id", "week"])


def downgrade():
    op.drop_table("shared_model_charges")
