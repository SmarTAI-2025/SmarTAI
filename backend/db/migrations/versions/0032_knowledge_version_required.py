"""Require an explicit knowledge content version for every new chunk."""
from alembic import op
import sqlalchemy as sa

revision = "0032_knowledge_version_required"
down_revision = "0031_registration_invites"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("knowledge_chunks") as batch:
        batch.alter_column("content_version", existing_type=sa.String(64),
                           existing_nullable=False, server_default=None)


def downgrade():
    with op.batch_alter_table("knowledge_chunks") as batch:
        batch.alter_column("content_version", existing_type=sa.String(64),
                           existing_nullable=False, server_default="legacy")
