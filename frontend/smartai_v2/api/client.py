"""Shared httpx client + helpers for backend calls."""
from __future__ import annotations

import asyncio
import re
from typing import Any, AsyncIterator, Optional

import httpx

from smartai_v2.config import BACKEND_URL, REQUEST_TIMEOUT, UPLOAD_TIMEOUT


TRANSIENT_STATUS_CODES = {502, 503, 504}
RENDER_WAKE_MESSAGE = (
    "后端服务正在从 Render Free 冷启动，或暂时不可用。请等待 1 分钟后再试；"
    "如果连续失败，请检查 smartai-backend 的 Render Logs。"
)


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
            raise APIError(0, _network_error_message(e)) from e
    if r.status_code >= 400:
        raise APIError(r.status_code, _response_error_message(r), _safe_json(r))
    return _safe_json(r)


async def post_json(
    path: str,
    body: Any,
    *,
    token: str | None = None,
    retries: int = 0,
) -> Any:
    url = f"{BACKEND_URL}{path}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as cli:
        for attempt in range(retries + 1):
            try:
                r = await cli.post(url, json=body, headers=_auth_headers(token))
            except httpx.RequestError as e:
                if attempt < retries:
                    await asyncio.sleep(2 + attempt * 3)
                    continue
                raise APIError(0, _network_error_message(e)) from e
            if r.status_code not in TRANSIENT_STATUS_CODES or attempt >= retries:
                break
            await asyncio.sleep(2 + attempt * 3)
    if r.status_code >= 400:
        raise APIError(r.status_code, _response_error_message(r), _safe_json(r))
    return _safe_json(r)


async def put_json(path: str, body: Any, *, token: str | None = None) -> Any:
    url = f"{BACKEND_URL}{path}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as cli:
        try:
            r = await cli.put(url, json=body, headers=_auth_headers(token))
        except httpx.RequestError as e:
            raise APIError(0, _network_error_message(e)) from e
    if r.status_code >= 400:
        raise APIError(r.status_code, _response_error_message(r), _safe_json(r))
    return _safe_json(r)


async def delete_json(path: str, *, token: str | None = None) -> Any:
    url = f"{BACKEND_URL}{path}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as cli:
        try:
            r = await cli.delete(url, headers=_auth_headers(token))
        except httpx.RequestError as e:
            raise APIError(0, _network_error_message(e)) from e
    if r.status_code >= 400:
        raise APIError(r.status_code, _response_error_message(r), _safe_json(r))
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
            raise APIError(0, _network_error_message(e)) from e
    if r.status_code >= 400:
        raise APIError(r.status_code, _response_error_message(r), _safe_json(r))
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
                body = (await r.aread()).decode(errors="replace")
                raise APIError(r.status_code, _message_from_body(r.status_code, body))
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


def _network_error_message(e: httpx.RequestError) -> str:
    if isinstance(e, (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout)):
        return RENDER_WAKE_MESSAGE
    return f"网络错误：{e}"


def _response_error_message(r: httpx.Response) -> str:
    payload = _safe_json(r)
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("message")
        if isinstance(detail, str) and detail:
            return detail
    return _message_from_body(r.status_code, r.text)


def _message_from_body(status: int, body: str) -> str:
    if status in TRANSIENT_STATUS_CODES and (
        "Bad Gateway" in body or "<title>502</title>" in body or "Render" in body
    ):
        return RENDER_WAKE_MESSAGE
    text = re.sub(r"<[^>]+>", " ", body)
    text = " ".join(text.split())
    return text[:500] or f"请求失败：HTTP {status}"
