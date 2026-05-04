"""Ingest API client (problem/homework upload + parsing)."""
from __future__ import annotations

from .client import post_file


async def upload_problem_file(file_name: str, content: bytes, content_type: str = "application/octet-stream",
                              token: str | None = None) -> dict:
    """POST /prob_preview/ → dict[q_id → {q_id, number, stem, criterion, type, ...}]"""
    return await post_file("/prob_preview/", file_name, content, content_type, token=token)


async def upload_homework_archive(file_name: str, content: bytes, content_type: str = "application/octet-stream",
                                  token: str | None = None) -> dict:
    """POST /hw_preview/ → dict[stu_id → {stu_id, name, answers, ...}]"""
    return await post_file("/hw_preview/", file_name, content, content_type, token=token)
