"""Add atomic task-original quota reservations and cleanup tombstones.

Revision ID: 0015_source_file_lifecycle
Revises: 0014_password_reset_requests
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0015_source_file_lifecycle"
down_revision = "0014_password_reset_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("assignments") as batch_op:
        batch_op.add_column(sa.Column(
            "deletion_requested_at", sa.Float(), nullable=True
        ))
        batch_op.create_index(
            "ix_assignments_deletion_requested_at",
            ["deletion_requested_at"], unique=False,
        )

    with op.batch_alter_table("assignment_workflows") as batch_op:
        batch_op.add_column(sa.Column(
            "source_lifecycle_epoch", sa.Integer(), nullable=False,
            server_default=sa.text("0"),
        ))
        batch_op.create_check_constraint(
            "ck_assignment_workflows_source_lifecycle_epoch_nonnegative",
            "source_lifecycle_epoch >= 0",
        )

    with op.batch_alter_table("stored_files") as batch_op:
        batch_op.add_column(sa.Column(
            "source_quota_owner_id", sa.String(length=64), nullable=True
        ))
        batch_op.add_column(sa.Column(
            "source_quota_bytes", sa.Integer(), nullable=False,
            server_default=sa.text("0"),
        ))
        batch_op.add_column(sa.Column(
            "availability_status", sa.String(length=32), nullable=False,
            server_default="available",
        ))
        batch_op.add_column(sa.Column(
            "availability_reason", sa.String(length=64), nullable=True
        ))
        batch_op.add_column(sa.Column(
            "lifecycle_revision", sa.Integer(), nullable=False,
            server_default=sa.text("0"),
        ))
        batch_op.add_column(sa.Column(
            "cleanup_operation_id", sa.String(length=64), nullable=True
        ))
        batch_op.add_column(sa.Column(
            "cleanup_final_result_version", sa.Integer(), nullable=True
        ))
        batch_op.add_column(sa.Column(
            "cleanup_requested_at", sa.Float(), nullable=True
        ))
        batch_op.add_column(sa.Column(
            "cleanup_last_attempt_at", sa.Float(), nullable=True
        ))
        batch_op.add_column(sa.Column(
            "cleanup_attempt_count", sa.Integer(), nullable=False,
            server_default=sa.text("0"),
        ))
        batch_op.add_column(sa.Column(
            "cleanup_claim_token", sa.String(length=64), nullable=True
        ))
        batch_op.add_column(sa.Column(
            "cleanup_claimed_at", sa.Float(), nullable=True
        ))
        batch_op.add_column(sa.Column(
            "replacement_claim_group_id", sa.String(length=64), nullable=True
        ))
        batch_op.add_column(sa.Column(
            "replacement_claim_expires_at", sa.Float(), nullable=True
        ))
        batch_op.add_column(sa.Column(
            "replacement_group_id", sa.String(length=64), nullable=True
        ))
        batch_op.add_column(sa.Column(
            "unavailable_at", sa.Float(), nullable=True
        ))
        batch_op.create_foreign_key(
            "fk_stored_files_source_quota_owner_id_users",
            "users", ["source_quota_owner_id"], ["id"], ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_stored_files_cleanup_operation_id_workflow_operations",
            "workflow_operations", ["cleanup_operation_id"], ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_check_constraint(
            "ck_stored_files_source_quota_bytes_nonnegative",
            "source_quota_bytes >= 0",
        )
        batch_op.create_check_constraint(
            "ck_stored_files_source_lifecycle_counters_nonnegative",
            "lifecycle_revision >= 0 AND cleanup_attempt_count >= 0",
        )
        batch_op.create_check_constraint(
            "ck_stored_files_availability_status",
            "availability_status IN ('available', 'cleanup_pending', 'unavailable')",
        )
        batch_op.create_check_constraint(
            "ck_stored_files_availability_reason",
            "availability_reason IS NULL OR availability_reason IN "
            "('task_finalized', 'task_deleted', 'replaced', 'missing', "
            "'storage_delete_failed')",
        )
        batch_op.create_check_constraint(
            "ck_stored_files_replacement_claim_consistency",
            "(replacement_claim_group_id IS NULL AND "
            "replacement_claim_expires_at IS NULL) OR "
            "(replacement_claim_group_id IS NOT NULL AND "
            "replacement_claim_expires_at IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "ck_stored_files_source_quota_owner_consistency",
            "(source_quota_owner_id IS NULL AND source_quota_bytes = 0) OR "
            "(source_quota_owner_id IS NOT NULL AND source_quota_bytes >= 0)",
        )
        batch_op.create_index(
            "ix_stored_files_source_quota_owner_id",
            ["source_quota_owner_id"], unique=False,
        )
        batch_op.create_index(
            "ix_stored_files_availability_status",
            ["availability_status"], unique=False,
        )
        batch_op.create_index(
            "ix_stored_files_cleanup_operation_id",
            ["cleanup_operation_id"], unique=False,
        )
        batch_op.create_index(
            "ix_stored_files_source_quota_status",
            ["source_quota_owner_id", "availability_status"], unique=False,
        )
        batch_op.create_index(
            "ix_stored_files_assignment_source_status",
            ["assignment_id", "availability_status", "kind"], unique=False,
        )

    # Current task originals are assignment-linked. The legacy normalized
    # submission path stores its ACL owner as the student, so its quota owner is
    # derived through revision -> submission -> assignment.
    op.execute(sa.text(
        "UPDATE stored_files SET "
        "source_quota_owner_id = ("
        "  SELECT assignments.teacher_id FROM assignments "
        "  WHERE assignments.id = stored_files.assignment_id"
        "), source_quota_bytes = size_bytes "
        "WHERE kind IN ('problem', 'problem_source', 'submission_container', "
        "'submission_source', 'submission_source_reference') "
        "AND assignment_id IS NOT NULL"
    ))
    op.execute(sa.text(
        "UPDATE stored_files SET "
        "source_quota_owner_id = ("
        "  SELECT assignments.teacher_id "
        "  FROM submission_revisions "
        "  JOIN submissions ON submissions.id = submission_revisions.submission_id "
        "  JOIN assignments ON assignments.id = submissions.assignment_id "
        "  WHERE submission_revisions.id = stored_files.submission_revision_id"
        "), source_quota_bytes = size_bytes "
        "WHERE kind = 'submission' AND submission_revision_id IS NOT NULL"
    ))

    op.create_table(
        "source_storage_reservations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("file_owner_id", sa.String(length=64), nullable=False),
        sa.Column("quota_owner_id", sa.String(length=64), nullable=False),
        sa.Column("assignment_id", sa.String(length=64), nullable=False),
        sa.Column("submission_revision_id", sa.String(length=64), nullable=True),
        sa.Column(
            "source_lifecycle_epoch", sa.Integer(), nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("original_name", sa.String(length=512), nullable=False),
        sa.Column("storage_backend", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("requested_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("replacement_group_id", sa.String(length=64), nullable=True),
        sa.Column(
            "replacement_credit_bytes", sa.Integer(), nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "purpose", sa.String(length=32), nullable=False,
            server_default="upload",
        ),
        sa.Column(
            "state", sa.String(length=32), nullable=False,
            server_default="reserved",
        ),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column(
            "retry_count", sa.Integer(), nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("cleanup_claim_token", sa.String(length=64), nullable=True),
        sa.Column("cleanup_claimed_at", sa.Float(), nullable=True),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "requested_bytes >= 0 AND replacement_credit_bytes >= 0 "
            "AND retry_count >= 0 "
            "AND source_lifecycle_epoch >= 0",
            name="ck_source_storage_reservations_counters_nonnegative",
        ),
        sa.CheckConstraint(
            "purpose IN ('upload', 'orphan_cleanup', 'artifact_write')",
            name="ck_source_storage_reservations_purpose",
        ),
        sa.CheckConstraint(
            "state IN ('reserved', 'object_written', 'cleanup_pending')",
            name="ck_source_storage_reservations_state",
        ),
        sa.CheckConstraint(
            "length(kind) BETWEEN 1 AND 64",
            name="ck_source_storage_reservations_kind",
        ),
        sa.CheckConstraint(
            "(kind = 'submission' AND submission_revision_id IS NOT NULL) OR "
            "(kind <> 'submission' AND submission_revision_id IS NULL)",
            name="ck_source_storage_reservations_revision_link",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["workflow_operations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["file_owner_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["quota_owner_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["assignment_id"], ["assignments.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["submission_revision_id"], ["submission_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index(
        "ix_source_storage_reservations_operation_id",
        "source_storage_reservations", ["operation_id"], unique=True,
    )
    op.create_index(
        "ix_source_storage_reservations_file_owner_id",
        "source_storage_reservations", ["file_owner_id"], unique=False,
    )
    op.create_index(
        "ix_source_storage_reservations_quota_owner_id",
        "source_storage_reservations", ["quota_owner_id"], unique=False,
    )
    op.create_index(
        "ix_source_storage_reservations_assignment_id",
        "source_storage_reservations", ["assignment_id"], unique=False,
    )
    op.create_index(
        "ix_source_storage_reservations_submission_revision_id",
        "source_storage_reservations", ["submission_revision_id"], unique=False,
    )
    op.create_index(
        "ix_source_storage_reservations_expires_at",
        "source_storage_reservations", ["expires_at"], unique=False,
    )
    op.create_index(
        "ix_source_storage_reservations_owner_expiry",
        "source_storage_reservations", ["quota_owner_id", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_source_storage_reservations_submission_revision_id",
        table_name="source_storage_reservations",
    )
    op.drop_index(
        "ix_source_storage_reservations_owner_expiry",
        table_name="source_storage_reservations",
    )
    op.drop_index(
        "ix_source_storage_reservations_expires_at",
        table_name="source_storage_reservations",
    )
    op.drop_index(
        "ix_source_storage_reservations_assignment_id",
        table_name="source_storage_reservations",
    )
    op.drop_index(
        "ix_source_storage_reservations_quota_owner_id",
        table_name="source_storage_reservations",
    )
    op.drop_index(
        "ix_source_storage_reservations_file_owner_id",
        table_name="source_storage_reservations",
    )
    op.drop_index(
        "ix_source_storage_reservations_operation_id",
        table_name="source_storage_reservations",
    )
    op.drop_table("source_storage_reservations")

    with op.batch_alter_table("stored_files") as batch_op:
        batch_op.drop_index("ix_stored_files_assignment_source_status")
        batch_op.drop_index("ix_stored_files_source_quota_status")
        batch_op.drop_index("ix_stored_files_cleanup_operation_id")
        batch_op.drop_index("ix_stored_files_availability_status")
        batch_op.drop_index("ix_stored_files_source_quota_owner_id")
        batch_op.drop_constraint(
            "ck_stored_files_source_quota_owner_consistency", type_="check"
        )
        batch_op.drop_constraint(
            "ck_stored_files_availability_reason", type_="check"
        )
        batch_op.drop_constraint(
            "ck_stored_files_replacement_claim_consistency", type_="check"
        )
        batch_op.drop_constraint(
            "ck_stored_files_availability_status", type_="check"
        )
        batch_op.drop_constraint(
            "ck_stored_files_source_lifecycle_counters_nonnegative", type_="check"
        )
        batch_op.drop_constraint(
            "ck_stored_files_source_quota_bytes_nonnegative", type_="check"
        )
        batch_op.drop_constraint(
            "fk_stored_files_cleanup_operation_id_workflow_operations",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_stored_files_source_quota_owner_id_users", type_="foreignkey"
        )
        batch_op.drop_column("unavailable_at")
        batch_op.drop_column("cleanup_attempt_count")
        batch_op.drop_column("cleanup_claimed_at")
        batch_op.drop_column("cleanup_claim_token")
        batch_op.drop_column("replacement_claim_expires_at")
        batch_op.drop_column("replacement_claim_group_id")
        batch_op.drop_column("replacement_group_id")
        batch_op.drop_column("cleanup_last_attempt_at")
        batch_op.drop_column("cleanup_requested_at")
        batch_op.drop_column("cleanup_final_result_version")
        batch_op.drop_column("cleanup_operation_id")
        batch_op.drop_column("lifecycle_revision")
        batch_op.drop_column("availability_reason")
        batch_op.drop_column("availability_status")
        batch_op.drop_column("source_quota_bytes")
        batch_op.drop_column("source_quota_owner_id")

    with op.batch_alter_table("assignment_workflows") as batch_op:
        batch_op.drop_constraint(
            "ck_assignment_workflows_source_lifecycle_epoch_nonnegative",
            type_="check",
        )
        batch_op.drop_column("source_lifecycle_epoch")

    with op.batch_alter_table("assignments") as batch_op:
        batch_op.drop_index("ix_assignments_deletion_requested_at")
        batch_op.drop_column("deletion_requested_at")
