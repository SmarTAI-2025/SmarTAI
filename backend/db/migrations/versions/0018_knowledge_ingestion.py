"""Durable, versioned, page-scoped knowledge ingestion."""
from alembic import op
import sqlalchemy as sa

revision = "0018_knowledge_ingestion"
down_revision = "0017_revision_recognition"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("knowledge_documents", sa.Column("active_version", sa.String(64), nullable=True))
    op.add_column("knowledge_documents", sa.Column("ingestion_summary", sa.JSON(), nullable=False, server_default="{}"))
    with op.batch_alter_table("knowledge_chunks") as batch:
        batch.add_column(sa.Column("content_version", sa.String(64), nullable=False, server_default="legacy"))
        batch.drop_constraint("uq_knowledge_chunks_document_index", type_="unique")
        batch.create_unique_constraint("uq_knowledge_chunks_version_index", ["document_id", "content_version", "chunk_index"])
    op.create_table("knowledge_ingestions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("document_id", sa.String(64), sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("total_pages", sa.Integer(), nullable=False),
        sa.Column("extra_reserved", sa.Integer(), nullable=False),
        sa.Column("next_chunk_index", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(64)), sa.Column("lease_expires_at", sa.Float()),
        sa.Column("error_code", sa.String(128)),
        sa.Column("created_at", sa.Float(), nullable=False), sa.Column("updated_at", sa.Float(), nullable=False))
    for name in ("document_id", "owner_id", "status", "updated_at"):
        op.create_index("ix_knowledge_ingestions_" + name, "knowledge_ingestions", [name])
    op.create_table("knowledge_pages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("ingestion_id", sa.String(64), sa.ForeignKey("knowledge_ingestions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False), sa.Column("state", sa.String(32), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False), sa.Column("operation", sa.JSON(), nullable=False),
        sa.Column("extra_allowed", sa.Boolean(), nullable=False), sa.Column("extra_reserved", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("ingestion_id", "page_number", name="uq_knowledge_page"))
    for name in ("ingestion_id", "state"):
        op.create_index("ix_knowledge_pages_" + name, "knowledge_pages", [name])
    op.create_table("knowledge_evidence",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("document_id", sa.String(64), sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_id", sa.String(64), sa.ForeignKey("knowledge_pages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("identity_key", sa.String(64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False), sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False))
    for name in ("document_id", "page_id", "identity_key"):
        op.create_index("ix_knowledge_evidence_" + name, "knowledge_evidence", [name])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM knowledge_ingestions")).scalar():
        raise RuntimeError("Export versioned knowledge evidence before downgrade; automatic data loss is forbidden.")
    op.drop_table("knowledge_evidence")
    op.drop_table("knowledge_pages")
    op.drop_table("knowledge_ingestions")
    with op.batch_alter_table("knowledge_chunks") as batch:
        batch.drop_constraint("uq_knowledge_chunks_version_index", type_="unique")
        batch.drop_column("content_version")
        batch.create_unique_constraint("uq_knowledge_chunks_document_index", ["document_id", "chunk_index"])
    op.drop_column("knowledge_documents", "ingestion_summary")
    op.drop_column("knowledge_documents", "active_version")
