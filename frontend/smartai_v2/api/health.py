"""Health check API client."""
from __future__ import annotations

from .client import get_json


async def root(token: str | None = None) -> dict:
    return await get_json("/", token=token)


async def health(token: str | None = None) -> dict:
    return await get_json("/health", token=token)
