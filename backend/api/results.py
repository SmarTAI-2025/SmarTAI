"""Results read models: teacher summary, per-student result, per-question
aggregates, review queue, and released student result (Task 8).

Deterministic aggregates (averages, counts, score distribution) are computed
from normalized SQL/result rows here, never pre-baked during grading. The
common-mistake analysis stays an on-demand endpoint backed by ``analytics_agent``;
it does not block grading or become the source for deterministic statistics.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.api.errors import domain_error_response
from backend.auth import get_current_user, require_teacher
from backend.db import assignment_repository, grading_repository, submission_repository
from backend.domain.errors import DomainError, NotFound
from backend.models import User

router = APIRouter(prefix="/results", tags=["results"])


def _effective_score(result, review) -> float | None:
    """Display score: the latest teacher review if present, else the AI score.
    Non-graded results (failed/needs_review) contribute nothing to totals."""
    if review is not None:
        return review.new_score
    return result.ai_score


def _serialize_result(result, review) -> dict:
    review_payload = result.teacher_review
    if review_payload is None and review is not None:
        review_payload = review.model_dump()
    effective_score = result.effective_score
    effective_comment = result.effective_comment
    if review is not None:
        effective_score = review.new_score
        effective_comment = review.new_comment
    return {
        "id": result.id,
        "grading_run_id": result.grading_run_id,
        "question_id": result.question_id,
        "q_id": result.q_id,
        "student_id": result.student_id,
        "ai_score": result.ai_score,
        "ai_max_score": result.ai_max_score,
        "ai_comment": result.ai_comment,
        "submission_revision_id": result.submission_revision_id,
        "result_status": result.result_status,
        "requires_review": result.requires_review,
        "review_reason": result.review_reason,
        "effective_score": effective_score,
        "effective_comment": effective_comment,
        "teacher_review": review_payload,
        "score": effective_score,
        "teacher_comment": effective_comment,
    }


def _latest_run_for_assignment(assignment_id: str, teacher_id: str):
    """Most recent released run, falling back to the newest owned run."""
    runs = grading_repository.list_runs_for_assignment(
        assignment_id=assignment_id, actor_id=teacher_id
    )
    released = [run for run in runs if run.released_at is not None]
    return max(released, key=lambda run: run.released_at or 0) if released else (runs[-1] if runs else None)


@router.get("/assignment/{assignment_id}/summary")
def teacher_summary(assignment_id: str, current: User = Depends(require_teacher)):
    """Teacher-facing deterministic summary: per-student and per-question
    aggregates over the latest released run's graded results."""
    try:
        assignment = assignment_repository.get_assignment(
            assignment_id=assignment_id, actor_id=current.id
        )
    except DomainError as exc:
        return domain_error_response(exc)
    return _summary(assignment_id, teacher_id=current.id)


@router.get("/assignment/{assignment_id}/student/{student_id}")
def teacher_student_result(assignment_id: str, student_id: str,
                           current: User = Depends(require_teacher)):
    """Per-student result view for the teacher (graded + review results)."""
    try:
        assignment_repository.get_assignment(assignment_id=assignment_id, actor_id=current.id)
    except DomainError as exc:
        return domain_error_response(exc)
    results = _all_results_for_assignment(assignment_id, current.id)
    out = []
    for r in results:
        if r.student_id != student_id:
            continue
        review = grading_repository.latest_teacher_review(grade_result_id=r.id)
        out.append(_serialize_result(r, review))
    return out


@router.get("/assignment/{assignment_id}/questions")
def per_question_aggregates(assignment_id: str, current: User = Depends(require_teacher)):
    """Deterministic per-question aggregates (mean, max, count) over graded
    results of the latest run. Non-graded results are excluded."""
    try:
        assignment_repository.get_assignment(assignment_id=assignment_id, actor_id=current.id)
    except DomainError as exc:
        return domain_error_response(exc)
    results = _all_results_for_assignment(assignment_id, current.id, released_only=True)
    by_q: dict[str, list] = {}
    for r in results:
        if r.result_status != "graded":
            continue
        by_q.setdefault(r.q_id, []).append(r)
    out = []
    for q_id, items in by_q.items():
        scores = [(_effective_score(r, grading_repository.latest_teacher_review(grade_result_id=r.id)) or 0.0) for r in items]
        max_score = items[0].ai_max_score if items else 10.0
        out.append({
            "q_id": q_id,
            "count": len(scores),
            "max_score": max_score,
            "mean": sum(scores) / len(scores) if scores else 0.0,
            "min": min(scores) if scores else 0.0,
            "max": max(scores) if scores else 0.0,
        })
    return out


@router.get("/assignment/{assignment_id}/review-queue")
def review_queue(assignment_id: str, current: User = Depends(require_teacher)):
    """Failed / needs_review results across the assignment's runs."""
    try:
        assignment_repository.get_assignment(assignment_id=assignment_id, actor_id=current.id)
    except DomainError as exc:
        return domain_error_response(exc)
    items = grading_repository.list_results_for_review(assignment_id=assignment_id)
    return [_serialize_result(r, None) for r in items]


@router.get("/assignment/{assignment_id}/me")
def student_result(assignment_id: str, current: User = Depends(get_current_user)):
    """Released results for the current student. Empty until the run is released
    and only graded results are shown (no provider traces, no draft grades)."""
    from backend.services import grading_runs
    rows = grading_runs.student_results(student_id=current.id, assignment_id=assignment_id)
    return [
        {
            "q_id": r.q_id,
            "score": r.effective_score,
            "max_score": r.ai_max_score,
            "comment": r.effective_comment,
            "effective_score": r.effective_score,
            "effective_comment": r.effective_comment,
            "ai_score": r.ai_score,
            "ai_max_score": r.ai_max_score,
            "ai_comment": r.ai_comment,
            "teacher_review": r.teacher_review,
            "result_status": r.result_status,
        }
        for r in rows
    ]


# ─── helpers ──────────────────────────────────────────────────────────────────


def _all_results_for_assignment(
    assignment_id: str, teacher_id: str, *, released_only: bool = False
) -> list:
    """Results from the newest released run, or newest run while unreleased."""
    runs = grading_repository.list_runs_for_assignment(
        assignment_id=assignment_id, actor_id=teacher_id
    )
    if not runs:
        return []
    released = [run for run in runs if run.released_at is not None]
    if released_only and not released:
        return []
    target = max(released, key=lambda run: run.released_at or 0) if released else runs[-1]
    return grading_repository.list_results_for_run(run_id=target.id)


def _summary(assignment_id: str, teacher_id: str) -> dict:
    results = _all_results_for_assignment(assignment_id, teacher_id, released_only=True)
    graded = [r for r in results if r.result_status == "graded"]
    needs_review = [r for r in results if r.result_status != "graded"]
    # Per-student total (sum of effective scores over graded results).
    by_student: dict[str, float] = {}
    for r in graded:
        review = grading_repository.latest_teacher_review(grade_result_id=r.id)
        score = _effective_score(r, review) or 0.0
        by_student[r.student_id] = by_student.get(r.student_id, 0.0) + score
    return {
        "assignment_id": assignment_id,
        "graded_count": len(graded),
        "needs_review_count": len(needs_review),
        "students": [
            {"student_id": sid, "total": total} for sid, total in by_student.items()
        ],
    }
