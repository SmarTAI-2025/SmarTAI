"""Students API client (student-facing aggregations)."""
from __future__ import annotations

from .client import get_json


async def my_grades(token: str | None = None) -> list[dict]:
    """GET /students/me/grades"""
    return await get_json("/students/me/grades", token=token)
