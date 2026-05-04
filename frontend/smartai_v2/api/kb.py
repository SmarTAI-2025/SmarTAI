"""Task-scoped Knowledge Base (RAG) API client.

Mirrors backend/api/tasks.py KB endpoints:
  POST   /tasks/{task_id}/kb         upload + index a doc
  GET    /tasks/{task_id}/kb         list docs in this task's index
  DELETE /tasks/{task_id}/kb/{doc_id} drop one doc

The backend stores chunks + vectors fully in-memory and evicts them when
the task is deleted or the process restarts (Render free-tier sleep). The
frontend doesn't need to know — same call signature as any other upload.
"""
from __future__ import annotations

from typing import Any

from .client import post_file, get_json, delete_json


async def upload_kb(
    task_id: str,
    file_name: str,
    content: bytes,
    content_type: str = "application/octet-stream",
    *,
    token: str | None = None,
) -> dict:
    """POST /tasks/{task_id}/kb — synchronous chunk + embed.

    Returns shape:
      {status: "started"|"already_done", doc_id, filename, chunk_count, embedder?}
    """
    return await post_file(
        f"/tasks/{task_id}/kb",
        file_name, content, content_type, token=token,
    )


async def list_kb(task_id: str, *, token: str | None = None) -> dict:
    """GET /tasks/{task_id}/kb → {docs: [...]}."""
    return await get_json(f"/tasks/{task_id}/kb", token=token)


async def delete_kb(task_id: str, doc_id: str, *, token: str | None = None) -> dict:
    """DELETE /tasks/{task_id}/kb/{doc_id}."""
    return await delete_json(f"/tasks/{task_id}/kb/{doc_id}", token=token)
