"""Durable global admission for the isolated anonymous Frontier demo."""
from alembic import op
import sqlalchemy as sa

revision = "0028_frontier_demo_usage"
down_revision = "0027_task_student_identities"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "frontier_demo_daily_usage",
        sa.Column("day", sa.String(10), primary_key=True),
        sa.Column("sessions", sa.BigInteger(), nullable=False),
        sa.Column("requests", sa.BigInteger(), nullable=False),
        sa.Column("estimated_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("last_session_at", sa.Float(), nullable=False),
        sa.CheckConstraint("sessions >= 0 AND requests >= 0 AND estimated_input_tokens >= 0", name="ck_frontier_demo_usage_nonnegative"),
    )


def downgrade():
    op.drop_table("frontier_demo_daily_usage")
