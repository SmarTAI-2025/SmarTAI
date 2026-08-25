"""Merge operation leases with the published custom-provider draft branch.

Revision ID: 0009_merge_provider_heads
Revises: 0008_operation_leases, 0008_custom_provider_endpoints
"""
from __future__ import annotations


revision = "0009_merge_provider_heads"
down_revision = (
    "0008_operation_leases",
    "0008_custom_provider_endpoints",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
