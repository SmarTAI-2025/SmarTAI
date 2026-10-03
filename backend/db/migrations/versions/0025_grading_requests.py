"""Durable explicit grading intents and active-run aliases."""
from alembic import op
import sqlalchemy as sa

revision = "0025_grading_requests"
down_revision = "0024_model_daily_usage"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "grading_requests",
        sa.Column("assignment_id", sa.String(64), sa.ForeignKey("assignments.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("request_id", sa.String(160), primary_key=True),
        sa.Column("workflow_revision", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("grading_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_grading_requests_run_id", "grading_requests", ["run_id"])


def downgrade():
    op.drop_table("grading_requests")
