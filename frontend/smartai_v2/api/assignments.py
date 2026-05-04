"""Assignments API client."""
from __future__ import annotations

from typing import Any, Optional

from .client import get_json, post_json, delete_json, post_file


async def create_assignment(course_id: str, name: str, description: str = "",
                            problem_data: Optional[dict] = None, due_at: Optional[float] = None,
                            token: str | None = None) -> dict:
    return await post_json("/assignments/", {
        "course_id": course_id,
        "name": name,
        "description": description,
        "problem_data": problem_data or {},
        "due_at": due_at,
    }, token=token)


async def list_assignments(course_id: Optional[str] = None, status_filter: Optional[str] = None,
                           token: str | None = None) -> list[dict]:
    params = {}
    if course_id:
        params["course_id"] = course_id
    if status_filter:
        params["status_filter"] = status_filter
    return await get_json("/assignments/", token=token, params=params)


async def get_assignment(assignment_id: str, token: str | None = None) -> dict:
    return await get_json(f"/assignments/{assignment_id}", token=token)


async def publish_assignment(assignment_id: str, token: str | None = None) -> dict:
    return await post_json(f"/assignments/{assignment_id}/publish", {}, token=token)


async def delete_assignment(assignment_id: str, token: str | None = None) -> dict:
    return await delete_json(f"/assignments/{assignment_id}", token=token)


async def submit_assignment(assignment_id: str, file_name: str, content: bytes,
                            content_type: str = "application/octet-stream",
                            token: str | None = None) -> dict:
    return await post_file(
        f"/assignments/{assignment_id}/submit",
        file_name, content, content_type, token=token,
    )


async def get_my_submission(assignment_id: str, token: str | None = None) -> dict:
    return await get_json(f"/assignments/{assignment_id}/my_submission", token=token)


async def get_my_grade(assignment_id: str, token: str | None = None) -> dict:
    return await get_json(f"/assignments/{assignment_id}/my_grade", token=token)
