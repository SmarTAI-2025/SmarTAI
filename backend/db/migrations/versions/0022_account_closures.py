"""Durable account closure and exact email registration blocks."""
from alembic import op
import sqlalchemy as sa
revision = "0022_account_closures"
down_revision = "0021_read_only_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("account_closures", sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), unique=True, nullable=False),
        sa.Column("mode", sa.String(32), nullable=False), sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False), sa.Column("completed_at", sa.Float()),
        sa.Column("lease_token", sa.String(64)), sa.Column("lease_until", sa.Float()), sa.Column("error_code", sa.String(80)))
    op.create_table("blocked_registration_emails", sa.Column("normalized_email", sa.String(320), primary_key=True),
        sa.Column("created_at", sa.Float(), nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM account_closures WHERE status != 'completed'")).scalar():
        raise RuntimeError("Finish account cleanup before downgrade")
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM blocked_registration_emails")).scalar():
        raise RuntimeError("Export and explicitly resolve email blocks before downgrade")
    op.drop_table("blocked_registration_emails")
    op.drop_table("account_closures")
