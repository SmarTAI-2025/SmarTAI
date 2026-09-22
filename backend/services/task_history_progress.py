"""Numeric history progress from the same owner-scoped state as task pages."""
from __future__ import annotations

import logging
import math
import time

from backend.services import task_facade


logger = logging.getLogger(__name__)
ACTIVE_STATUSES = {"extracting_problems", "parsing_submissions", "grading"}
COMPLETE_STATUSES = {"graded", "review_confirmed", "finalized"}


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def progress_fields(status: str, progress: dict | None, *, now: float) -> dict:
    percent = eta = None
    if status in COMPLETE_STATUSES:
        return {"progress_percent": 100, "eta_seconds": 0}
    if status == "draft":
        return {"progress_percent": 0, "eta_seconds": None}
    if status not in ACTIVE_STATUSES or not isinstance(progress, dict):
        return {"progress_percent": None, "eta_seconds": None}

    students = _number(progress.get("total_students")) or 0
    questions = _number(progress.get("total_questions")) or 0
    total = students * questions if status == "grading" else students if status == "parsing_submissions" else max(students, questions)
    completed = _number(progress.get("completed_units"))
    steps, done_steps = _number(progress.get("total_steps")), _number(progress.get("completed_steps"))
    if steps is not None and steps > 0 and done_steps is not None:
        percent = min(100, max(0, math.floor(done_steps / steps * 100 + .5)))
    elif progress.get("phase") == "done":
        percent = 100
    elif total > 0 and completed is not None:
        percent = min(100, max(0, math.floor(completed / total * 100 + .5)))

    # Only grading exposes completed student/question units with a useful rate.
    if status == "grading" and total > 0 and completed is not None and completed > 0 and progress.get("phase") != "error" and not progress.get("error_detail"):
        started = _number(progress.get("started_at"))
        if started is not None and started > 1_000_000_000_000:
            started /= 1000
        if started is not None and 0 < started < now:
            eta = max(0, math.floor((total - min(total, completed)) * (now - started) / completed + .5))
    return {"progress_percent": percent, "eta_seconds": eta}


async def enrich_history_progress(items: list[dict], *, owner_id: str) -> list[dict]:
    """Enrich already-owned summaries; do not fetch state for inactive tasks."""
    result = []
    now = time.time()
    for item in items:
        status, progress = item.get("status"), None
        if status in ACTIVE_STATUSES:
            try:
                state = await task_facade.async_task_state(task_id=item["task_id"], owner_id=owner_id)
                status, progress = state.get("status"), state.get("progress")
            except Exception:
                # A disappearing/unavailable task must not break the history page.
                logger.warning("Task history progress unavailable")
        result.append({**item, **progress_fields(status, progress, now=now)})
    return result
