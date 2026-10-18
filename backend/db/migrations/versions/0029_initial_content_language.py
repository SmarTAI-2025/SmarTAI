"""Remember the UI language at the task's first question-upload entry."""
from alembic import op
import sqlalchemy as sa

revision = "0029_initial_content_language"
down_revision = "0028_frontier_demo_usage"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("assignment_workflows", sa.Column("initial_content_language", sa.String(2), nullable=True))


def downgrade():
    op.drop_column("assignment_workflows", "initial_content_language")
