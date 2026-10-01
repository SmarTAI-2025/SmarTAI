"""Allow fenced recognition artifacts to retain their submission revision link."""
from alembic import op
import sqlalchemy as sa

revision = "0017_revision_recognition"
down_revision = "0016_knowledge_storage_quota"
branch_labels = None
depends_on = None

NAME = "ck_source_storage_reservations_revision_link"
OLD = ("(kind = 'submission' AND submission_revision_id IS NOT NULL) OR "
       "(kind <> 'submission' AND submission_revision_id IS NULL)")
NEW = OLD + " OR (purpose = 'artifact_write' AND submission_revision_id IS NOT NULL)"


def upgrade():
    with op.batch_alter_table("source_storage_reservations") as batch:
        batch.drop_constraint(NAME, type_="check")
        batch.create_check_constraint(NAME, NEW)


def downgrade():
    # Retain cleanup intents; rolling back must never delete their object keys.
    active = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM source_storage_reservations "
        "WHERE kind <> 'submission' AND submission_revision_id IS NOT NULL"
    )).scalar()
    if active:
        raise RuntimeError("Drain revision artifact write intents before downgrade.")
    with op.batch_alter_table("source_storage_reservations") as batch:
        batch.drop_constraint(NAME, type_="check")
        batch.create_check_constraint(NAME, OLD)
