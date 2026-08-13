"""Add safe custom OpenAI-compatible provider identity and capability state.

Revision ID: 0008_custom_provider_endpoints
Revises: 0007_source_outcome_diagnostics
"""
from __future__ import annotations

from urllib.parse import urlsplit

from alembic import op
import sqlalchemy as sa


revision = "0008_custom_provider_endpoints"
down_revision = "0007_source_outcome_diagnostics"
branch_labels = None
depends_on = None


_OFFICIAL_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "deepseek": "https://api.deepseek.com/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "gemini": "https://generativelanguage.googleapis.com",
    "anthropic": "https://api.anthropic.com",
}


def upgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("provider_configs") as batch_op:
        batch_op.add_column(sa.Column("endpoint_identity", sa.String(length=1024), nullable=True))
        batch_op.add_column(sa.Column(
            "vision_verification_status", sa.String(length=32), nullable=False,
            server_default="unverified",
        ))
        batch_op.add_column(sa.Column("vision_last_checked_at", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column(
            "vision_verification_error_code", sa.String(length=128), nullable=True
        ))
        batch_op.add_column(sa.Column("risk_ack_version", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("risk_ack_at", sa.Float(), nullable=True))

    for provider_type, endpoint in _OFFICIAL_ENDPOINTS.items():
        op.execute(sa.text(
            "UPDATE provider_configs SET endpoint_identity = :endpoint "
            "WHERE provider_type = :provider_type"
        ).bindparams(endpoint=endpoint, provider_type=provider_type))
    if not op.get_context().as_sql:
        duplicate_ustc = bind.execute(sa.text(
            "SELECT owner_id, model, RTRIM(base_url, '/') AS endpoint_identity, "
            "COUNT(*) AS duplicate_count FROM provider_configs "
            "WHERE provider_type = 'deepseek' AND ("
            "LOWER(base_url) = 'https://api.llm.ustc.edu.cn' OR "
            "LOWER(base_url) LIKE 'https://api.llm.ustc.edu.cn/%') "
            "GROUP BY owner_id, model, RTRIM(base_url, '/') HAVING COUNT(*) > 1"
        )).first()
        if duplicate_ustc is not None:
            raise RuntimeError(
                "Cannot migrate duplicate USTC provider endpoint/model records"
            )
    op.execute(sa.text(
        "UPDATE provider_configs SET "
        "provider_type = 'openai_compatible', "
        "base_url = RTRIM(base_url, '/'), "
        "endpoint_identity = RTRIM(base_url, '/'), "
        "display_name = COALESCE(NULLIF(display_name, ''), 'USTC LLM'), "
        "enabled = false, verification_status = 'unverified', "
        "last_checked_at = NULL, verification_error_code = NULL "
        "WHERE provider_type = 'deepseek' AND ("
        "LOWER(base_url) = 'https://api.llm.ustc.edu.cn' OR "
        "LOWER(base_url) LIKE 'https://api.llm.ustc.edu.cn/%')"
    ))

    with op.batch_alter_table("provider_configs") as batch_op:
        batch_op.alter_column("endpoint_identity", nullable=False)
        batch_op.drop_constraint("uq_provider_configs_owner_provider_model", type_="unique")
        batch_op.create_unique_constraint(
            "uq_provider_configs_owner_provider_endpoint_model",
            ["owner_id", "provider_type", "endpoint_identity", "model"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    provider_configs = sa.table(
        "provider_configs",
        sa.column("id", sa.String()),
        sa.column("provider_type", sa.String()),
        sa.column("base_url", sa.String()),
        sa.column("endpoint_identity", sa.String()),
        sa.column("enabled", sa.Boolean()),
        sa.column("verification_status", sa.String()),
        sa.column("last_checked_at", sa.Float()),
        sa.column("verification_error_code", sa.String()),
    )
    if not op.get_context().as_sql:
        rows = list(bind.execute(sa.select(
            provider_configs.c.base_url,
        ).where(provider_configs.c.provider_type == "openai_compatible")).mappings())
        if any(
            urlsplit(row["base_url"] or "").hostname != "api.llm.ustc.edu.cn"
            for row in rows
        ):
            raise RuntimeError("Cannot downgrade non-USTC custom provider records")
    op.execute(sa.text(
        "UPDATE provider_configs SET provider_type = 'deepseek', enabled = false, "
        "verification_status = 'unverified', last_checked_at = NULL, "
        "verification_error_code = NULL "
        "WHERE provider_type = 'openai_compatible' AND ("
        "LOWER(base_url) = 'https://api.llm.ustc.edu.cn' OR "
        "LOWER(base_url) LIKE 'https://api.llm.ustc.edu.cn/%')"
    ))

    with op.batch_alter_table("provider_configs") as batch_op:
        batch_op.drop_constraint(
            "uq_provider_configs_owner_provider_endpoint_model", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_provider_configs_owner_provider_model",
            ["owner_id", "provider_type", "model"],
        )
        batch_op.drop_column("risk_ack_at")
        batch_op.drop_column("risk_ack_version")
        batch_op.drop_column("vision_verification_error_code")
        batch_op.drop_column("vision_last_checked_at")
        batch_op.drop_column("vision_verification_status")
        batch_op.drop_column("endpoint_identity")
