"""Public recognition telemetry; no changes to recognition or retry policy."""
from __future__ import annotations

import asyncio
import logging
import math
from contextlib import asynccontextmanager
from contextvars import ContextVar
from weakref import WeakKeyDictionary

from backend.services.background_errors import SAFE_BACKGROUND_ERROR_CODES, safe_background_error_code

logger = logging.getLogger(__name__)
_current = ContextVar("recognition_activity", default=None)
OPERATIONS = {"question_preparation", "problem_extraction", "submission_recognition"}
STAGES = set("""validating_sources extracting_questions aligning_uploaded_materials
generating_solutions aligning_rubrics preparing_programming_tests detecting_conflicts
committing_question_packages reading_sources reading_source recognizing_structure
validating_questions detecting_scanned_content recognizing_with_ocr normalizing_ocr_output
source_prepared calling_recognition organizing_structure completed preparing_submission_files
recognizing_submissions consolidating_submission_results""".split())
STEP_ALIASES = {
    **dict.fromkeys(("recognition_inspect", "recognition_document_batch", "recognition_locate", "pdf_index"), "reading_source"),
    "recognition_read": "recognizing_with_ocr",
    **dict.fromkeys(("recognition_recheck", "recognition_assess"), "normalizing_ocr_output"),
    **dict.fromkeys(("recognition_document_complete", "recognition_resume", "recognition_restore", "recognition_cache_lookup"), "source_prepared"),
    "recognition_persist": "completed",
}
METRICS = set("""solution_total_questions solution_completed_questions solution_failed_questions
files_total files_processed identities_matched identities_needing_review answers_split
submissions_recognized parse_failures""".split())
CODES = {"activity_preparing", "activity_reading", "activity_recognizing", "activity_materials",
         "activity_saved", "activity_completed", "activity_failed", "activity_stopped",
         "activity_queued", "activity_responding", "activity_retrying", "activity_received", "activity_response_received"}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1e15 and math.isfinite(value)


def rows(value):
    return value if isinstance(value, list) else []


def mapping(value):
    return value if isinstance(value, dict) else {}


def public_request(value):
    if not isinstance(value, dict) or not isinstance(value.get("state"), str) or value["state"] not in {"queued", "responding", "retrying"}:
        return None
    result = {"state": value["state"]}
    for key in ("since", "attempt", "max_attempts", "retry_at", "count"):
        if number(value.get(key)):
            result[key] = value[key]
    if isinstance(value.get("reason"), str) and value["reason"] in {"rate_limit", "timeout", "unavailable"}:
        result["reason"] = value["reason"]
    return result


def public_message(event):
    """Map known events to fixed public codes; unknown text never crosses the API."""
    message = event.get("message", "")
    if not isinstance(message, str) or not number(event.get("ts")):
        return None
    lower = message.lower()
    if message in CODES or message in SAFE_BACKGROUND_ERROR_CODES:
        code = message
    elif lower == "phase: done" or lower in {"submission recognition completed.", "question materials are ready for teacher review.", "problem recognition completed."}:
        code = "activity_completed"
    elif lower.startswith(("reading source ", "reading pdf ", "reading problem source", "reading source evidence", "locating candidate source pages", "inspecting source evidence", "ocr recognizing ")):
        code = "activity_reading"
    elif lower.startswith(("generating materials for major question ", "generating complete answers", "aligning answers", "matching uploaded answers", "normalizing programming")):
        code = "activity_materials"
    elif lower.startswith("major question ") and lower.endswith(" generation completed"):
        code = "activity_received"
    elif lower.startswith("major question ") and lower.endswith(" generation failed"):
        code = "activity_failed"
    elif lower in {"submission recognized.", "completed one grading unit."}:
        code = "activity_received"
    elif lower.startswith(("saving recognition evidence", "committing prepared question", "consolidating recognized submissions")):
        code = "activity_saved"
    elif lower in {"phase: parsing", "phase: extracting", "recognizing question structure", "problem recognition started.", "submission recognition started.", "organizing recognized problem structure."}:
        code = "activity_recognizing"
    elif lower in {"problem source prepared.", "submission files prepared.", "validated question and optional material sources"}:
        code = "activity_preparing"
    else:
        return None
    result = {"ts": event["ts"], "level": event.get("level") if event.get("level") in ("info", "warn", "error") else "info", "message": code}
    if details := public_request(event.get("recognition")):
        result["recognition"] = details
    return result


def public_progress(progress, *, terminal=False):
    """Explicit field allowlist for live AND persisted/legacy progress."""
    if not isinstance(progress, dict):
        return None
    result = {"contract_version": 1, "active": [], "messages": [], "stage_metrics": {}, "model_waits": []}
    for key in ("job_id",):
        if isinstance(progress.get(key), str):
            result[key] = progress[key][:120]
    result["phase"] = progress.get("phase") if progress.get("phase") in ("pending", "ingesting", "extracting", "parsing", "paused", "done", "error") else "parsing"
    for key in ("total_students", "total_questions", "completed_units", "started_at", "last_activity_at", "total_steps", "completed_steps"):
        result[key] = progress.get(key) if number(progress.get(key)) else (None if key.endswith("_at") or key.endswith("steps") else 0)
    workflow = progress.get("workflow")
    result["workflow"] = workflow if workflow in ("question_preparation", "problem_recognition", "submission_recognition") else None
    step = progress.get("current_step")
    result["current_step"] = step if isinstance(step, str) and step in STAGES else STEP_ALIASES.get(step) if isinstance(step, str) else None
    result["stage_sequence"] = [s for s in rows(progress.get("stage_sequence")) if isinstance(s, str) and s in STAGES]
    metrics = progress.get("stage_metrics")
    if isinstance(metrics, dict):
        result["stage_metrics"] = {k: int(v) for k, v in metrics.items() if k in METRICS and number(v)}
    for key in ("completed_question_ids", "active_question_ids", "failed_question_ids"):
        result[key] = [s for s in rows(progress.get(key)) if isinstance(s, str) and len(s) <= 120]
    ids = set(sum((result[k] for k in ("completed_question_ids", "active_question_ids", "failed_question_ids")), []))
    labels = mapping(progress.get("question_labels"))
    result["question_labels"] = {k: v[:120] for k, v in labels.items() if k in ids and isinstance(v, str)}
    errors = mapping(progress.get("question_error_codes"))
    result["question_error_codes"] = {k: safe_background_error_code(v, "question_preparation_failed") for k, v in errors.items() if k in ids}
    result["error_detail"] = safe_background_error_code(progress["error_detail"], "workflow_failed") if progress.get("error_detail") else None
    result["messages"] = [safe for event in rows(progress.get("messages")) if isinstance(event, dict) and (safe := public_message(event))][-200:]
    # Persisted snapshots can predate the typed contract. Validate diagnostics
    # independently and never expose raw exception fields from legacy rows.
    from backend.models import ProviderFailure
    from pydantic import ValidationError
    result["provider_failures"] = []
    for failure in rows(progress.get("provider_failures"))[-8:]:
        if not isinstance(failure, dict):
            continue
        try:
            safe_failure = ProviderFailure.model_validate(failure)
        except ValidationError:
            continue
        result["provider_failures"].append(safe_failure.model_dump(mode="json"))
    result["recognition_requests"] = [] if terminal or result["phase"] in {"paused", "done", "error"} else [safe for row in rows(progress.get("recognition_requests")) if (safe := public_request(row))]
    # Keep existing rate-limit recovery controls compatible with old snapshots.
    from backend.progress.grading_activity import public_waits
    if not terminal and result["phase"] not in {"paused", "done", "error"}:
        result["model_waits"] = public_waits(progress.get("model_waits"))
        if "recognition_requests" not in progress:
            result["recognition_requests"] = [{"state": "retrying", "reason": "rate_limit",
                **{k: wait[k] for k in ("attempt", "max_attempts", "retry_at")}}
                for wait in result["model_waits"]]
    failure = progress.get("recognition_failure")
    if isinstance(failure, dict):
        result["recognition_failure"] = {key: [int(v) for v in rows(failure.get(key)) if number(v)] for key in ("failed_pages", "processed_pages")}
    return result


class RecognitionActivity:
    def __init__(self, operation):
        self.operation = operation
        self.requests = WeakKeyDictionary()
        self.lock = asyncio.Lock()
        self.active = True

    async def capture(self, reporter, event):
        reporter.recognition_activity = self
        if public_message(event.model_dump(mode="json")) is None:
            return
        await self.save(reporter)

    async def save(self, reporter):
        try:
            async with self.lock:
                if self.active:
                    # Keep recovery fields intact. Only messages are projected here;
                    # the API projects the complete snapshot independently.
                    snapshot = (await reporter.snapshot()).model_dump(mode="json")
                    await self.operation.update_progress(snapshot)
        except Exception:
            logger.warning("Recognition progress could not be saved", exc_info=False)

    def states(self):
        # Count identical waits together. Large classes must not enlarge the
        # existing 64 KiB operation snapshot or retain completed task results.
        groups = {}
        for task, row in self.requests.items():
            if task.done():
                continue
            key = tuple(row.get(k) for k in ("state", "reason", "attempt", "max_attempts"))
            if key not in groups:
                groups[key] = {**row, "count": 0}
            group = groups[key]
            group["count"] += 1
            for field in ("since", "retry_at"):
                if field in row:
                    group[field] = min(group.get(field, row[field]), row[field])
        return list(groups.values())

    async def transition(self, state, **details):
        from backend.progress.tracker import get_reporter
        from backend.models import ProgressEvent
        import time
        reporter = get_reporter(self.operation.operation_id)
        if reporter is None or not self.active:
            return
        task = asyncio.current_task()
        if state == "processing":
            self.requests.pop(task, None)
            code = "activity_response_received"
            request = None
        else:
            request = public_request({"state": state, "since": time.time(), **details})
            if request is None:
                return
            self.requests[task] = request
            code = f"activity_{state}"
        await reporter._emit(ProgressEvent(message=code, recognition=request))


@asynccontextmanager
async def observe_recognition(operation):
    observer = RecognitionActivity(operation) if operation.operation_type in OPERATIONS else None
    token = _current.set(observer)
    try:
        yield
    finally:
        if observer:
            observer.active = False
            observer.requests.clear()
        _current.reset(token)


async def capture(reporter, event):
    if (observer := _current.get()) is not None and observer.operation.operation_id == reporter.job_id:
        await observer.capture(reporter, event)


async def report_provider_state(provider, state, **details):
    """Share existing observation points without changing the call/retry path."""
    from backend.progress.grading_activity import report_provider_state as grading_state
    await grading_state(provider, state, **details)
    if (observer := _current.get()) is not None:
        try:
            await observer.transition(state, **details)
        except Exception:
            logger.warning("Recognition activity update failed", exc_info=False)


async def finish_provider_request():
    """Clear a failed/cancelled request without calling it a successful response."""
    if (observer := _current.get()) is not None:
        try:
            if observer.requests.pop(asyncio.current_task(), None) is not None:
                from backend.progress.tracker import get_reporter
                if (reporter := get_reporter(observer.operation.operation_id)) is not None:
                    await observer.save(reporter)
        except Exception:
            logger.warning("Recognition request status could not be cleared", exc_info=False)
