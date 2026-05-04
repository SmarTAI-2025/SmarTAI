"""Grading API client."""
from __future__ import annotations

from typing import AsyncIterator

from .client import post_json, get_json, delete_json, put_json, stream_sse


async def grade_student(student_id: str, token: str | None = None) -> dict:
    """POST /ai_grading/grade_student/ → {job_id}"""
    return await post_json("/ai_grading/grade_student/", {"student_id": student_id}, token=token)


async def grade_all(token: str | None = None) -> dict:
    """POST /ai_grading/grade_all/ → {job_id}"""
    return await post_json("/ai_grading/grade_all/", {}, token=token)


async def get_result(job_id: str, token: str | None = None) -> dict:
    """GET /ai_grading/grade_result/{job_id}"""
    return await get_json(f"/ai_grading/grade_result/{job_id}", token=token)


async def get_progress(job_id: str, token: str | None = None) -> dict:
    """GET /ai_grading/progress/{job_id} (snapshot)"""
    return await get_json(f"/ai_grading/progress/{job_id}", token=token)


async def stream_progress(job_id: str, token: str | None = None) -> AsyncIterator[dict]:
    """GET /ai_grading/progress/{job_id}/stream (SSE)"""
    async for ev in stream_sse(f"/ai_grading/progress/{job_id}/stream", token=token):
        yield ev


async def discard_job(job_id: str, token: str | None = None) -> dict:
    return await delete_json(f"/ai_grading/discard_job/{job_id}", token=token)


async def reset_all_grading(token: str | None = None) -> dict:
    return await delete_json("/ai_grading/reset_all_grading", token=token)


async def all_job_metadata(token: str | None = None) -> dict:
    return await get_json("/ai_grading/all_job_metadata", token=token)


async def all_history(token: str | None = None) -> dict:
    return await get_json("/ai_grading/all_history", token=token)


async def job_metadata(job_id: str, token: str | None = None) -> dict:
    return await get_json(f"/ai_grading/job_metadata/{job_id}", token=token)


async def history(job_id: str, token: str | None = None) -> dict:
    return await get_json(f"/ai_grading/history/{job_id}", token=token)


async def rename_job(job_id: str, name: str, token: str | None = None) -> dict:
    return await put_json(f"/ai_grading/rename_job/{job_id}", {"name": name}, token=token)
