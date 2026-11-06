"""Persist learned BYOK operating parameters separately from user RPM."""
from alembic import op
import sqlalchemy as sa

revision = "0032_provider_admission"
down_revision = "0031_registration_invites"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("provider_configs", sa.Column("adaptive_limits", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("provider_configs") as batch:
        batch.drop_column("adaptive_limits")
