"""Human-edit API client (manual updates to problems / student answers)."""
from __future__ import annotations

from .client import post_json


async def update_problems(problems: dict, token: str | None = None) -> dict:
    """POST /human_edit/problems"""
    return await post_json("/human_edit/problems", problems, token=token)


async def update_student_answers(students: dict, token: str | None = None) -> dict:
    """POST /human_edit/stu_ans"""
    return await post_json("/human_edit/stu_ans", students, token=token)
