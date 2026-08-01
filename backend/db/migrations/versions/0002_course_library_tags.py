"""durable course library metadata and normalized assignment tags

Revision ID: 0002_course_library_tags
Revises: 0001_normalized_learning
Create Date: 2026-07-30

The canonical upload remains knowledge_documents -> stored_files -> object
storage.  This migration adds only owner-scoped classification metadata and
normalized assignment/tag associations.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "0002_course_library_tags"
down_revision: Union[str, None] = "0001_normalized_learning"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "course_material_groups",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("course_id", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("normalized_name", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "normalized_name",
            name="uq_course_material_groups_owner_normalized_name",
        ),
    )
    op.create_index(
        "ix_course_material_groups_owner_id", "course_material_groups", ["owner_id"]
    )
    op.create_index(
        "ix_course_material_groups_course_id", "course_material_groups", ["course_id"]
    )

    op.create_table(
        "course_materials",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("course_id", sa.String(length=64), nullable=True),
        sa.Column("group_id", sa.String(length=64), nullable=True),
        sa.Column("display_name", sa.String(length=512), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False, server_default="other"),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "category IN ('textbook', 'answer', 'lecture', 'rubric', 'other')",
            name="ck_course_materials_category",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["knowledge_documents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["group_id"], ["course_material_groups.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", name="uq_course_materials_document_id"),
    )
    op.create_index("ix_course_materials_document_id", "course_materials", ["document_id"])
    op.create_index("ix_course_materials_owner_id", "course_materials", ["owner_id"])
    op.create_index("ix_course_materials_course_id", "course_materials", ["course_id"])
    op.create_index("ix_course_materials_group_id", "course_materials", ["group_id"])

    op.create_table(
        "tags",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("normalized_name", sa.String(length=80), nullable=False),
        sa.Column("color", sa.String(length=16), nullable=False, server_default="slate"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "color IN ('slate', 'blue', 'teal', 'green', 'amber', 'rose', 'violet')",
            name="ck_tags_color",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "normalized_name", name="uq_tags_owner_normalized_name"
        ),
    )
    op.create_index("ix_tags_owner_id", "tags", ["owner_id"])

    op.create_table(
        "assignment_tags",
        sa.Column("assignment_id", sa.String(length=64), nullable=False),
        sa.Column("tag_id", sa.String(length=64), nullable=False),
        sa.Column("assigned_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["assignment_id"], ["assignments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tag_id"], ["tags.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("assignment_id", "tag_id"),
        sa.UniqueConstraint(
            "assignment_id", "tag_id", name="uq_assignment_tags_assignment_tag"
        ),
    )
    op.create_index("ix_assignment_tags_tag_id", "assignment_tags", ["tag_id"])


def downgrade() -> None:
    op.drop_index("ix_assignment_tags_tag_id", table_name="assignment_tags")
    op.drop_table("assignment_tags")

    op.drop_index("ix_tags_owner_id", table_name="tags")
    op.drop_table("tags")

    op.drop_index("ix_course_materials_group_id", table_name="course_materials")
    op.drop_index("ix_course_materials_course_id", table_name="course_materials")
    op.drop_index("ix_course_materials_owner_id", table_name="course_materials")
    op.drop_index("ix_course_materials_document_id", table_name="course_materials")
    op.drop_table("course_materials")

    op.drop_index(
        "ix_course_material_groups_course_id", table_name="course_material_groups"
    )
    op.drop_index(
        "ix_course_material_groups_owner_id", table_name="course_material_groups"
    )
    op.drop_table("course_material_groups")
