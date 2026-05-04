"""Auth API client.

NOTE: backend endpoints listed here are NOT yet implemented in `backend/api/`.
See BACKEND_STUB_ENDPOINTS.md for the contract. While backend is missing, the
frontend AuthState falls back to a "demo login" mode (frontend-only token).
"""
from __future__ import annotations

from .client import post_json, get_json


async def login(username: str, password: str) -> dict:
    """POST /auth/login → {token, user}"""
    return await post_json("/auth/login", {"username": username, "password": password})


async def register(username: str, password: str, email: str, role: str, invite_code: str | None = None) -> dict:
    return await post_json("/auth/register", {
        "username": username,
        "password": password,
        "email": email,
        "role": role,
        "invite_code": invite_code,
    })


async def me(token: str) -> dict:
    return await get_json("/auth/me", token=token)


async def refresh(token: str) -> dict:
    return await post_json("/auth/refresh", {}, token=token)


async def logout(token: str) -> dict:
    return await post_json("/auth/logout", {}, token=token)
