"""Store reusable BYOK credentials and their concrete model selections."""
import sqlalchemy as sa
from alembic import op

revision = "0036_byok_connections"
down_revision = "0035_shared_weekly_budget"
branch_labels = None
depends_on = None

_OLD_UNIQUE = "uq_provider_configs_owner_provider_protocol_endpoint_model"
_NEW_UNIQUE = "uq_provider_configs_route_variant"
_LINK = "fk_provider_configs_byok_connection"
_ROUTE = ("owner_id", "provider_type", "wire_protocol", "endpoint_identity", "model")


def upgrade():
    op.create_table(
        "byok_connections",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider_type", sa.String(32), nullable=False),
        sa.Column("base_url", sa.String(1024)),
        sa.Column("endpoint_identity", sa.String(1024), nullable=False),
        sa.Column("wire_protocol", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(255)),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("nonce", sa.String(128), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("rpm", sa.Integer(), nullable=False),
        sa.Column("models", sa.JSON(), nullable=False),
        sa.Column("catalog_status", sa.String(32), nullable=False),
        sa.Column("adaptive_limits", sa.JSON()),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_byok_connections_owner_id", "byok_connections", ["owner_id"])
    with op.batch_alter_table("provider_configs") as batch:
        batch.add_column(sa.Column("byok_connection_id", sa.String(64)))
        batch.add_column(sa.Column("reasoning_effort", sa.String(16)))
        batch.add_column(sa.Column("route_variant", sa.String(96), nullable=False, server_default="legacy"))
        batch.alter_column("encrypted_api_key", existing_type=sa.Text(), nullable=True)
        batch.alter_column("nonce", existing_type=sa.String(128), nullable=True)
        batch.create_foreign_key(_LINK, "byok_connections", ["byok_connection_id"], ["id"], ondelete="CASCADE")
        batch.create_index("ix_provider_configs_byok_connection_id", ["byok_connection_id"])
        batch.drop_constraint(_OLD_UNIQUE, type_="unique")
        batch.create_unique_constraint(_NEW_UNIQUE, (*_ROUTE, "route_variant"))


def downgrade():
    op.execute(sa.text(
        "UPDATE provider_preferences SET default_provider_id = NULL WHERE default_provider_id IN "
        "(SELECT id FROM provider_configs WHERE byok_connection_id IS NOT NULL)"
    ))
    op.execute(sa.text("DELETE FROM provider_configs WHERE byok_connection_id IS NOT NULL"))
    with op.batch_alter_table("provider_configs") as batch:
        batch.drop_constraint(_NEW_UNIQUE, type_="unique")
        batch.create_unique_constraint(_OLD_UNIQUE, _ROUTE)
        batch.drop_index("ix_provider_configs_byok_connection_id")
        batch.drop_constraint(_LINK, type_="foreignkey")
        batch.alter_column("nonce", existing_type=sa.String(128), nullable=False)
        batch.alter_column("encrypted_api_key", existing_type=sa.Text(), nullable=False)
        batch.drop_column("route_variant")
        batch.drop_column("reasoning_effort")
        batch.drop_column("byok_connection_id")
    op.drop_index("ix_byok_connections_owner_id", table_name="byok_connections")
    op.drop_table("byok_connections")
