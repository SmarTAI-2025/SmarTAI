"""Normalize editable provider routes and persist their wire protocol.

Revision ID: 0010_provider_wire_protocol
Revises: 0009_merge_provider_heads
"""
from __future__ import annotations

from urllib.parse import urlsplit

from alembic import op
import sqlalchemy as sa


revision = "0010_provider_wire_protocol"
down_revision = "0009_merge_provider_heads"
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
_DEFAULT_PROTOCOLS = {
    "openai": "openai_chat_completions",
    "zhipu": "openai_chat_completions",
    "deepseek": "openai_chat_completions",
    "moonshot": "openai_chat_completions",
    "qwen": "openai_chat_completions",
    "anthropic": "anthropic_messages",
    "gemini": "gemini_generate_content",
}
_OFFICIAL_ORIGINS = {
    provider_type: f"{urlsplit(endpoint).scheme}://{urlsplit(endpoint).netloc}"
    for provider_type, endpoint in _OFFICIAL_ENDPOINTS.items()
}


def upgrade() -> None:
    with op.batch_alter_table("provider_configs") as batch_op:
        batch_op.add_column(sa.Column("wire_protocol", sa.String(length=64), nullable=True))

    # The earlier draft migration temporarily represented USTC as a special,
    # disabled openai_compatible provider. Restore it to an ordinary enabled
    # DeepSeek route. Other legacy generic relays retain their URL and become
    # OpenAI-branded OpenAI Chat Completions configurations.
    op.execute(sa.text(
        "UPDATE provider_configs SET provider_type = 'deepseek', enabled = true "
        "WHERE provider_type = 'openai_compatible' AND ("
        "LOWER(base_url) = 'https://api.llm.ustc.edu.cn' OR "
        "LOWER(base_url) LIKE 'https://api.llm.ustc.edu.cn/%')"
    ))
    op.execute(sa.text(
        "UPDATE provider_configs SET provider_type = 'openai' "
        "WHERE provider_type = 'openai_compatible'"
    ))

    for provider_type, endpoint in _OFFICIAL_ENDPOINTS.items():
        protocol = _DEFAULT_PROTOCOLS[provider_type]
        origin = _OFFICIAL_ORIGINS[provider_type]
        op.execute(sa.text(
            "UPDATE provider_configs SET wire_protocol = :wire_protocol, "
            "endpoint_identity = :endpoint "
            "WHERE provider_type = :provider_type AND (base_url IS NULL OR "
            "LOWER(RTRIM(base_url, '/')) IN (LOWER(:endpoint), LOWER(:origin)))"
        ).bindparams(
            provider_type=provider_type,
            wire_protocol=protocol,
            endpoint=endpoint,
            origin=origin,
        ))
        op.execute(sa.text(
            "UPDATE provider_configs SET wire_protocol = :wire_protocol, "
            "base_url = RTRIM(base_url, '/'), "
            "endpoint_identity = RTRIM(base_url, '/') "
            "WHERE provider_type = :provider_type AND base_url IS NOT NULL "
            "AND LOWER(RTRIM(base_url, '/')) NOT IN "
            "(LOWER(:endpoint), LOWER(:origin))"
        ).bindparams(
            provider_type=provider_type,
            wire_protocol=protocol,
            endpoint=endpoint,
            origin=origin,
        ))

    with op.batch_alter_table("provider_configs") as batch_op:
        batch_op.alter_column("wire_protocol", nullable=False)
        batch_op.drop_constraint(
            "uq_provider_configs_owner_provider_endpoint_model", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_provider_configs_owner_provider_protocol_endpoint_model",
            ["owner_id", "provider_type", "wire_protocol", "endpoint_identity", "model"],
        )
        batch_op.drop_column("risk_ack_at")
        batch_op.drop_column("risk_ack_version")
        batch_op.drop_column("vision_verification_error_code")
        batch_op.drop_column("vision_last_checked_at")
        batch_op.drop_column("vision_verification_status")


def downgrade() -> None:
    with op.batch_alter_table("provider_configs") as batch_op:
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
        batch_op.drop_constraint(
            "uq_provider_configs_owner_provider_protocol_endpoint_model", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_provider_configs_owner_provider_endpoint_model",
            ["owner_id", "provider_type", "endpoint_identity", "model"],
        )
        batch_op.drop_column("wire_protocol")
