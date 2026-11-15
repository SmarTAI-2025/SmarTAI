"""Keep one adaptive admission estimate per platform provider quota group."""
from alembic import op
import sqlalchemy as sa

revision = "0033_shared_admission"
down_revision = "0032_provider_admission"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("shared_provider_admission",
                    sa.Column("fingerprint", sa.String(64), primary_key=True),
                    sa.Column("adaptive_limits", sa.JSON(), nullable=False))


def downgrade():
    op.drop_table("shared_provider_admission")
