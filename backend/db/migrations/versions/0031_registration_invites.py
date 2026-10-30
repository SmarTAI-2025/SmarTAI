"""Add single-use registration admission without changing existing users."""
from alembic import op
import sqlalchemy as sa

revision = "0031_registration_invites"
down_revision = "0030_answer_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "registration_invites",
        sa.Column("code_digest", sa.String(64), primary_key=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("used_at", sa.Float(), nullable=True),
    )
    op.add_column("email_verification_requests", sa.Column("registration_invite_digest", sa.String(64), nullable=True))


def downgrade():
    with op.batch_alter_table("email_verification_requests") as batch:
        batch.drop_column("registration_invite_digest")
    op.drop_table("registration_invites")
