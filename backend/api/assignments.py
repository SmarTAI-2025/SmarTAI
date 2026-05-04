"""Assignments API router — CRUD + publish + submit + my_submission + my_grade."""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from backend.auth import get_current_user, require_student, require_teacher
from backend.models import Assignment, Submission, User
from backend.state import (
    get_assignment_store,
    get_course_store,
    get_submission_by_assignment_student,
    get_submission_store,
    index_submission,
)

router = APIRouter(prefix="/assignments", tags=["assignments"])


class CreateAssignmentRequest(BaseModel):
    course_id: str
    name: str
    description: str = ""
    due_at: Optional[float] = None
    problem_data: Dict[str, Dict[str, Any]] = {}


@router.post("/")
def create_assignment(req: CreateAssignmentRequest, current: User = Depends(require_teacher)):
    course = get_course_store().get(req.course_id)
    if course is None or (current.role != "admin" and course.teacher_id != current.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Not your course")
    a = Assignment(
        id=f"a_{uuid.uuid4().hex[:10]}",
        course_id=req.course_id,
        teacher_id=current.id,
        name=req.name,
        description=req.description,
        due_at=req.due_at,
        problem_data=req.problem_data,
    )
    get_assignment_store()[a.id] = a
    return _serialize(a, current)


@router.get("/")
def list_assignments(course_id: Optional[str] = None, status_filter: Optional[str] = None,
                     current: User = Depends(get_current_user)):
    store = get_assignment_store()
    items: list[Assignment] = list(store.values())
    if course_id:
        items = [a for a in items if a.course_id == course_id]
    if status_filter:
        items = [a for a in items if a.status == status_filter]
    if current.role == "student":
        # Students only see published assignments in courses they're enrolled in
        items = [a for a in items if a.status == "published" and a.course_id in current.course_ids]
    elif current.role == "teacher":
        items = [a for a in items if a.teacher_id == current.id]
    return [_serialize(a, current) for a in items]


@router.get("/{assignment_id}")
def get_assignment(assignment_id: str, current: User = Depends(get_current_user)):
    a = _get_or_404(assignment_id)
    _check_view(a, current)
    out = _serialize(a, current)
    out["problem_data"] = a.problem_data
    return out


@router.post("/{assignment_id}/publish")
def publish_assignment(assignment_id: str, current: User = Depends(require_teacher)):
    a = _get_or_404(assignment_id)
    if a.teacher_id != current.id and current.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Not your assignment")
    a.status = "published"
    a.published_at = time.time()
    return {"status": "published", "published_at": a.published_at}


@router.delete("/{assignment_id}")
def delete_assignment(assignment_id: str, current: User = Depends(require_teacher)):
    a = _get_or_404(assignment_id)
    if a.teacher_id != current.id and current.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Not your assignment")
    get_assignment_store().pop(assignment_id, None)
    return {"status": "success"}


@router.post("/{assignment_id}/submit")
async def submit_assignment(
    assignment_id: str,
    file: UploadFile = File(...),
    current: User = Depends(require_student),
):
    a = _get_or_404(assignment_id)
    if a.status != "published":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Assignment is not published")
    if a.course_id not in current.course_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Not enrolled in this course")

    content = await file.read()
    # Naive: store filename + bytes-as-text (real impl: object storage)
    try:
        text = content.decode("utf-8", errors="ignore")
    except Exception:
        text = ""

    sub = get_submission_by_assignment_student(assignment_id, current.id)
    if sub is None:
        sub = Submission(
            id=f"s_{uuid.uuid4().hex[:10]}",
            assignment_id=assignment_id,
            student_id=current.id,
            file_name=file.filename or "submission.txt",
        )
    else:
        sub.submitted_at = time.time()
        sub.file_name = file.filename or sub.file_name

    # Very simple split-by-question heuristic — replace with a backend agent later
    if a.problem_data and text:
        sub.answers = _naive_split(text, list(a.problem_data.keys()))

    index_submission(sub)
    return {"submission_id": sub.id, "status": "submitted", "submitted_at": sub.submitted_at}


@router.get("/{assignment_id}/my_submission")
def get_my_submission(assignment_id: str, current: User = Depends(require_student)):
    sub = get_submission_by_assignment_student(assignment_id, current.id)
    if sub is None:
        return {"status": "not_submitted"}
    return {
        "submission_id": sub.id,
        "submitted_at": sub.submitted_at,
        "file_name": sub.file_name,
        "answers": sub.answers,
        "grade": sub.grade,
    }


@router.get("/{assignment_id}/my_grade")
def get_my_grade(assignment_id: str, current: User = Depends(require_student)):
    sub = get_submission_by_assignment_student(assignment_id, current.id)
    if sub is None or sub.grade is None:
        return {"status": "no_grade"}
    return {"status": "graded", "grade": sub.grade}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get_or_404(aid: str) -> Assignment:
    a = get_assignment_store().get(aid)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Assignment not found")
    return a


def _check_view(a: Assignment, user: User) -> None:
    if user.role == "admin":
        return
    if user.role == "teacher" and a.teacher_id == user.id:
        return
    if user.role == "student" and a.status == "published" and a.course_id in user.course_ids:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, detail="No access to this assignment")


def _serialize(a: Assignment, viewer: User) -> dict:
    out = {
        "id": a.id,
        "course_id": a.course_id,
        "teacher_id": a.teacher_id,
        "name": a.name,
        "description": a.description,
        "status": a.status,
        "due_at": a.due_at,
        "created_at": a.created_at,
        "published_at": a.published_at,
        "problem_count": len(a.problem_data),
    }
    return out


def _naive_split(text: str, q_ids: list[str]) -> dict:
    """Split a free-form text submission across question IDs.

    This is a placeholder. Replace with backend.agents.ingest_agent.parse_student_answers
    once you wire student-uploaded files through the same pipeline.
    """
    if not q_ids:
        return {}
    chunks = max(1, len(q_ids))
    n = len(text) // chunks
    return {q: text[i * n : (i + 1) * n] for i, q in enumerate(q_ids)}
