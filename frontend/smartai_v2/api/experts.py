"""Experts (BYOK) API client."""
from __future__ import annotations

from .client import post_json, get_json, delete_json


async def add_key(
    provider_type: str,
    api_key: str,
    model: str,
    base_url: str | None = None,
    display_name: str | None = None,
    max_concurrent: int = 5,
    token: str | None = None,
) -> dict:
    """POST /experts/keys → {status, provider_id}"""
    return await post_json("/experts/keys", {
        "provider_type": provider_type,
        "api_key": api_key,
        "model": model,
        "base_url": base_url,
        "display_name": display_name,
        "max_concurrent": max_concurrent,
    }, token=token)


async def list_available(token: str | None = None) -> list[dict]:
    """GET /experts/available → [{provider_id, display_name, ...}, ...]"""
    return await get_json("/experts/available", token=token)


async def select(provider_id: str, enabled: bool, token: str | None = None) -> dict:
    return await post_json("/experts/select", {"provider_id": provider_id, "enabled": enabled}, token=token)


async def remove(provider_id: str, token: str | None = None) -> dict:
    return await delete_json(f"/experts/{provider_id}", token=token)
