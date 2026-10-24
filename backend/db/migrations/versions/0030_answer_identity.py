"""Keep answer identity independent of its editable question assignment."""
from alembic import op
import sqlalchemy as sa

revision = "0030_answer_identity"
down_revision = "0029_initial_content_language"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("submission_answers", sa.Column("answer_key", sa.String(64), nullable=True))
    op.execute("UPDATE submission_answers SET answer_key = id")
    with op.batch_alter_table("submission_answers") as batch:
        batch.alter_column("answer_key", existing_type=sa.String(64), nullable=False)
        batch.drop_constraint("uq_submission_answers_revision_question", type_="unique")
        batch.create_unique_constraint("uq_submission_answers_revision_identity", ["revision_id", "answer_key"])


def downgrade():
    # Historical revisions are immutable; never discard a temporary duplicate.
    duplicates = op.get_bind().execute(sa.text(
        "SELECT 1 FROM submission_answers GROUP BY revision_id, question_id HAVING COUNT(*) > 1 LIMIT 1"
    )).first()
    if duplicates:
        raise RuntimeError("Cannot downgrade: current or historical revisions contain duplicate answer assignments.")
    with op.batch_alter_table("submission_answers") as batch:
        batch.drop_constraint("uq_submission_answers_revision_identity", type_="unique")
        batch.create_unique_constraint("uq_submission_answers_revision_question", ["revision_id", "question_id"])
        batch.drop_column("answer_key")
