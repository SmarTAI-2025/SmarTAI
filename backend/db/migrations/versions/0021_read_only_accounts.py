"""Separate read-only business access from login suspension."""
from alembic import op
import sqlalchemy as sa

revision = "0021_read_only_accounts"
down_revision = "0020_merge_admin_knowledge"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("is_read_only", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("users", "is_read_only")
