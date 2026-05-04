"""Analytics API client (NL filter/summary/chart + per-question breakdown)."""
from __future__ import annotations

from .client import post_json, get_json, delete_json


async def nl_query(task_id: str, question: str, mode: str,
                   *, token: str | None = None) -> dict:
    """POST /analytics/{task_id}/query → {mode, ...mode-specific fields}"""
    return await post_json(
        f"/analytics/{task_id}/query",
        {"question": question, "mode": mode},
        token=token,
    )


async def per_question(task_id: str, q_id: str,
                       *, token: str | None = None) -> dict:
    return await get_json(f"/analytics/{task_id}/per_question/{q_id}", token=token)


async def reset_per_question_cache(task_id: str, q_id: str,
                                   *, token: str | None = None) -> dict:
    return await delete_json(f"/analytics/{task_id}/per_question/{q_id}/cache", token=token)
