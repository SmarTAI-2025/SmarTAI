"""Shared numeric progress for history, dashboard and task pages."""
from __future__ import annotations

import logging
import math

from backend.services import task_facade


logger = logging.getLogger(__name__)
ACTIVE_STATUSES = {"extracting_problems", "parsing_submissions", "grading"}
COMPLETE_STATUSES = {"graded", "finalized"}
SNAPSHOT_STATUSES = ACTIVE_STATUSES | {"problems_paused", "submissions_paused", "grading_paused"}


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def progress_fields(status: str, progress: dict | None) -> dict:
    percent = eta = None
    if status in COMPLETE_STATUSES:
        return {"progress_percent": 100, "eta_seconds": 0}
    if status == "draft":
        return {"progress_percent": 0, "eta_seconds": None}
    if status not in SNAPSHOT_STATUSES or not isinstance(progress, dict):
        return {"progress_percent": None, "eta_seconds": None}

    students = _number(progress.get("total_students")) or 0
    questions = _number(progress.get("total_questions")) or 0
    grading = status in {"grading", "grading_paused"} or progress.get("workflow") == "grading" or progress.get("phase") == "grading"
    parsing = status in {"parsing_submissions", "submissions_paused"} or progress.get("phase") == "parsing"
    total = students * questions if grading else students if parsing else max(students, questions)
    completed = _number(progress.get("completed_units"))
    steps, done_steps = _number(progress.get("total_steps")), _number(progress.get("completed_steps"))
    if steps is not None and steps > 0 and done_steps is not None:
        percent = min(100, max(0, math.floor(done_steps / steps * 100 + .5)))
        metrics = progress.get("stage_metrics") or {}
        generation_total = _number(metrics.get("solution_total_questions"))
        generation_done = _number(metrics.get("solution_completed_questions"))
        if progress.get("current_step") == "generating_solutions" and generation_total and generation_total > 0 and generation_done is not None:
            fraction = max(0, min(1, generation_done / generation_total))
            percent = min(100, max(percent, math.floor((done_steps + fraction) / steps * 100)))
    elif progress.get("phase") == "done":
        percent = 100
    elif total > 0 and completed is not None:
        percent = min(100, max(0, math.floor(completed / total * 100 + .5)))
        models = progress.get("model_progress") or []
        initial_total = sum(max(0, _number(model.get("total")) or 0) for model in models)
        if grading and initial_total > 0:
            initial_done = sum(min(max(0, _number(model.get("total")) or 0),
                max(0, (_number(model.get("succeeded")) or 0) + (_number(model.get("failed")) or 0))) for model in models)
            percent = max(0, math.floor((initial_done + min(total, max(0, completed))) / (initial_total + total) * 100 + .5))

    # Finishing model calls (even a reporter's done phase) precedes durable
    # task completion. Only graded/finalized above may show grading at 100%.
    if grading and percent is not None:
        percent = min(99, percent)
    if status == "grading" and progress.get("phase") not in {"error", "paused"} and not progress.get("error_detail"):
        eta = _number(progress.get("grading_eta_seconds"))
        if eta is not None and eta < 0:
            eta = None
    return {"progress_percent": percent, "eta_seconds": eta}


async def enrich_history_progress(items: list[dict], *, owner_id: str) -> list[dict]:
    """Enrich already-owned summaries; do not fetch state for inactive tasks."""
    result = []
    for item in items:
        status, progress = item.get("status"), None
        if status in ACTIVE_STATUSES:
            try:
                state = await task_facade.async_task_state(task_id=item["task_id"], owner_id=owner_id)
                status, progress = state.get("status"), state.get("progress")
            except Exception:
                # A disappearing/unavailable task must not break the history page.
                logger.warning("Task history progress unavailable")
        result.append({**item, "status": status, **progress_fields(status, progress)})
    return result
