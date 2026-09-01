"""Add independent durable knowledge-storage quota and cleanup ledger.

Revision ID: 0016_knowledge_storage_quota
Revises: 0015_source_file_lifecycle
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0016_knowledge_storage_quota"
down_revision = "0015_source_file_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_storage_records",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=True),
        sa.Column("stored_file_id", sa.String(length=64), nullable=True),
        sa.Column("origin_assignment_id", sa.String(length=64), nullable=True),
        sa.Column(
            "retention_policy", sa.String(length=32), nullable=False,
            server_default="retained",
        ),
        sa.Column(
            "state", sa.String(length=32), nullable=False,
            server_default="reserved",
        ),
        sa.Column("original_name", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_backend", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("cleanup_reason", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("cleanup_operation_id", sa.String(length=64), nullable=True),
        sa.Column("reservation_expires_at", sa.Float(), nullable=True),
        sa.Column("unattached_expires_at", sa.Float(), nullable=True),
        sa.Column("available_at", sa.Float(), nullable=True),
        sa.Column("cleanup_requested_at", sa.Float(), nullable=True),
        sa.Column("cleanup_last_attempt_at", sa.Float(), nullable=True),
        sa.Column("cleanup_retry_at", sa.Float(), nullable=True),
        sa.Column(
            "cleanup_attempt_count", sa.Integer(), nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("cleanup_claim_token", sa.String(length=64), nullable=True),
        sa.Column("cleanup_claimed_at", sa.Float(), nullable=True),
        sa.Column("writer_claim_token", sa.String(length=64), nullable=True),
        sa.Column("writer_claimed_at", sa.Float(), nullable=True),
        sa.Column("writer_heartbeat_at", sa.Float(), nullable=True),
        sa.Column("writer_lease_expires_at", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "retention_policy IN ('retained', 'task_only')",
            name="ck_knowledge_storage_retention_policy",
        ),
        sa.CheckConstraint(
            "state IN ('reserved', 'available', 'cleanup_pending')",
            name="ck_knowledge_storage_state",
        ),
        sa.CheckConstraint(
            "size_bytes >= 0 AND cleanup_attempt_count >= 0",
            name="ck_knowledge_storage_counters_nonnegative",
        ),
        sa.CheckConstraint(
            "cleanup_reason IS NULL OR cleanup_reason IN "
            "('explicit_delete', 'task_unreferenced', 'task_deleted', "
            "'task_attach_failed', 'upload_abandoned', 'upload_write_failed', "
            "'upload_integrity_failed', 'storage_delete_failed')",
            name="ck_knowledge_storage_cleanup_reason",
        ),
        sa.CheckConstraint(
            "(cleanup_claim_token IS NULL AND cleanup_claimed_at IS NULL) OR "
            "(cleanup_claim_token IS NOT NULL AND cleanup_claimed_at IS NOT NULL)",
            name="ck_knowledge_storage_claim_consistency",
        ),
        sa.CheckConstraint(
            "(writer_claim_token IS NULL AND writer_claimed_at IS NULL AND "
            "writer_heartbeat_at IS NULL AND writer_lease_expires_at IS NULL) OR "
            "(writer_claim_token IS NOT NULL AND writer_claimed_at IS NOT NULL AND "
            "writer_heartbeat_at IS NOT NULL AND writer_lease_expires_at IS NOT NULL)",
            name="ck_knowledge_storage_writer_claim_consistency",
        ),
        sa.CheckConstraint(
            "(state = 'cleanup_pending' AND cleanup_operation_id IS NOT NULL) OR "
            "(state <> 'cleanup_pending' AND cleanup_operation_id IS NULL)",
            name="ck_knowledge_storage_operation_consistency",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_id"], ["knowledge_documents.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["stored_file_id"], ["stored_files.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["origin_assignment_id"], ["assignments.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
        sa.UniqueConstraint(
            "owner_id", "sha256", name="uq_knowledge_storage_owner_sha256"
        ),
    )
    for name, columns, unique in (
        ("ix_knowledge_storage_records_owner_id", ["owner_id"], False),
        ("ix_knowledge_storage_records_document_id", ["document_id"], True),
        ("ix_knowledge_storage_records_stored_file_id", ["stored_file_id"], True),
        ("ix_knowledge_storage_records_origin_assignment_id", ["origin_assignment_id"], False),
        ("ix_knowledge_storage_records_retention_policy", ["retention_policy"], False),
        ("ix_knowledge_storage_records_state", ["state"], False),
        ("ix_knowledge_storage_records_cleanup_operation_id", ["cleanup_operation_id"], True),
        ("ix_knowledge_storage_records_reservation_expires_at", ["reservation_expires_at"], False),
        ("ix_knowledge_storage_records_unattached_expires_at", ["unattached_expires_at"], False),
        ("ix_knowledge_storage_records_cleanup_retry_at", ["cleanup_retry_at"], False),
        ("ix_knowledge_storage_records_writer_lease_expires_at", ["writer_lease_expires_at"], False),
        ("ix_knowledge_storage_owner_state", ["owner_id", "state"], False),
        ("ix_knowledge_storage_cleanup_scan", ["state", "cleanup_retry_at", "cleanup_claimed_at"], False),
    ):
        op.create_index(name, "knowledge_storage_records", columns, unique=unique)

    # No FK to users by design. A user-row deletion must not remove the only
    # durable protection against a late object-store write.
    op.create_table(
        "knowledge_storage_orphan_guards",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("storage_backend", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("writer_claim_token", sa.String(length=64), nullable=False),
        sa.Column("next_check_at", sa.Float(), nullable=False),
        sa.Column("last_attempt_at", sa.Float(), nullable=True),
        sa.Column(
            "attempt_count", sa.Integer(), nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claimed_at", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_knowledge_orphan_guards_attempt_nonnegative",
        ),
        sa.CheckConstraint(
            "(claim_token IS NULL AND claimed_at IS NULL) OR "
            "(claim_token IS NOT NULL AND claimed_at IS NOT NULL)",
            name="ck_knowledge_orphan_guards_claim_consistency",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index(
        "ix_knowledge_storage_orphan_guards_owner_id",
        "knowledge_storage_orphan_guards", ["owner_id"], unique=False,
    )
    op.create_index(
        "ix_knowledge_storage_orphan_guards_next_check_at",
        "knowledge_storage_orphan_guards", ["next_check_at"], unique=False,
    )
    op.create_index(
        "ix_knowledge_orphan_guards_scan",
        "knowledge_storage_orphan_guards", ["next_check_at", "claimed_at"],
        unique=False,
    )

    # Existing knowledge objects predate the independent ledger.  They are
    # permanent by default and already occupy their full original byte size.
    # Broken/orphan document rows without a StoredFile do not represent proven
    # physical bytes and are intentionally not charged by this backfill.
    op.execute(sa.text(
        "INSERT INTO knowledge_storage_records ("
        "id, owner_id, document_id, stored_file_id, origin_assignment_id, "
        "retention_policy, state, original_name, content_type, size_bytes, "
        "sha256, storage_backend, storage_key, cleanup_reason, error_code, "
        "cleanup_operation_id, reservation_expires_at, unattached_expires_at, "
        "available_at, cleanup_requested_at, cleanup_last_attempt_at, "
        "cleanup_retry_at, cleanup_attempt_count, cleanup_claim_token, "
        "cleanup_claimed_at, created_at, updated_at) "
        "SELECT d.id, d.owner_id, d.id, f.id, NULL, 'retained', 'available', "
        "f.original_name, f.content_type, f.size_bytes, f.sha256, "
        "f.storage_backend, f.storage_key, NULL, NULL, NULL, NULL, NULL, "
        "d.updated_at, NULL, NULL, NULL, 0, NULL, NULL, d.created_at, d.updated_at "
        "FROM knowledge_documents d JOIN stored_files f ON f.id = d.stored_file_id "
        "WHERE f.owner_id = d.owner_id "
        "AND f.source_quota_owner_id IS NULL AND f.source_quota_bytes = 0"
    ))


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_orphan_guards_scan",
        table_name="knowledge_storage_orphan_guards",
    )
    op.drop_index(
        "ix_knowledge_storage_orphan_guards_next_check_at",
        table_name="knowledge_storage_orphan_guards",
    )
    op.drop_index(
        "ix_knowledge_storage_orphan_guards_owner_id",
        table_name="knowledge_storage_orphan_guards",
    )
    op.drop_table("knowledge_storage_orphan_guards")
    for name in (
        "ix_knowledge_storage_cleanup_scan",
        "ix_knowledge_storage_owner_state",
        "ix_knowledge_storage_records_cleanup_retry_at",
        "ix_knowledge_storage_records_writer_lease_expires_at",
        "ix_knowledge_storage_records_unattached_expires_at",
        "ix_knowledge_storage_records_reservation_expires_at",
        "ix_knowledge_storage_records_cleanup_operation_id",
        "ix_knowledge_storage_records_state",
        "ix_knowledge_storage_records_retention_policy",
        "ix_knowledge_storage_records_origin_assignment_id",
        "ix_knowledge_storage_records_stored_file_id",
        "ix_knowledge_storage_records_document_id",
        "ix_knowledge_storage_records_owner_id",
    ):
        op.drop_index(name, table_name="knowledge_storage_records")
    op.drop_table("knowledge_storage_records")
