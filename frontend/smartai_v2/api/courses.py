"""Courses API client."""
from __future__ import annotations

from .client import get_json, post_json, delete_json


async def list_courses(token: str | None = None) -> list[dict]:
    return await get_json("/courses/", token=token)


async def create_course(name: str, code: str = "", description: str = "", token: str | None = None) -> dict:
    return await post_json("/courses/", {"name": name, "code": code, "description": description}, token=token)


async def get_course(course_id: str, token: str | None = None) -> dict:
    return await get_json(f"/courses/{course_id}", token=token)


async def get_course_students(course_id: str, token: str | None = None) -> list[dict]:
    return await get_json(f"/courses/{course_id}/students", token=token)


async def enroll_students(course_id: str, student_ids: list[str], token: str | None = None) -> dict:
    return await post_json(f"/courses/{course_id}/enroll", {"student_ids": student_ids}, token=token)


async def delete_course(course_id: str, token: str | None = None) -> dict:
    return await delete_json(f"/courses/{course_id}", token=token)
