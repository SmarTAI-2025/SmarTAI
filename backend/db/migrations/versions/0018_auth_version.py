"""Add monotonic user authentication version for access-token invalidation."""
from alembic import op
import sqlalchemy as sa

revision = "0018_auth_version"
down_revision = "0017_admin_audit_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("auth_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("users", "auth_version")
