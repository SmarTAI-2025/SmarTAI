"""Students API router — student-facing aggregations.

  GET /students/me/grades   — list all my grades across assignments
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.auth import require_student
from backend.models import User
from backend.state import (
    get_assignment_store,
    get_submission_store,
)

router = APIRouter(prefix="/students", tags=["students"])


@router.get("/me/grades")
def my_grades(current: User = Depends(require_student)):
    out = []
    a_store = get_assignment_store()
    for sub in get_submission_store().values():
        if sub.student_id != current.id:
            continue
        a = a_store.get(sub.assignment_id)
        if a is None:
            continue
        score = max_score = None
        if isinstance(sub.grade, dict):
            score = sub.grade.get("score")
            max_score = sub.grade.get("max_score")
        out.append({
            "assignment_id": a.id,
            "assignment_name": a.name,
            "course_id": a.course_id,
            "submitted_at": sub.submitted_at,
            "graded": sub.grade is not None,
            "score": score,
            "max_score": max_score,
        })
    def get_submitted_at_key(r):
        submitted_at = r.get("submitted_at")
        if isinstance(submitted_at, (int, float)):
            return float(submitted_at)
        return 0.0 # Default for None or non-numeric types

    out.sort(key=get_submitted_at_key, reverse=True)
    return out
