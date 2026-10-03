"""Configuration-bound image evidence; existing records remain unverified."""
from alembic import op
import sqlalchemy as sa

revision = "0025_image_capability"
down_revision = "0024_model_daily_usage"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("provider_configs", sa.Column("image_capability_status", sa.String(32), nullable=False, server_default="unverified"))
    op.add_column("provider_configs", sa.Column("image_checked_at", sa.Float(), nullable=True))
    op.add_column("provider_configs", sa.Column("image_reason", sa.String(128), nullable=True))


def downgrade():
    op.drop_column("provider_configs", "image_reason")
    op.drop_column("provider_configs", "image_checked_at")
    op.drop_column("provider_configs", "image_capability_status")
