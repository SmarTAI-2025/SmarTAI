"""Configuration-bound image evidence; existing records remain unverified."""
from alembic import op
import sqlalchemy as sa

revision = "0026_image_capability"
down_revision = "0025_grading_requests"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("provider_configs", sa.Column("image_capability_status", sa.String(32), nullable=False, server_default="unverified"))
    op.add_column("provider_configs", sa.Column("image_checked_at", sa.Float(), nullable=True))
    op.add_column("provider_configs", sa.Column("image_reason", sa.String(128), nullable=True))
    op.create_table("shared_provider_image_evidence",
        sa.Column("owner_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("provider_type", sa.String(32), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("checked_at", sa.Float(), nullable=True),
        sa.Column("reason", sa.String(128), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False))


def downgrade():
    op.drop_table("shared_provider_image_evidence")
    op.drop_column("provider_configs", "image_reason")
    op.drop_column("provider_configs", "image_checked_at")
    op.drop_column("provider_configs", "image_capability_status")
