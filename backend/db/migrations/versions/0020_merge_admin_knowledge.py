"""Join the shipped knowledge branch and the private administrator branch.

Keep both histories intact so existing databases at either tip can upgrade.
"""

revision = "0020_merge_admin_knowledge"
down_revision = ("0018_knowledge_ingestion", "0019_admin_usage_events")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
