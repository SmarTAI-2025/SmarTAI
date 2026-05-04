"""Shared httpx client + helpers for backend calls."""
from __future__ import annotations

from typing import Any, AsyncIterator, Optional

import httpx

from smartai_v2.config import BACKEND_URL, REQUEST_TIMEOUT, UPLOAD_TIMEOUT


class APIError(Exception):
    def __init__(self, status: int, message: str, payload: Optional[dict] = None):
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.message = message
        self.payload = payload or {}


def _auth_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


async def get_json(path: str, *, token: str | None = None, params: dict | None = None) -> Any:
    url = f"{BACKEND_URL}{path}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as cli:
        try:
            r = await cli.get(url, headers=_auth_headers(token), params=params)
        except httpx.RequestError as e:
            raise APIError(0, f"Network error: {e}") from e
    if r.status_code >= 400:
        raise APIError(r.status_code, r.text, _safe_json(r))
    return _safe_json(r)


async def post_json(path: str, body: Any, *, token: str | None = None) -> Any:
    url = f"{BACKEND_URL}{path}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as cli:
        try:
            r = await cli.post(url, json=body, headers=_auth_headers(token))
        except httpx.RequestError as e:
            raise APIError(0, f"Network error: {e}") from e
    if r.status_code >= 400:
        raise APIError(r.status_code, r.text, _safe_json(r))
    return _safe_json(r)


async def put_json(path: str, body: Any, *, token: str | None = None) -> Any:
    url = f"{BACKEND_URL}{path}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as cli:
        try:
            r = await cli.put(url, json=body, headers=_auth_headers(token))
        except httpx.RequestError as e:
            raise APIError(0, f"Network error: {e}") from e
    if r.status_code >= 400:
        raise APIError(r.status_code, r.text, _safe_json(r))
    return _safe_json(r)


async def delete_json(path: str, *, token: str | None = None) -> Any:
    url = f"{BACKEND_URL}{path}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as cli:
        try:
            r = await cli.delete(url, headers=_auth_headers(token))
        except httpx.RequestError as e:
            raise APIError(0, f"Network error: {e}") from e
    if r.status_code >= 400:
        raise APIError(r.status_code, r.text, _safe_json(r))
    return _safe_json(r)


async def post_file(
    path: str,
    file_name: str,
    file_bytes: bytes,
    content_type: str = "application/octet-stream",
    *,
    token: str | None = None,
) -> Any:
    url = f"{BACKEND_URL}{path}"
    files = {"file": (file_name, file_bytes, content_type)}
    async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as cli:
        try:
            r = await cli.post(url, files=files, headers=_auth_headers(token))
        except httpx.RequestError as e:
            raise APIError(0, f"Network error: {e}") from e
    if r.status_code >= 400:
        raise APIError(r.status_code, r.text, _safe_json(r))
    return _safe_json(r)


async def stream_sse(path: str, *, token: str | None = None) -> AsyncIterator[dict]:
    """Async iterator that yields parsed SSE events from a backend endpoint.

    Used by progress streaming. The backend writes lines like:
      data: {"phase": "grading", ...}\\n\\n
    """
    import json

    url = f"{BACKEND_URL}{path}"
    async with httpx.AsyncClient(timeout=None) as cli:
        async with cli.stream("GET", url, headers=_auth_headers(token)) as r:
            if r.status_code >= 400:
                raise APIError(r.status_code, await r.aread().decode())
            async for line in r.aiter_lines():
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        continue


def _safe_json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}
