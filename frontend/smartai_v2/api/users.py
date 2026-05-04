"""Users API client (admin/teacher views)."""
from __future__ import annotations

from .client import get_json, post_json, delete_json
import httpx
from smartai_v2.config import BACKEND_URL, REQUEST_TIMEOUT


async def list_users(token: str | None = None) -> list[dict]:
    return await get_json("/users/", token=token)


async def patch_user(user_id: str, body: dict, token: str | None = None) -> dict:
    """PATCH /users/{user_id}"""
    url = f"{BACKEND_URL}/users/{user_id}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as cli:
        r = await cli.patch(url, json=body, headers={"Authorization": f"Bearer {token}"} if token else {})
        return r.json() if r.status_code < 400 else {"error": r.text}


async def delete_user(user_id: str, token: str | None = None) -> dict:
    return await delete_json(f"/users/{user_id}", token=token)


async def invite(email: str, role: str, course_id: str | None = None, token: str | None = None) -> dict:
    return await post_json("/users/invite", {"email": email, "role": role, "course_id": course_id}, token=token)
