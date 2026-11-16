"""Track Sol inside the existing atomic owner/day shared allowance."""
from alembic import op
import sqlalchemy as sa

revision = "0034_shared_sol_quota"
down_revision = "0033_shared_admission"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("model_daily_usage", sa.Column("sol_requests", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("model_daily_usage", sa.Column("sol_estimated_input_tokens", sa.BigInteger(), nullable=False, server_default="0"))


def downgrade():
    with op.batch_alter_table("model_daily_usage") as batch:
        batch.drop_column("sol_estimated_input_tokens")
        batch.drop_column("sol_requests")
