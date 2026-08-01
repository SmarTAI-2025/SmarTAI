"""Grading-run API router — start, progress, review, release.

The run state machine and durable lease live in the service/repository; this
router maps requests to service calls and translates DomainError into the
stable error envelope. A run may be started only by the assignment's teacher;
review and release are likewise teacher-scoped.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.errors import domain_error_response
from backend.auth import get_current_user, require_teacher
from backend.domain.errors import DomainError
from backend.models import User
from backend.services import grading_runs

router = APIRouter(prefix="/grading-runs", tags=["grading-runs"])


class StartRunRequest(BaseModel):
    assignment_id: str


class ReviewRequest(BaseModel):
    new_score: float
    new_comment: str = ""


@router.post("")
def start_run(req: StartRunRequest, current: User = Depends(require_teacher)):
    try:
        run = grading_runs.start_run(assignment_id=req.assignment_id, teacher_id=current.id)
    except DomainError as exc:
        return domain_error_response(exc)
    return grading_runs._serialize_run(run)


@router.get("/{run_id}")
def get_run(run_id: str, current: User = Depends(get_current_user)):
    try:
        payload = grading_runs.get_run(run_id=run_id, actor_id=current.id, role=current.role)
    except DomainError as exc:
        return domain_error_response(exc)
    return payload


@router.get("/by-assignment/{assignment_id}")
def list_runs(assignment_id: str, current: User = Depends(get_current_user)):
    try:
        runs = grading_runs.list_runs(assignment_id=assignment_id, actor_id=current.id, role=current.role)
    except DomainError as exc:
        return domain_error_response(exc)
    return runs


@router.get("/by-assignment/{assignment_id}/review")
def review_queue(assignment_id: str, current: User = Depends(require_teacher)):
    try:
        results = grading_runs.list_review_queue(assignment_id=assignment_id, teacher_id=current.id)
    except DomainError as exc:
        return domain_error_response(exc)
    return [_serialize_result(r) for r in results]


@router.post("/results/{grade_result_id}/review")
def add_review(grade_result_id: str, req: ReviewRequest, current: User = Depends(require_teacher)):
    try:
        review = grading_runs.add_teacher_review(
            grade_result_id=grade_result_id, teacher_id=current.id,
            new_score=req.new_score, new_comment=req.new_comment,
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return {
        "id": review.id, "grade_result_id": review.grade_result_id,
        "previous_score": review.previous_score, "new_score": review.new_score,
        "new_comment": review.new_comment, "created_at": review.created_at,
    }


@router.post("/{run_id}/release")
def release_run(run_id: str, current: User = Depends(require_teacher)):
    try:
        run = grading_runs.release_run(run_id=run_id, teacher_id=current.id)
    except DomainError as exc:
        return domain_error_response(exc)
    return grading_runs._serialize_run(run)


def _serialize_result(r) -> dict:
    return {
        "id": r.id, "grading_run_id": r.grading_run_id,
        "submission_revision_id": r.submission_revision_id, "question_id": r.question_id,
        "student_id": r.student_id, "q_id": r.q_id,
        "ai_score": r.ai_score, "ai_max_score": r.ai_max_score,
        "ai_comment": r.ai_comment, "ai_confidence": r.ai_confidence,
        "requires_review": r.requires_review, "review_reason": r.review_reason,
        "result_status": r.result_status,
        "effective_score": r.effective_score,
        "effective_comment": r.effective_comment,
        "teacher_review": r.teacher_review,
        "created_at": r.created_at,
    }
