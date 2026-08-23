"""Normalized implementation of the Figma task presentation contract.

``task_id`` is the normalized assignment id.  This service translates between
the presentation DTO expected by the React app and the existing
course/assignment/submission/grading repositories.  It intentionally does not
recreate the removed TaskStore or JobStore.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
import uuid
from collections import defaultdict
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import delete, func, select, update

from backend.agents.ingest_agent import (
    SubmissionSourceParseResult,
    extract_problems,
    parse_student_answer_sources,
)
from backend.db import (
    assignment_repository,
    course_repository,
    file_repository,
    grading_repository,
    source_outcome_repository,
    submission_repository,
    workflow_repository,
)
from backend.db.models import (
    AssignmentQuestionRecord,
    AssignmentRecord,
    CourseEnrollmentRecord,
    CourseRecord,
    GradingRunRecord,
    SubmissionAnswerRecord,
    SubmissionRecord,
    SubmissionRevisionRecord,
    UserRecord,
)
from backend.db.session import session_scope
from backend.domain import education
from backend.domain.errors import (
    DomainError,
    DuplicateActiveRun,
    InvalidTransition,
    LeaseLost,
    NotFound,
    ValidationError,
    VersionConflict,
)
from backend.domain.source_outcomes import safe_source_diagnostic
from backend.models import TaskGradingSetup
from backend.models import User
from backend.progress.tracker import get_or_create_reporter, get_reporter, remove_reporter
from backend.services import grading_runs
from backend.services.background_errors import (
    SAFE_BACKGROUND_ERROR_CODES,
    classify_background_error,
    is_retryable_background_error,
    safe_background_error_code,
)
from backend.services.result_artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    build_artifact_bundle,
    build_artifact_files,
    build_artifact_manifest,
)
from backend.services.submission_source_pipeline import (
    failure_phase_for_code,
    prepare_submission_sources,
)
from backend.skills.ocr_ingest import LLMVisionOCRSkill
from backend.storage import get_storage
from backend.tools.file_processing import (
    ARCHIVE_EXTENSIONS,
    extract_text_from_upload,
    infer_upload_content_type,
)


SYSTEM_COURSE_CODE = "__SMARTAI_UNASSIGNED__"
SYSTEM_COURSE_NAME = "SmarTAI Workspace"
_SAFE_ERROR_CODES = SAFE_BACKGROUND_ERROR_CODES
_AUXILIARY_QUESTION_OPERATION_TYPES = {"material_import", "ai_completion"}
_OPERATION_PUBLICATION_TTL_SECONDS = 60
_OPERATION_RUNTIME_TTL_SECONDS = 2 * 60 * 60
logger = logging.getLogger(__name__)


def _hash_json(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _ensure_system_course(owner_id: str) -> str:
    with session_scope() as session:
        existing = session.scalar(
            select(CourseRecord).where(
                CourseRecord.teacher_id == owner_id,
                CourseRecord.code == SYSTEM_COURSE_CODE,
            )
        )
        if existing is not None:
            return existing.id
        now = time.time()
        course_id = f"course_system_{uuid.uuid4().hex[:10]}"
        session.add(CourseRecord(
            id=course_id,
            name=SYSTEM_COURSE_NAME,
            code=SYSTEM_COURSE_CODE,
            description="Internal course for tasks without a selected course.",
            teacher_id=owner_id,
            created_at=now,
            updated_at=now,
        ))
        return course_id


def create_task(
    *, owner_id: str, name: str, semester_id: str | None, course_id: str | None,
    idempotency_key: str, tag_ids: list[str] | None = None,
) -> dict:
    normalized_name = name.strip()
    if not normalized_name:
        raise ValidationError("task_name_required")
    if not idempotency_key or len(idempotency_key) > 160:
        raise ValidationError("idempotency_key_required")
    request_hash = _hash_json({
        "name": normalized_name,
        "semester_id": semester_id,
        "course_id": course_id,
        "tag_ids": sorted(set(tag_ids or [])),
    })
    from backend.services.task_creation import create_task_bundle

    assignment_id, _created = create_task_bundle(
        owner_id=owner_id,
        name=normalized_name,
        semester_id=semester_id,
        course_id=course_id,
        tag_ids=tag_ids or [],
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        system_course_code=SYSTEM_COURSE_CODE,
        system_course_name=SYSTEM_COURSE_NAME,
    )
    return get_task(task_id=assignment_id, owner_id=owner_id, full=False)


def update_task(
    *, task_id: str, owner_id: str, name: str | None = None,
    semester_id: str | None | object = ..., course_id: str | None | object = ...,
    tag_ids: list[str] | None = None,
) -> dict:
    assignment = assignment_repository.get_assignment(task_id, actor_id=owner_id)
    changes: dict[str, Any] = {}
    if name is not None:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValidationError("task_name_required")
        changes["name"] = normalized_name
    if course_id is not ...:
        resolved_course = course_id or _ensure_system_course(owner_id)
        course_repository.get_course(resolved_course, actor_id=owner_id)
        changes["course_id"] = resolved_course
    if changes:
        with session_scope() as session:
            row = session.scalar(select(AssignmentRecord).where(
                AssignmentRecord.id == task_id,
                AssignmentRecord.teacher_id == owner_id,
            ))
            if row is None:
                raise NotFound("assignment")
            for key, value in changes.items():
                setattr(row, key, value)
            row.version += 1
            row.updated_at = time.time()
    workflow_changes: dict[str, Any] = {}
    if semester_id is not ...:
        workflow_changes["semester_id"] = semester_id
    # Assignment metadata is independent from the grading workflow.  In
    # particular, renaming a task must not make an in-flight OCR job stale.
    if workflow_changes:
        workflow_repository.update_workflow(
            task_id, owner_id=owner_id, **workflow_changes
        )
    if tag_ids is not None:
        _set_task_tags(task_id, owner_id, tag_ids)
    return get_task(task_id=task_id, owner_id=owner_id, full=False)


def delete_task(*, task_id: str, owner_id: str) -> None:
    assignment_repository.get_assignment(task_id, actor_id=owner_id)
    with session_scope() as session:
        result = session.execute(delete(AssignmentRecord).where(
            AssignmentRecord.id == task_id,
            AssignmentRecord.teacher_id == owner_id,
        ))
        if result.rowcount != 1:
            raise NotFound("assignment")


def list_tasks(*, owner_id: str) -> dict[str, dict]:
    with session_scope() as session:
        assignments = session.scalars(
            select(AssignmentRecord)
            .where(AssignmentRecord.teacher_id == owner_id)
            .order_by(AssignmentRecord.updated_at.desc())
        ).all()
        ids = [row.id for row in assignments]
    output: dict[str, dict] = {}
    for task_id in ids:
        try:
            output[task_id] = get_task(task_id=task_id, owner_id=owner_id, full=False)
        except NotFound:
            continue
    return output


def get_task(*, task_id: str, owner_id: str, full: bool = True) -> dict:
    assignment = assignment_repository.get_assignment(task_id, actor_id=owner_id)
    try:
        workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    except NotFound:
        workflow = workflow_repository.ensure_workflow(
            assignment_id=task_id, owner_id=owner_id
        )
    workflow = _reconcile_terminal_active_operation(
        task_id=task_id, owner_id=owner_id, workflow=workflow
    )
    questions = assignment_repository.list_questions(task_id, teacher_id=owner_id)
    submissions = _active_submissions(task_id, owner_id)
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    latest_run = _current_grading_run(workflow, runs)
    grading_error_code = _workflow_grading_failure_code(workflow, latest_run)
    status = _presentation_status(workflow, questions, submissions, latest_run)
    selected_docs = _selected_knowledge(task_id, owner_id)
    tag_ids = _get_task_tags(task_id, owner_id)
    source_summary, source_rows = _submission_source_projection(workflow, owner_id)
    attention = bool(
        workflow.error_code
        or source_summary["failed"]
        or source_summary["identity_needs_review"]
        or any(row["unknown_question_ids"] for row in source_rows)
        or (latest_run and latest_run.status in {"failed", "partial_failed"})
        or (latest_run and grading_repository.has_review_queue_items(latest_run.id))
    )
    final_version = max(
        workflow.final_result_version,
        1 if latest_run and latest_run.released_at is not None else 0,
    )
    final_result_dirty = _final_result_is_dirty(runs, latest_run)
    payload: dict[str, Any] = {
        "task_id": assignment.id,
        "name": assignment.name,
        "owner_id": assignment.teacher_id,
        "status": status,
        "workflow_revision": workflow.workflow_revision,
        "semester_id": workflow.semester_id,
        "course_id": None if _is_system_course(assignment.course_id) else assignment.course_id,
        "tag_ids": tag_ids,
        "needs_attention": attention,
        "extract_job_id": workflow.extract_job_id,
        "parse_job_id": workflow.parse_job_id,
        "grading_job_id": latest_run.id if latest_run else workflow.grading_job_id,
        "last_failed_job_id": (
            latest_run.id if grading_error_code and latest_run else workflow.last_failed_job_id
        ),
        "problem_file_name": workflow.problem_file_name,
        "submission_file_name": workflow.submission_file_name,
        "pending_submission_file_name": workflow.pending_submission_file_name,
        "submission_identity_mode": workflow.submission_identity_mode,
        "submission_roster_name": workflow.submission_roster_name,
        "submission_recognition_provider_id": workflow.submission_recognition_provider_id,
        "reference_file_name": workflow.reference_file_name,
        "test_cases_file_name": workflow.test_cases_file_name,
        "reference_parse_job_id": None,
        "test_cases_parse_job_id": None,
        "ai_completion_job_id": None,
        "last_ai_completion_job_id": None,
        "ai_completion_error": None,
        "grading_setup_configured": workflow.grading_setup is not None,
        "final_result_version": final_version,
        "final_result_updated_at": workflow.final_result_updated_at or (
            latest_run.released_at if latest_run else None
        ),
        "final_result_dirty": final_result_dirty,
        "analysis_status": workflow.analysis_status,
        "analysis_result_version": workflow.analysis_result_version,
        "analysis_generated_at": workflow.analysis_generated_at,
        "analysis_error": workflow.analysis_error_code,
        "problem_count": len(questions),
        "student_count": len(submissions),
        "submission_source_summary": source_summary,
        "kb_docs": selected_docs,
        "kb_doc_count": len(selected_docs),
        "error": grading_error_code or workflow.error_code,
        "created_at": assignment.created_at,
        "updated_at": max(assignment.updated_at, workflow.updated_at),
    }
    if full:
        payload["problem_data"] = {
            question.q_id: _serialize_problem(question) for question in questions
        }
        payload["student_data"] = _serialize_student_data(
            task_id, owner_id, submissions
        )
        payload["submission_sources"] = source_rows
    return payload


def _submission_source_projection(workflow, owner_id: str) -> tuple[dict[str, int], list[dict]]:
    empty = {
        "uploaded": 0,
        "parsed": 0,
        "failed": 0,
        "identity_needs_review": 0,
        "pending": 0,
    }
    if not workflow.parse_job_id:
        return empty, []
    try:
        operation = workflow_repository.get_operation(
            workflow.parse_job_id, owner_id=owner_id
        )
        results = source_outcome_repository.list_source_results(
            operation_id=operation.id,
            owner_id=owner_id,
            attempt=operation.attempt,
        )
    except NotFound:
        return empty, []

    presentations = workflow_repository.list_student_presentations(workflow.assignment_id)
    resolved_identity_sources = {
        presentation.source_id
        for presentation in presentations.values()
        if presentation.source_id and presentation.identity_status == "matched"
    }
    rows: list[dict] = []
    summary = dict(empty)
    for result in results:
        outcome = result.outcome
        terminal_without_outcome = (
            outcome is None and operation.status not in {"pending", "running"}
        )
        safe_reason, safe_phase = (
            safe_source_diagnostic(
                outcome.status,
                outcome.stable_error_code,
                outcome.failure_phase,
            )
            if outcome is not None
            else safe_source_diagnostic(
                "parse_failed",
                operation.error_code or "submission_outcome_persistence_failed",
                failure_phase_for_code(
                    operation.error_code or "submission_outcome_persistence_failed"
                ),
            )
            if terminal_without_outcome
            else (None, None)
        )
        if terminal_without_outcome:
            external_status = "failed"
            internal_status = "parse_failed"
            summary["failed"] += 1
        elif outcome is None:
            external_status = "processing"
            internal_status = "pending"
            summary["pending"] += 1
        elif outcome.status == "parsed":
            external_status = "parsed"
            internal_status = outcome.status
            summary["parsed"] += 1
        elif (
            outcome.status == "identity_conflict"
            and result.source.id in resolved_identity_sources
        ):
            external_status = "parsed"
            internal_status = outcome.status
            summary["parsed"] += 1
        elif outcome.status == "identity_conflict":
            external_status = "identity_needs_review"
            internal_status = outcome.status
            summary["identity_needs_review"] += 1
        else:
            external_status = "failed"
            internal_status = outcome.status
            summary["failed"] += 1
        summary["uploaded"] += 1
        rows.append({
            "source_id": result.source.id,
            "file_id": result.source.stored_file_id,
            "file_name": result.source.original_name,
            "content_type": result.source.content_type,
            "size_bytes": result.source.size_bytes,
            "status": external_status,
            "internal_status": internal_status,
            "reason_code": (
                None
                if outcome
                and outcome.status == "identity_conflict"
                and result.source.id in resolved_identity_sources
                else safe_reason
            ),
            "recognition_reason_code": safe_reason,
            "resolution_status": (
                "identity_resolved"
                if outcome
                and outcome.status == "identity_conflict"
                and result.source.id in resolved_identity_sources
                else None
            ),
            "failure_phase": safe_phase,
            "retryable": (
                outcome.retryable
                if outcome
                else is_retryable_background_error(safe_reason or "")
            ),
            "student_candidate": outcome.student_candidate if outcome else None,
            "matched_answer_count": outcome.matched_answer_count if outcome else 0,
            "unknown_question_ids": list(outcome.unknown_question_ids) if outcome else [],
            "job_id": operation.id,
            "attempt": operation.attempt,
            "trace_id": f"{operation.id}:{operation.attempt}:{result.source.id}",
            "created_at": outcome.created_at if outcome else result.source.created_at,
        })
    return summary, rows


def _presentation_status(workflow, questions, submissions, latest_run) -> str:
    if workflow.presentation_status in {
        "extracting_problems", "parsing_submissions", "generating_analysis", "error"
    }:
        return workflow.presentation_status
    if latest_run is not None:
        if latest_run.status in {"queued", "running"}:
            return "grading"
        if latest_run.status == "failed":
            return "error"
        if latest_run.released_at is not None:
            return "finalized"
        if latest_run.status in {"completed", "partial_failed"}:
            return "graded"
    if submissions:
        return "submissions_ready"
    if questions:
        return "problems_ready"
    return "draft"


def _current_grading_run(workflow, runs):
    """Return the run selected by the workflow, not an obsolete history row."""
    if workflow.grading_job_id:
        return next(
            (run for run in reversed(runs) if run.id == workflow.grading_job_id),
            None,
        )
    # Older façade rows may predate the pointer.  Only status values that
    # explicitly describe a result allow a one-time legacy fallback.  A rewind
    # sets an earlier status and a null pointer, so historical runs stay hidden.
    if workflow.presentation_status in {
        "draft", "grading", "graded", "review_confirmed", "finalized"
    }:
        return runs[-1] if runs else None
    return None


def _final_result_is_dirty(runs, current_run) -> bool:
    """Return whether a released result was detached by a newer generation."""
    return bool(
        any(run.released_at is not None for run in runs)
        and (current_run is None or current_run.released_at is None)
    )


def _reconcile_terminal_active_operation(*, task_id: str, owner_id: str, workflow):
    """Repair durable active markers left behind by a terminated worker.

    Grading jobs live in ``grading_runs`` while OCR/import jobs live in
    ``workflow_operations``.  Treating both ids as workflow-operation ids is
    what caused failed grading tasks to remain permanently busy.
    """
    job_id = workflow.active_job_id
    if not job_id or workflow.active_operation != "grading":
        return workflow
    try:
        run = grading_repository.get_run(job_id, actor_id=owner_id)
    except NotFound:
        repaired = workflow_repository.update_workflow_if_active_job(
            task_id,
            owner_id=owner_id,
            active_job_id=job_id,
            active_operation=None,
            presentation_status="error",
            grading_job_id=None,
            last_failed_job_id=job_id,
            error_code="grading_persistence_failed",
        )
        return repaired or workflow_repository.get_workflow(
            task_id, owner_id=owner_id
        )
    if run.status in education.ACTIVE_GRADING_RUN_STATUSES:
        return workflow
    failed = run.status == education.GradingRunStatus.FAILED.value
    grading_error_code = _grading_failure_code(run) if failed else None
    repaired = workflow_repository.update_workflow_if_active_job(
        task_id,
        owner_id=owner_id,
        active_job_id=job_id,
        active_operation=None,
        presentation_status=("error" if failed else "graded"),
        last_failed_job_id=(job_id if failed else None),
        error_code=grading_error_code,
    )
    return repaired or workflow_repository.get_workflow(
        task_id, owner_id=owner_id
    )


def _grading_failure_code(run) -> str | None:
    if run is None or run.status != education.GradingRunStatus.FAILED.value:
        return None
    return safe_background_error_code(run.error_message, "grading_failed")


def _workflow_grading_failure_code(workflow, run) -> str | None:
    """Project a grading failure only while that run owns the task failure.

    A confirmed upstream restart can fail after an older grading run failed.
    The historical run remains queryable for audit, but it must not replace the
    newer OCR/import reason shown on the task and submission progress pages.
    """
    if run is None:
        return None
    # Normalized/legacy callers can create a grading run without a façade
    # pointer, so a null failure marker still allows the selected run's error.
    # A non-null marker naming another job is authoritative and must win.
    if (
        workflow.last_failed_job_id is not None
        and workflow.last_failed_job_id != run.id
    ):
        return None
    return _grading_failure_code(run)


def _serialize_problem(question) -> dict:
    presentation = dict((question.source or {}).get("presentation") or {})
    max_score = float(question.max_score)
    return {
        "q_id": question.q_id,
        "number": question.number,
        "type": question.type,
        "stem": question.stem,
        "criterion": question.criterion,
        "max_score": max_score,
        "max_score_source": presentation.get("max_score_source") or (
            "default_10" if max_score == 10 else "legacy"
        ),
        "max_score_review_status": presentation.get(
            "max_score_review_status", "needs_review"
        ),
        "review_status": presentation.get("review_status", "needs_review"),
        "reference_answer": question.reference_answer,
        "solution_code": presentation.get("solution_code"),
        "test_cases": question.test_cases,
        "material_provenance": presentation.get("material_provenance", {}),
        "ai_completion_provenance": presentation.get("ai_completion_provenance", {}),
        "preparation_issues": presentation.get("preparation_issues", []),
    }


def _serialize_student_data(task_id: str, owner_id: str, submissions) -> dict[str, dict]:
    presentations = workflow_repository.list_student_presentations(task_id)
    revisions = []
    for submission in submissions:
        if submission.current_revision_id:
            revisions.append((submission, submission_repository.get_revision(
                revision_id=submission.current_revision_id, actor_id=owner_id
            )))
    answer_ids = [answer.id for _, revision in revisions for answer in revision.answers]
    review_statuses = workflow_repository.answer_review_statuses(answer_ids)
    output: dict[str, dict] = {}
    for submission, revision in revisions:
        presentation = presentations.get(submission.student_id)
        display_id = presentation.display_student_id if presentation else submission.student_id
        output[display_id] = {
            "stu_id": display_id,
            "stu_name": presentation.display_name if presentation else submission.student_id,
            "stu_ans": [
                {
                    "q_id": answer.q_id,
                    "number": answer.number,
                    "type": answer.type,
                    "content": answer.content,
                    "flag": list(answer.flag or []),
                    "review_status": review_statuses.get(answer.id, "pending"),
                }
                for answer in revision.answers
            ],
            "source_filename": (
                presentation.source_filename if presentation else revision.file_name
            ),
            "identity_match_method": (
                presentation.identity_match_method if presentation else "filename"
            ),
            "identity_status": presentation.identity_status if presentation else "matched",
            "source_id": presentation.source_id if presentation else None,
        }
    return output


def _active_submissions(task_id: str, owner_id: str):
    submissions = submission_repository.list_submissions(task_id, actor_id=owner_id)
    presentations = workflow_repository.list_student_presentations(task_id)
    inactive_ids = {
        student_id
        for student_id, presentation in presentations.items()
        if not presentation.is_active
    }
    return [
        submission for submission in submissions
        if submission.student_id not in inactive_ids
    ]


def _selected_knowledge(task_id: str, owner_id: str) -> dict[str, dict]:
    from backend.db import course_library_repository
    from backend.db.knowledge_repository import (
        list_selected_documents,
        selected_document_metadata,
    )

    metadata = selected_document_metadata(task_id, owner_id)
    output: dict[str, dict] = {}
    for document in list_selected_documents(task_id, owner_id):
        attachment = metadata.get(document.id, {})
        material_id = attachment.get("library_material_id")
        if material_id is None:
            material = course_library_repository.get_material_by_document(
                document.id, owner_id
            )
            material_id = material.material_id if material is not None else None
        output[document.id] = {
            "doc_id": document.id,
            "filename": document.original_name,
            "chunk_count": document.chunk_count,
            "uploaded_at": document.created_at,
            "source_kind": attachment.get("source_kind") or "upload",
            "library_material_id": material_id,
            "saved_to_library": material_id is not None,
        }
    return output


def _is_system_course(course_id: str) -> bool:
    with session_scope() as session:
        code = session.scalar(select(CourseRecord.code).where(CourseRecord.id == course_id))
        return code == SYSTEM_COURSE_CODE


def _operation_state(operation) -> str:
    return (
        "already_running"
        if operation.status in {"preparing", "pending", "running"}
        else "already_done"
    )


def _operation_is_retryable(operation, *, now: float | None = None) -> bool:
    if operation.status == "error":
        return True
    current_time = time.time() if now is None else now
    return bool(
        operation.status in {"preparing", "pending", "running"}
        and operation.expires_at is not None
        and operation.expires_at <= current_time
    )


def retryable_operation_claim_revision(
    *, workflow, replay, requested_revision: int,
) -> int:
    """Resolve the CAS base for an exact retry without hiding real edits.

    Claiming an operation is itself a workflow mutation, so a failed/expired
    attempt leaves the task exactly one revision ahead of the base stored in
    that attempt.  An identical retry may consume that internal claim bump;
    any additional revision means some other task mutation occurred and the
    original client request must remain stale.
    """
    current_revision = workflow.workflow_revision
    if current_revision == requested_revision:
        return requested_revision
    if replay is None or not _operation_is_retryable(replay):
        _raise_stale_revision()
    payload = dict(replay.payload or {})
    previous_claim_base = payload.get("base_workflow_revision")
    if isinstance(previous_claim_base, bool) or not isinstance(previous_claim_base, int):
        _raise_stale_revision()
    same_operation_owns_delta = (
        workflow.last_failed_job_id == replay.id
        or workflow.active_job_id == replay.id
    )
    if (
        not same_operation_owns_delta
        or current_revision != previous_claim_base + 1
    ):
        _raise_stale_revision()
    return current_revision


def find_task_operation(
    *, task_id: str, owner_id: str, operation_type: str, input_hash: str,
):
    with session_scope() as session:
        row = session.scalar(select(workflow_repository.WorkflowOperationRecord).where(
            workflow_repository.WorkflowOperationRecord.assignment_id == task_id,
            workflow_repository.WorkflowOperationRecord.owner_id == owner_id,
            workflow_repository.WorkflowOperationRecord.operation_type == operation_type,
            workflow_repository.WorkflowOperationRecord.input_hash == input_hash,
        ))
        if row is not None:
            session.expunge(row)
        return row


def _cas_operation_attempt_for_write(
    session, *, task_id: str, owner_id: str, operation_id: str,
    expected_operation_attempt: int, expected_statuses: tuple[str, ...],
    expected_lease_token: str | None = None,
    changes: dict[str, Any] | None = None,
):
    """Lock one operation generation through an attempt-and-status CAS.

    The first write deliberately happens before related workflow/domain writes.
    A retry that increments ``attempt`` therefore cannot race an already
    validated worker and then be overwritten by that worker's ORM flush.
    """
    now = time.time()
    claimed = session.execute(
        update(workflow_repository.WorkflowOperationRecord)
        .where(
            workflow_repository.WorkflowOperationRecord.id == operation_id,
            workflow_repository.WorkflowOperationRecord.assignment_id == task_id,
            workflow_repository.WorkflowOperationRecord.owner_id == owner_id,
            workflow_repository.WorkflowOperationRecord.attempt
            == expected_operation_attempt,
            workflow_repository.WorkflowOperationRecord.status.in_(expected_statuses),
            workflow_repository._lease_write_predicate(expected_lease_token, now),
        )
        .values(**(changes or {}), updated_at=now)
    )
    if claimed.rowcount != 1:
        current = session.scalar(select(
            workflow_repository.WorkflowOperationRecord
        ).where(
            workflow_repository.WorkflowOperationRecord.id == operation_id,
            workflow_repository.WorkflowOperationRecord.owner_id == owner_id,
        ))
        if current is None or current.assignment_id != task_id:
            raise NotFound("workflow_operation")
        if current.attempt != expected_operation_attempt:
            raise VersionConflict(
                "A newer workflow operation attempt is active.",
                code="stale_operation_attempt",
            )
        if not workflow_repository._lease_allows_write(
            current, expected_lease_token, now
        ):
            from backend.domain.errors import LeaseLost

            raise LeaseLost(
                "The operation lease is held by another worker or expired.",
                code="lease_lost",
            )
        raise InvalidTransition(
            "The workflow job is not in the expected state.", code="workflow_busy"
        )
    operation = session.scalar(select(
        workflow_repository.WorkflowOperationRecord
    ).where(
        workflow_repository.WorkflowOperationRecord.id == operation_id,
        workflow_repository.WorkflowOperationRecord.owner_id == owner_id,
        workflow_repository.WorkflowOperationRecord.attempt
        == expected_operation_attempt,
    ))
    assert operation is not None
    return operation


def claim_workflow_operation_atomic(
    *, task_id: str, owner_id: str, operation_id: str,
    expected_operation_attempt: int, expected_workflow_revision: int,
    workflow_changes: dict[str, Any],
) -> int:
    """CAS the task revision and transition its durable job to running together."""
    now = time.time()
    allowed = {
        column.name
        for column in workflow_repository.AssignmentWorkflowRecord.__table__.columns
        if column.name not in {
            "assignment_id", "owner_id", "created_at", "updated_at",
            "workflow_revision",
        }
    }
    values = {
        key: value for key, value in workflow_changes.items() if key in allowed
    }
    with session_scope() as session:
        operation = _cas_operation_attempt_for_write(
            session, task_id=task_id, owner_id=owner_id,
            operation_id=operation_id,
            expected_operation_attempt=expected_operation_attempt,
            expected_statuses=("pending",),
            changes={"status": "running", "error_code": None},
        )
        if operation.operation_type in _AUXILIARY_QUESTION_OPERATION_TYPES:
            values["presentation_status"] = "problems_ready"
            values["error_code"] = None
        claimed = session.execute(
            update(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
                workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
                workflow_repository.AssignmentWorkflowRecord.workflow_revision
                == expected_workflow_revision,
            )
            .values(
                **values,
                workflow_revision=(
                    workflow_repository.AssignmentWorkflowRecord.workflow_revision + 1
                ),
                updated_at=now,
            )
        )
        if claimed.rowcount != 1:
            _raise_stale_revision()
        session.flush()
        return expected_workflow_revision + 1


def activate_workflow_operation_atomic(
    *, task_id: str, owner_id: str, operation_id: str,
    expected_operation_attempt: int, expected_workflow_revision: int,
    operation_payload: dict[str, Any], workflow_changes: dict[str, Any],
) -> int:
    """Publish a fully persisted operation to the durable worker queue."""
    validated_payload = workflow_repository._validate_json_object(
        operation_payload,
        field="payload",
        max_bytes=workflow_repository.MAX_OPERATION_PAYLOAD_BYTES,
    )
    now = time.time()
    allowed = {
        column.name
        for column in workflow_repository.AssignmentWorkflowRecord.__table__.columns
        if column.name not in {
            "assignment_id", "owner_id", "created_at", "updated_at",
            "workflow_revision",
        }
    }
    values = {
        key: value for key, value in workflow_changes.items() if key in allowed
    }
    with session_scope() as session:
        _cas_operation_attempt_for_write(
            session, task_id=task_id, owner_id=owner_id,
            operation_id=operation_id,
            expected_operation_attempt=expected_operation_attempt,
            expected_statuses=("preparing",),
            changes={
                "status": "pending", "payload": validated_payload,
                "error_code": None,
                "expires_at": now + _OPERATION_RUNTIME_TTL_SECONDS,
            },
        )
        claimed = session.execute(
            update(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
                workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
                workflow_repository.AssignmentWorkflowRecord.workflow_revision
                == expected_workflow_revision,
            )
            .values(
                **values,
                workflow_revision=(
                    workflow_repository.AssignmentWorkflowRecord.workflow_revision + 1
                ),
                updated_at=now,
            )
        )
        if claimed.rowcount != 1:
            _raise_stale_revision()
        return expected_workflow_revision + 1


def _detail_error(error: DomainError, fallback: str) -> str:
    for candidate in (error.code, error.message):
        if candidate in _SAFE_ERROR_CODES:
            return candidate
    return fallback


def _raise_stale_revision() -> None:
    raise VersionConflict("The task changed while this operation was running.", code="stale_revision")


def _raise_replacement_confirmation_required() -> None:
    raise InvalidTransition(
        "Existing confirmed data can only be replaced after explicit confirmation.",
        code="replacement_confirmation_required",
    )


def _ensure_no_other_active_operation(
    *, task_id: str, owner_id: str, operation_type: str, input_hash: str,
    allow_supersede: bool = False,
):
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    workflow = _reconcile_terminal_active_operation(
        task_id=task_id, owner_id=owner_id, workflow=workflow
    )
    if not workflow.active_job_id:
        return workflow, None
    if workflow.active_operation == "grading":
        # Reconciliation above leaves only a genuinely queued/running grading
        # run active.  It is not a workflow-operation row and must never be
        # looked up in ``workflow_operations``.
        if allow_supersede:
            grading_repository.cancel(
                workflow.active_job_id, teacher_id=owner_id
            )
            return workflow_repository.get_workflow(
                task_id, owner_id=owner_id
            ), None
        raise InvalidTransition("The task is busy.", code="workflow_busy")
    try:
        active = workflow_repository.get_operation(
            workflow.active_job_id, owner_id=owner_id
        )
    except NotFound:
        raise InvalidTransition("The task is busy.", code="workflow_busy")
    if active.status not in {"pending", "running"} or _operation_is_retryable(active):
        return workflow, None
    if active.operation_type == operation_type and active.input_hash == input_hash:
        return workflow, active
    if allow_supersede:
        released = workflow_repository.supersede_workflow_operation(
            task_id,
            owner_id=owner_id,
            operation_id=active.id,
            expected_attempt=active.attempt,
        )
        if released is not None:
            return released, None
    raise InvalidTransition("The task is busy.", code="workflow_busy")


def _has_draft_questions(task_id: str) -> bool:
    with session_scope() as session:
        return bool(session.scalar(
            select(AssignmentQuestionRecord.id)
            .where(AssignmentQuestionRecord.assignment_id == task_id)
            .limit(1)
        ))


def queue_task_problem_extraction(
    *, task_id: str, owner_id: str, filename: str, content: bytes,
    content_type: str | None, registry, input_hash: str | None = None,
    expected_workflow_revision: int | None = None,
    replace_confirmed: bool = False,
    extraction_options: dict[str, Any] | None = None,
) -> dict:
    """Persist and claim a problem-extraction job without doing OCR/LLM work."""
    assignment = assignment_repository.get_assignment(task_id, actor_id=owner_id)
    if assignment.status not in education.EDITABLE_ASSIGNMENT_STATUSES:
        raise InvalidTransition("assignment_not_editable")
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    base_revision = (
        workflow.workflow_revision
        if expected_workflow_revision is None
        else expected_workflow_revision
    )
    digest = _hash_json({
        "source": input_hash or hashlib.sha256(content).hexdigest(),
        "replace_confirmed": replace_confirmed,
        "extraction_options": extraction_options or {},
    })
    replay = find_task_operation(
        task_id=task_id, owner_id=owner_id,
        operation_type="problem_extraction", input_hash=digest,
    )
    if replay is not None and not _operation_is_retryable(replay):
        return {
            "status": _operation_state(replay), "task_id": task_id,
            "job_id": replay.id, "workflow_revision": workflow.workflow_revision,
        }
    claim_base_revision = retryable_operation_claim_revision(
        workflow=workflow, replay=replay, requested_revision=base_revision,
    )
    if _has_draft_questions(task_id) and not replace_confirmed:
        _raise_replacement_confirmation_required()
    if registry.pick_default() is None:
        raise ValidationError(
            "No enabled provider is available.", code="no_provider_configured"
        )
    workflow, active = _ensure_no_other_active_operation(
        task_id=task_id, owner_id=owner_id,
        operation_type="problem_extraction", input_hash=digest,
        allow_supersede=replace_confirmed,
    )
    if active is not None:
        return {
            "status": "already_running", "task_id": task_id,
            "job_id": active.id, "workflow_revision": workflow.workflow_revision,
        }
    operation, created = workflow_repository.create_operation(
        assignment_id=task_id,
        owner_id=owner_id,
        operation_type="problem_extraction",
        input_hash=digest,
        payload={},
        expires_at=time.time() + _OPERATION_PUBLICATION_TTL_SECONDS,
        initial_status="preparing",
    )
    if not created:
        return {
            "status": _operation_state(operation), "task_id": task_id,
            "job_id": operation.id, "workflow_revision": workflow.workflow_revision,
        }
    remove_reporter(operation.id)
    try:
        source_sha256 = hashlib.sha256(content).hexdigest()
        stored = next((
            item for item in file_repository.list_files(
                owner_id=owner_id, assignment_id=task_id
            )
            if item.kind == "problem_source" and item.sha256 == source_sha256
        ), None)
        if stored is None:
            stored = file_repository.save_file(
                storage=get_storage(), owner_id=owner_id,
                kind="problem_source", original_name=filename, content=content,
                content_type=content_type or "application/octet-stream",
                assignment_id=task_id,
            )
        source, _ = source_outcome_repository.register_source(
            owner_id=owner_id, assignment_id=task_id,
            operation_id=operation.id, expected_attempt=operation.attempt,
            order_index=0, stored_file_id=stored.id,
        )
        claimed_revision = activate_workflow_operation_atomic(
            task_id=task_id, owner_id=owner_id, operation_id=operation.id,
            expected_operation_attempt=operation.attempt,
            expected_workflow_revision=claim_base_revision,
            operation_payload={
                "source_id": source.id,
                "base_workflow_revision": claim_base_revision,
                "replace_confirmed": replace_confirmed,
                "extraction_options": extraction_options or {},
            },
            workflow_changes={
                "presentation_status": "extracting_problems",
                "active_operation": "problem_extraction",
                "active_job_id": operation.id, "extract_job_id": operation.id,
                "problem_file_name": filename, "error_code": None,
            },
        )
    except VersionConflict:
        workflow_repository.update_operation(
            operation.id, owner_id=owner_id,
            expected_attempt=operation.attempt, status="error",
            error_code="stale_revision", completed_at=time.time(),
        )
        _raise_stale_revision()
    except Exception:
        workflow_repository.update_operation(
            operation.id, owner_id=owner_id,
            expected_attempt=operation.attempt, status="error",
            error_code="problem_extraction_failed", completed_at=time.time(),
        )
        raise
    return {
        "status": "started", "task_id": task_id, "job_id": operation.id,
        "workflow_revision": claimed_revision,
        "_job_attempt": operation.attempt,
    }


async def run_task_problem_extraction(
    *, task_id: str, owner_id: str, job_id: str, filename: str,
    content: bytes, registry, job_attempt: int,
    claimed_workflow_revision: int,
    replace_confirmed: bool, extraction_options: dict[str, Any] | None = None,
) -> None:
    """Run a previously claimed extraction job and durably record its outcome."""
    try:
        provider = registry.pick_default()
        if provider is None:
            raise ValidationError(
                "No enabled provider is available.", code="no_provider_configured"
            )
        vision = registry.pick_vision(provider)
        ocr_skill = LLMVisionOCRSkill(vision) if vision is not None else None
        reporter = get_or_create_reporter(job_id)
        await reporter.configure_workflow(
            "problem_recognition",
            ("reading_source", "recognizing_structure", "validating_questions", "completed"),
        )
        await reporter.set_phase("extracting")
        await reporter.set_stage_progress(
            "reading_source", total_steps=4, completed_steps=0,
            message="Reading problem source.",
        )
        text = await extract_text_from_upload(
            content, filename, ocr_skill=ocr_skill, purpose="problems", reporter=reporter
        )
        problem_data: dict[str, dict] = {}
        await extract_problems(
            text, provider, problem_data, reporter=reporter,
            structure_mode=str((extraction_options or {}).get("structure_mode") or "organized"),
            extraction_hint=str((extraction_options or {}).get("extraction_hint") or ""),
            confirmed_candidates=list((extraction_options or {}).get("confirmed_candidates") or []),
            manage_progress_lifecycle=False,
        )
        await reporter.set_stage_progress(
            "validating_questions", total_steps=4, completed_steps=3,
            message="Validating recognized questions.",
        )
        await reporter.set_stage_progress(
            "completed", total_steps=4, completed_steps=4,
            message="Problem recognition completed.",
        )
        await reporter.set_phase("done")
        snapshot = (await reporter.snapshot()).model_dump(mode="json")
        _replace_draft_questions(
            task_id, owner_id, problem_data, filename,
            expected_workflow_revision=claimed_workflow_revision,
            replace_confirmed=replace_confirmed,
            operation_id=job_id,
            expected_operation_attempt=job_attempt,
            operation_progress=snapshot,
        )
    except Exception as exc:
        code = classify_background_error(exc, "problem_extraction_failed")
        logger.warning(
            "Background problem extraction failed; job_id=%s code=%s exception_type=%s",
            job_id,
            code,
            type(exc).__name__,
        )
        _fail_operation(
            task_id, owner_id, job_id, job_attempt, code
        )


def _registry_for_owner(owner_id: str):
    from backend.llm.registry import _build_scoped_registry

    with session_scope() as session:
        record = session.get(UserRecord, owner_id)
        if record is None:
            raise NotFound("workflow_operation_owner")
        user = User(
            id=record.id,
            username=record.username,
            email=record.email or "",
            role=record.role,
            password_hash=record.password_hash,
            created_at=record.created_at,
            is_active=record.is_active,
        )
    return _build_scoped_registry(user)


async def run_durable_problem_extraction(operation) -> None:
    from backend.services.problem_extraction import run_problem_extraction

    try:
        await run_problem_extraction(
            operation,
            registry_factory=_registry_for_owner,
            extract_text=extract_text_from_upload,
            extract_questions=extract_problems,
            commit=_replace_draft_questions,
        )
    except Exception as exc:
        from backend.domain.errors import LeaseLost

        if isinstance(exc, LeaseLost):
            raise
        code = (
            _detail_error(exc, "problem_extraction_failed")
            if isinstance(exc, DomainError)
            else "problem_extraction_failed"
        )
        _fail_operation(
            operation.assignment_id,
            operation.owner_id,
            operation.operation_id,
            operation.attempt,
            code,
            expected_lease_token=operation.lease_token,
        )


def _replace_draft_questions(
    task_id: str, owner_id: str, problem_data: dict[str, dict], filename: str,
    *, expected_workflow_revision: int | None = None,
    replace_confirmed: bool = False, operation_id: str | None = None,
    expected_operation_attempt: int | None = None,
    expected_lease_token: str | None = None,
    operation_progress: dict | None = None,
    operation_checkpoint: dict | None = None,
    operation_artifact_refs: list[str] | None = None,
) -> int:
    """Atomically CAS the workflow and replace the complete draft question set."""
    now = time.time()
    validated_progress = workflow_repository._validate_json_object(
        operation_progress or {},
        field="progress",
        max_bytes=workflow_repository.MAX_OPERATION_PROGRESS_BYTES,
    )
    validated_checkpoint = workflow_repository._validate_json_object(
        operation_checkpoint or {},
        field="checkpoint",
        max_bytes=workflow_repository.MAX_OPERATION_CHECKPOINT_BYTES,
    )
    validated_refs = workflow_repository._validate_artifact_refs(
        operation_artifact_refs or []
    )
    terminal_summary = workflow_repository._validate_json_object(
        {"problem_count": len(problem_data)},
        field="terminal_summary",
        max_bytes=workflow_repository.MAX_OPERATION_TERMINAL_SUMMARY_BYTES,
    )
    with session_scope() as session:
        operation = None
        if operation_id is not None:
            if expected_operation_attempt is None:
                raise ValidationError("operation_attempt_required")
            operation = _cas_operation_attempt_for_write(
                session, task_id=task_id, owner_id=owner_id,
                operation_id=operation_id,
                expected_operation_attempt=expected_operation_attempt,
                expected_statuses=("running",),
                expected_lease_token=expected_lease_token,
            )
            if validated_refs:
                matched_refs = set(session.scalars(select(
                    file_repository.StoredFileRecord.id
                ).where(
                    file_repository.StoredFileRecord.id.in_(validated_refs),
                    file_repository.StoredFileRecord.owner_id == owner_id,
                    file_repository.StoredFileRecord.assignment_id == task_id,
                )))
                if matched_refs != set(validated_refs):
                    raise NotFound("stored_file")
        allowed_statuses = list(education.EDITABLE_ASSIGNMENT_STATUSES)
        if replace_confirmed:
            allowed_statuses.append(education.AssignmentStatus.PUBLISHED.value)
        assignment = session.scalar(select(AssignmentRecord).where(
            AssignmentRecord.id == task_id,
            AssignmentRecord.teacher_id == owner_id,
            AssignmentRecord.status.in_(allowed_statuses),
        ))
        if assignment is None:
            raise InvalidTransition("assignment_not_editable")
        existing = session.scalars(select(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == task_id
        )).all()
        if existing and not replace_confirmed:
            _raise_replacement_confirmation_required()
        workflow = session.scalar(select(workflow_repository.AssignmentWorkflowRecord).where(
            workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
            workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
        ))
        if workflow is None:
            raise NotFound("workflow")
        expected = (
            workflow.workflow_revision
            if expected_workflow_revision is None
            else expected_workflow_revision
        )
        result = session.execute(
            update(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
                workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
                workflow_repository.AssignmentWorkflowRecord.workflow_revision == expected,
            )
            .values(
                workflow_revision=workflow_repository.AssignmentWorkflowRecord.workflow_revision + 1,
                presentation_status="problems_ready", active_operation=None,
                active_job_id=None, problem_file_name=filename,
                parse_job_id=None, grading_job_id=None,
                last_failed_job_id=None,
                submission_file_name=None,
                pending_submission_file_name=None,
                submission_roster_name=None,
                submission_recognition_provider_id=None,
                reference_file_name=None,
                test_cases_file_name=None,
                analysis_status="not_generated",
                analysis_result_version=None,
                analysis_generated_at=None,
                analysis_error_code=None,
                error_code=None, updated_at=now,
            )
        )
        if result.rowcount != 1:
            _raise_stale_revision()
        session.execute(delete(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == task_id
        ))
        # A new question set invalidates every recognized submission and every
        # result derived from it.  Deactivate the old student presentation so
        # no stale submission can remain in the current task generation.
        session.execute(
            update(workflow_repository.AssignmentStudentPresentationRecord)
            .where(
                workflow_repository.AssignmentStudentPresentationRecord.assignment_id
                == task_id
            )
            .values(is_active=False, updated_at=now)
        )
        for index, (q_id, raw) in enumerate(problem_data.items()):
            source = {
                "origin": "figma_task_facade",
                "filename": filename,
                "presentation": {
                    "review_status": raw.get("review_status", "needs_review"),
                    "max_score_source": raw.get("max_score_source") or (
                        "default_10"
                        if float(raw.get("max_score") or 10) == 10
                        else "legacy"
                    ),
                    "max_score_review_status": raw.get(
                        "max_score_review_status", "needs_review"
                    ),
                    "solution_code": raw.get("solution_code"),
                    "material_provenance": raw.get("material_provenance", {}),
                    "ai_completion_provenance": raw.get("ai_completion_provenance", {}),
                    "preparation_issues": raw.get("preparation_issues", []),
                },
            }
            session.add(AssignmentQuestionRecord(
                id=f"q_{uuid.uuid4().hex[:12]}", assignment_id=task_id,
                q_id=str(raw.get("q_id") or q_id), order_index=index,
                number=str(raw.get("number") or index + 1),
                type=str(raw.get("type") or "其他"),
                stem=str(raw.get("stem") or ""),
                criterion=str(raw.get("criterion") or ""),
                max_score=float(raw.get("max_score") or 10),
                reference_answer=raw.get("reference_answer"),
                test_cases=raw.get("test_cases"), source=source,
                version=1, created_at=now, updated_at=now,
            ))
        assignment.status = education.AssignmentStatus.READY.value
        assignment.published_at = None
        assignment.version += 1
        assignment.updated_at = now
        if operation is not None:
            payload = dict(operation.payload or {})
            payload.update({"filename": filename, "problem_count": len(problem_data)})
            operation.status = "done"
            operation.progress = validated_progress
            operation.payload = payload
            operation.error_code = None
            operation.completed_at = now
            operation.updated_at = now
            operation.checkpoint_revision += 1
            operation.checkpoint_stage = "completed"
            operation.checkpoint = validated_checkpoint
            operation.artifact_refs = validated_refs
            operation.terminal_summary = terminal_summary
            operation.lease_owner = None
            operation.lease_token = None
            operation.lease_expires_at = None
            operation.lease_heartbeat_at = None
        return expected + 1


def queue_task_submission_parsing(
    *, task_id: str, owner_id: str, filename: str, content: bytes,
    content_type: str | None, registry, identity_mode: str = "filename",
    roster_entries: list[dict[str, str]] | None = None,
    roster_name: str | None = None, recognition_provider_id: str | None = None,
    replace_confirmed: bool = False,
) -> dict:
    assignment = assignment_repository.get_assignment(task_id, actor_id=owner_id)
    questions = assignment_repository.list_questions(task_id, teacher_id=owner_id)
    if not questions:
        raise InvalidTransition("problems_required")
    if _active_submissions(task_id, owner_id) and not replace_confirmed:
        _raise_replacement_confirmation_required()
    if recognition_provider_id:
        enabled_ids = {
            str(item.get("provider_id"))
            for item in registry.list_configs()
            if item.get("enabled")
        }
        if recognition_provider_id not in enabled_ids:
            raise ValidationError(
                "The selected recognition provider is not enabled.",
                code="recognition_provider_not_enabled",
            )
    provider = (
        registry.get(recognition_provider_id)
        if recognition_provider_id else registry.pick_default()
    )
    if provider is None:
        code = (
            "recognition_provider_not_enabled"
            if recognition_provider_id else "no_provider_configured"
        )
        raise ValidationError("No enabled recognition provider is available.", code=code)
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    digest = _hash_json({
        "sha256": hashlib.sha256(content).hexdigest(),
        "identity_mode": identity_mode,
        "roster": roster_entries or [],
        "provider": recognition_provider_id,
        "replace_confirmed": replace_confirmed,
    })
    workflow, active = _ensure_no_other_active_operation(
        task_id=task_id, owner_id=owner_id,
        operation_type="submission_recognition", input_hash=digest,
        allow_supersede=replace_confirmed,
    )
    if active is not None:
        return {
            "status": "already_running", "task_id": task_id,
            "job_id": active.id, "workflow_revision": workflow.workflow_revision,
        }
    operation, created = workflow_repository.create_operation(
        assignment_id=task_id, owner_id=owner_id,
        operation_type="submission_recognition", input_hash=digest,
        payload={}, expires_at=time.time() + _OPERATION_PUBLICATION_TTL_SECONDS,
        initial_status="preparing",
    )
    if not created:
        return {
            "status": _operation_state(operation), "task_id": task_id,
            "job_id": operation.id, "workflow_revision": workflow.workflow_revision,
        }
    remove_reporter(operation.id)
    workflow_changes: dict[str, Any] = {
        "presentation_status": "parsing_submissions",
        "active_operation": "submission_recognition",
        "active_job_id": operation.id,
        "parse_job_id": operation.id,
        "pending_submission_file_name": filename,
        "submission_identity_mode": identity_mode,
        "submission_roster_name": roster_name,
        "submission_recognition_provider_id": recognition_provider_id,
        "error_code": None,
    }
    if replace_confirmed:
        # The teacher explicitly accepted downstream invalidation. Detach the
        # previous grading generation at claim time so a failed recognition
        # attempt cannot surface the obsolete grading error or result again.
        workflow_changes.update({
            "grading_job_id": None,
            "last_failed_job_id": None,
            "analysis_status": "not_generated",
            "analysis_result_version": None,
            "analysis_generated_at": None,
            "analysis_error_code": None,
        })
    try:
        is_archive = (filename or "").lower().endswith(ARCHIVE_EXTENSIONS)
        source_kind = "submission_container" if is_archive else "submission_source"
        stored = next((
            item for item in file_repository.list_files(
                owner_id=owner_id, assignment_id=task_id
            )
            if item.kind == source_kind
            and item.sha256 == hashlib.sha256(content).hexdigest()
        ), None)
        if stored is None:
            stored = file_repository.save_file(
                storage=get_storage(), owner_id=owner_id,
                kind=source_kind, original_name=filename,
                content=content,
                content_type=infer_upload_content_type(
                    filename, content_type, content
                ),
                assignment_id=task_id,
            )
        source_ids: list[str] = []
        if not is_archive:
            source, _ = source_outcome_repository.register_source(
                owner_id=owner_id, assignment_id=task_id,
                operation_id=operation.id, expected_attempt=operation.attempt,
                order_index=0, stored_file_id=stored.id,
            )
            source_ids.append(source.id)
        workflow_repository.save_operation_checkpoint(
            operation.id,
            owner_id=owner_id,
            expected_attempt=operation.attempt,
            expected_checkpoint_revision=operation.checkpoint_revision,
            stage="submission_source_saved",
            checkpoint={"input_file_id": stored.id},
            artifact_refs=[stored.id],
        )
        claimed_revision = activate_workflow_operation_atomic(
            task_id=task_id, owner_id=owner_id, operation_id=operation.id,
            expected_operation_attempt=operation.attempt,
            expected_workflow_revision=workflow.workflow_revision,
            operation_payload={
                "input_file_id": stored.id,
                "source_ids": source_ids,
                "base_workflow_revision": workflow.workflow_revision,
                "identity_mode": identity_mode,
                "roster_entries": roster_entries or [],
                "roster_name": roster_name,
                "recognition_provider_id": recognition_provider_id,
                "replace_confirmed": replace_confirmed,
            },
            workflow_changes=workflow_changes,
        )
    except VersionConflict:
        workflow_repository.update_operation(
            operation.id, owner_id=owner_id,
            expected_attempt=operation.attempt, status="error",
            error_code="stale_revision", completed_at=time.time(),
        )
        _raise_stale_revision()
    except Exception:
        workflow_repository.update_operation(
            operation.id, owner_id=owner_id,
            expected_attempt=operation.attempt, status="error",
            error_code="submission_parse_failed", completed_at=time.time(),
        )
        raise
    return {
        "status": "started", "task_id": task_id, "job_id": operation.id,
        "workflow_revision": claimed_revision,
        "_job_attempt": operation.attempt,
    }


def _submission_result_artifact(
    *, owner_id: str, task_id: str, job_id: str, job_attempt: int,
):
    expected_name = f"{job_id}-attempt-{job_attempt}-parsed.json"
    return next((
        item for item in file_repository.list_files(
            owner_id=owner_id, assignment_id=task_id
        )
        if item.kind == "submission_recognition_result"
        and item.original_name == expected_name
    ), None)


def _serialize_submission_results(
    results: list[SubmissionSourceParseResult],
) -> bytes:
    return json.dumps([
        {
            "source_id": result.source_id,
            "stored_file_id": result.stored_file_id,
            "filename": result.filename,
            "status": result.status,
            "student": result.student,
            "student_candidate": result.student_candidate,
            "matched_answer_count": result.matched_answer_count,
            "unknown_question_ids": list(result.unknown_question_ids),
            "stable_error_code": result.stable_error_code,
            "failure_phase": result.failure_phase,
            "retryable": result.retryable,
        }
        for result in results
    ], ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _load_submission_results(artifact) -> list[SubmissionSourceParseResult]:
    with get_storage().open(artifact.storage_key) as stream:
        payload = json.loads(stream.read().decode("utf-8"))
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("submission_parse_invalid")
    return [SubmissionSourceParseResult(
        source_id=str(item["source_id"]),
        stored_file_id=str(item["stored_file_id"]),
        filename=str(item["filename"]),
        status=item["status"],
        student=item.get("student"),
        student_candidate=item.get("student_candidate"),
        matched_answer_count=int(item.get("matched_answer_count") or 0),
        unknown_question_ids=tuple(item.get("unknown_question_ids") or []),
        stable_error_code=item.get("stable_error_code"),
        failure_phase=item.get("failure_phase"),
        retryable=bool(item.get("retryable")),
    ) for item in payload if isinstance(item, dict)]


async def run_task_submission_parsing(
    *, task_id: str, owner_id: str, job_id: str, filename: str,
    content: bytes, content_type: str | None, registry, job_attempt: int,
    identity_mode: str,
    roster_entries: list[dict[str, str]] | None, recognition_provider_id: str | None,
    replace_confirmed: bool, claimed_workflow_revision: int,
    leased_operation=None,
) -> None:
    reporter = get_or_create_reporter(job_id)
    current_failure_phase = "source_persistence"
    parsed_artifact = None
    try:
        if leased_operation is None:
            current_operation = workflow_repository.get_operation(
                job_id, owner_id=owner_id
            )
            if current_operation.status == "pending":
                workflow_repository.update_operation(
                    job_id,
                    owner_id=owner_id,
                    expected_attempt=job_attempt,
                    status="running",
                    started_at=time.time(),
                )
        assignment = assignment_repository.get_assignment(task_id, actor_id=owner_id)
        questions = assignment_repository.list_questions(task_id, teacher_id=owner_id)
        results = None
        if leased_operation is not None:
            parsed_artifact = _submission_result_artifact(
                owner_id=owner_id,
                task_id=task_id,
                job_id=job_id,
                job_attempt=job_attempt,
            )
            if parsed_artifact is not None:
                results = _load_submission_results(parsed_artifact)
        if results is None:
            provider = (
                registry.get(recognition_provider_id)
                if recognition_provider_id else registry.pick_default()
            )
            if provider is None:
                raise ValidationError(
                    "No enabled recognition provider is available.",
                    code=(
                        "recognition_provider_not_enabled"
                        if recognition_provider_id else "no_provider_configured"
                    ),
                )
            pick_vision = getattr(registry, "pick_vision", None)
            vision = pick_vision(provider) if pick_vision is not None else None
            ocr_skill = LLMVisionOCRSkill(vision) if vision is not None else None
            sources = await prepare_submission_sources(
                content=content,
                filename=filename,
                content_type=content_type,
                owner_id=owner_id,
                task_id=task_id,
                job_id=job_id,
                job_attempt=job_attempt,
                ocr_skill=ocr_skill,
                reporter=reporter,
            )
            current_failure_phase = "recognition"
            results = await parse_student_answer_sources(
                sources,
                {q.q_id: _serialize_problem(q) for q in questions},
                provider,
                reporter=reporter,
                identity_mode=identity_mode,
                roster_entries=roster_entries,
            )
            if leased_operation is not None:
                current_failure_phase = "outcome_persistence"
                parsed_artifact = file_repository.save_file(
                    storage=get_storage(),
                    owner_id=owner_id,
                    kind="submission_recognition_result",
                    original_name=(
                        f"{job_id}-attempt-{job_attempt}-parsed.json"
                    ),
                    content=_serialize_submission_results(results),
                    content_type="application/json",
                    assignment_id=task_id,
                )
                await leased_operation.checkpoint(
                    stage="submissions_parsed",
                    checkpoint={"parsed_artifact_id": parsed_artifact.id},
                    artifact_refs=list(dict.fromkeys([
                        *leased_operation.artifact_refs,
                        parsed_artifact.id,
                    ])),
                )

        current_failure_phase = "outcome_persistence"
        for result in results:
            source_outcome_repository.record_outcome(
                source_id=result.source_id,
                owner_id=owner_id,
                status=result.status,
                student_candidate=result.student_candidate,
                matched_answer_count=result.matched_answer_count,
                unknown_question_ids=list(result.unknown_question_ids),
                stable_error_code=result.stable_error_code,
                failure_phase=result.failure_phase,
                retryable=result.retryable,
                artifact_file_id=(
                    parsed_artifact.id if parsed_artifact is not None else None
                ),
            )

        current_failure_phase = "result_persistence"
        summary = source_outcome_repository.summarize_sources(
            operation_id=job_id,
            owner_id=owner_id,
            attempt=job_attempt,
        )
        students = [
            result.student
            for result in results
            if result.student is not None
            and result.status in {"parsed", "identity_conflict"}
        ]
        if not students:
            code = next(
                (
                    result.stable_error_code
                    for result in results
                    if result.stable_error_code
                ),
                "submission_parse_failed",
            )
            safe_code = safe_background_error_code(code, "submission_parse_failed")
            await reporter.set_error(safe_code)
            snapshot = (await reporter.snapshot()).model_dump(mode="json")
            _fail_operation(
                task_id,
                owner_id,
                job_id,
                job_attempt,
                safe_code,
                expected_lease_token=(
                    leased_operation.lease_token
                    if leased_operation is not None else None
                ),
                operation_progress=snapshot,
            )
            return

        await reporter.set_phase("done")
        snapshot = (await reporter.snapshot()).model_dump(mode="json")
        _commit_imported_submissions(
            task_id=task_id,
            owner_id=owner_id,
            course_id=assignment.course_id,
            students=students,
            replace_existing=replace_confirmed,
            expected_workflow_revision=claimed_workflow_revision,
            operation_id=job_id,
            expected_operation_attempt=job_attempt,
            expected_lease_token=(
                leased_operation.lease_token
                if leased_operation is not None else None
            ),
            operation_progress=snapshot,
            submission_file_name=filename,
            source_summary={
                "uploaded": summary.uploaded_count,
                "parsed": summary.success_count,
                "failed": summary.failed_count,
                "identity_needs_review": summary.conflict_count,
                "pending": summary.pending_count,
            },
        )
    except Exception as exc:
        if isinstance(exc, LeaseLost):
            raise
        persistence_code = {
            "source_persistence": "submission_source_persistence_failed",
            "outcome_persistence": "submission_outcome_persistence_failed",
            "result_persistence": "submission_persistence_failed",
        }.get(current_failure_phase)
        fallback_code = persistence_code or "submission_parse_failed"
        code = classify_background_error(
            exc,
            fallback_code,
            persistence_code=persistence_code,
        )
        logger.warning(
            "Background submission parsing failed; job_id=%s code=%s exception_type=%s",
            job_id,
            code,
            type(exc).__name__,
        )
        terminal_reason, terminal_phase = safe_source_diagnostic(
            "parse_failed",
            code,
            current_failure_phase,
        )
        assert terminal_reason is not None and terminal_phase is not None
        try:
            source_outcome_repository.finalize_pending_sources_as_failed(
                owner_id=owner_id,
                assignment_id=task_id,
                operation_id=job_id,
                expected_attempt=job_attempt,
                reason_code=terminal_reason,
                failure_phase=terminal_phase,
                retryable=is_retryable_background_error(terminal_reason),
            )
        except Exception as cleanup_exc:
            logger.warning(
                "Pending submission source finalization failed; job_id=%s "
                "exception_type=%s",
                job_id,
                type(cleanup_exc).__name__,
            )
        await reporter.set_error(code)
        snapshot = (await reporter.snapshot()).model_dump(mode="json")
        _fail_operation(
            task_id,
            owner_id,
            job_id,
            job_attempt,
            code,
            expected_lease_token=(
                leased_operation.lease_token
                if leased_operation is not None else None
            ),
            operation_progress=snapshot,
        )


async def run_durable_submission_recognition(operation) -> None:
    """Recover one persisted submission archive and commit it under its lease."""
    try:
        input_file_id = str(
            (operation.payload or {}).get("input_file_id") or ""
        )
        input_file = file_repository.get_file(
            file_id=input_file_id, owner_id=operation.owner_id
        )
        if input_file is None or input_file.assignment_id != operation.assignment_id:
            raise NotFound("stored_file")
        with get_storage().open(input_file.storage_key) as stream:
            content = stream.read()
        await run_task_submission_parsing(
            task_id=operation.assignment_id,
            owner_id=operation.owner_id,
            job_id=operation.operation_id,
            filename=input_file.original_name,
            content=content,
            content_type=input_file.content_type,
            registry=_registry_for_owner(operation.owner_id),
            job_attempt=operation.attempt,
            identity_mode=str(
                (operation.payload or {}).get("identity_mode") or "filename"
            ),
            roster_entries=list(
                (operation.payload or {}).get("roster_entries") or []
            ),
            recognition_provider_id=(
                operation.payload or {}
            ).get("recognition_provider_id"),
            replace_confirmed=bool(
                (operation.payload or {}).get("replace_confirmed")
            ),
            claimed_workflow_revision=(
                int((operation.payload or {}).get("base_workflow_revision") or 0)
                + 1
            ),
            leased_operation=operation,
        )
    except Exception as exc:
        if isinstance(exc, LeaseLost):
            raise
        code = (
            _detail_error(exc, "submission_parse_failed")
            if isinstance(exc, DomainError)
            else "submission_parse_failed"
        )
        _fail_operation(
            operation.assignment_id, operation.owner_id, operation.operation_id,
            operation.attempt, code, expected_lease_token=operation.lease_token,
        )


def apply_question_patches_atomic(
    *, task_id: str, owner_id: str, expected_workflow_revision: int,
    patches: list[dict[str, Any]], operation_id: str,
    expected_operation_attempt: int,
    expected_lease_token: str | None = None,
    required_operation_status: str, final_operation_status: str,
    operation_payload: dict[str, Any], operation_progress: dict | None = None,
    require_missing: bool = False,
) -> int:
    """Apply a generated/imported question batch and finish its job atomically.

    Every target and the workflow revision is validated before the first row is
    changed.  Any invalid target, stale revision, expired job, or database error
    rolls back the workflow claim, question changes, and operation transition.
    """
    validated_payload = workflow_repository._validate_json_object(
        operation_payload,
        field="payload",
        max_bytes=workflow_repository.MAX_OPERATION_PAYLOAD_BYTES,
    )
    validated_progress = (
        workflow_repository._validate_json_object(
            operation_progress,
            field="progress",
            max_bytes=workflow_repository.MAX_OPERATION_PROGRESS_BYTES,
        )
        if operation_progress is not None else None
    )
    now = time.time()
    allowed_fields = {
        "stem", "criterion", "max_score", "reference_answer", "test_cases"
    }
    allowed_presentation = {
        "review_status", "solution_code", "material_provenance",
        "ai_completion_provenance", "preparation_issues",
        "max_score_source", "max_score_review_status",
    }
    with session_scope() as session:
        assignment = session.scalar(select(AssignmentRecord).where(
            AssignmentRecord.id == task_id,
            AssignmentRecord.teacher_id == owner_id,
        ))
        if assignment is None:
            raise NotFound("assignment")
        if assignment.status not in education.EDITABLE_ASSIGNMENT_STATUSES:
            raise InvalidTransition("assignment_not_editable")

        operation = _cas_operation_attempt_for_write(
            session, task_id=task_id, owner_id=owner_id,
            operation_id=operation_id,
            expected_operation_attempt=expected_operation_attempt,
            expected_statuses=(required_operation_status,),
            expected_lease_token=expected_lease_token,
        )
        if operation.expires_at is not None and operation.expires_at <= now:
            raise InvalidTransition("The workflow job expired.", code="stale_revision")

        questions = session.scalars(select(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == task_id
        )).all()
        question_map = {question.q_id: question for question in questions}
        normalized: list[tuple[AssignmentQuestionRecord, dict, dict]] = []
        for patch in patches:
            q_id = str(patch.get("q_id") or "")
            question = question_map.get(q_id)
            if question is None:
                raise ValidationError(
                    "A generated target no longer matches a question.",
                    code="unknown_ai_completion_target" if require_missing else "stale_revision",
                )
            fields = dict(patch.get("fields") or {})
            presentation_updates = dict(patch.get("presentation") or {})
            if not set(fields).issubset(allowed_fields) or not set(
                presentation_updates
            ).issubset(allowed_presentation):
                raise ValidationError("Unsupported question patch.")
            if "max_score" in fields:
                max_score = float(fields["max_score"])
                if not math.isfinite(max_score) or not 0 < max_score <= 10_000:
                    raise ValidationError(
                        "Question maximum score must be between 0 and 10000.",
                        code="invalid_max_score",
                    )
                fields["max_score"] = max_score
            if require_missing:
                current_presentation = dict(
                    (question.source or {}).get("presentation") or {}
                )
                for key in fields:
                    if getattr(question, key) not in (None, "", []):
                        raise InvalidTransition(
                            "A requested completion target is no longer missing.",
                            code="unknown_ai_completion_target",
                        )
                if (
                    "solution_code" in presentation_updates
                    and current_presentation.get("solution_code")
                ):
                    raise InvalidTransition(
                        "A requested completion target is no longer missing.",
                        code="unknown_ai_completion_target",
                    )
            normalized.append((question, fields, presentation_updates))

        workflow = session.scalar(select(workflow_repository.AssignmentWorkflowRecord).where(
            workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
            workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
        ))
        if workflow is None:
            raise NotFound("workflow")
        workflow_values: dict[str, Any] = {
            "workflow_revision": (
                workflow_repository.AssignmentWorkflowRecord.workflow_revision + 1
            ),
            "error_code": None,
            "updated_at": now,
        }
        if operation.operation_type in _AUXILIARY_QUESTION_OPERATION_TYPES:
            workflow_values["presentation_status"] = "problems_ready"
        if workflow.active_job_id == operation_id:
            workflow_values.update(active_job_id=None, active_operation=None)
        claimed = session.execute(
            update(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
                workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
                workflow_repository.AssignmentWorkflowRecord.workflow_revision
                == expected_workflow_revision,
            )
            .values(**workflow_values)
        )
        if claimed.rowcount != 1:
            _raise_stale_revision()

        for question, fields, presentation_updates in normalized:
            source = dict(question.source or {})
            presentation = dict(source.get("presentation") or {})
            for key, value in presentation_updates.items():
                if key in {"material_provenance", "ai_completion_provenance"}:
                    merged = dict(presentation.get(key) or {})
                    merged.update(dict(value or {}))
                    presentation[key] = merged
                else:
                    presentation[key] = value
            source["presentation"] = presentation
            for key, value in fields.items():
                setattr(question, key, value)
            question.source = source
            question.version += 1
            question.updated_at = now

        operation.status = final_operation_status
        operation.payload = validated_payload
        operation.progress = (
            validated_progress
            if validated_progress is not None
            else workflow_repository._validate_json_object(
                dict(operation.progress or {}),
                field="progress",
                max_bytes=workflow_repository.MAX_OPERATION_PROGRESS_BYTES,
            )
        )
        operation.error_code = None
        operation.completed_at = now
        operation.updated_at = now
        operation.lease_owner = None
        operation.lease_token = None
        operation.lease_expires_at = None
        operation.lease_heartbeat_at = None
        session.flush()
        return expected_workflow_revision + 1


def complete_planning_operation_atomic(
    *, task_id: str, owner_id: str, expected_workflow_revision: int,
    operation_id: str, expected_operation_attempt: int,
    expected_lease_token: str | None = None,
    payload: dict[str, Any], progress: dict | None,
    final_status: str = "ready",
) -> int:
    """Publish a background-generated plan only if its task snapshot is current."""
    validated_payload = workflow_repository._validate_json_object(
        payload,
        field="payload",
        max_bytes=workflow_repository.MAX_OPERATION_PAYLOAD_BYTES,
    )
    validated_progress = workflow_repository._validate_json_object(
        progress or {},
        field="progress",
        max_bytes=workflow_repository.MAX_OPERATION_PROGRESS_BYTES,
    )
    now = time.time()
    with session_scope() as session:
        operation = _cas_operation_attempt_for_write(
            session, task_id=task_id, owner_id=owner_id,
            operation_id=operation_id,
            expected_operation_attempt=expected_operation_attempt,
            expected_statuses=("running",),
            expected_lease_token=expected_lease_token,
        )
        if operation.expires_at is not None and operation.expires_at <= now:
            _raise_stale_revision()
        claimed = session.execute(
            update(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
                workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
                workflow_repository.AssignmentWorkflowRecord.workflow_revision
                == expected_workflow_revision,
                workflow_repository.AssignmentWorkflowRecord.active_job_id == operation_id,
            )
            .values(
                active_job_id=None, active_operation=None, error_code=None,
                presentation_status=(
                    "problems_ready"
                    if operation.operation_type in _AUXILIARY_QUESTION_OPERATION_TYPES
                    else workflow_repository.AssignmentWorkflowRecord.presentation_status
                ),
                updated_at=now,
            )
        )
        if claimed.rowcount != 1:
            _raise_stale_revision()
        operation.status = final_status
        operation.payload = validated_payload
        operation.progress = validated_progress
        operation.error_code = None
        operation.completed_at = now
        operation.updated_at = now
        operation.lease_owner = None
        operation.lease_token = None
        operation.lease_expires_at = None
        operation.lease_heartbeat_at = None
        session.flush()
        return expected_workflow_revision


def _safe_student_token(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    return cleaned[:48] or "student"


def _commit_imported_submissions(
    *, task_id: str, owner_id: str, course_id: str, students: list[dict],
    replace_existing: bool = False,
    expected_workflow_revision: int | None = None,
    operation_id: str | None = None,
    expected_operation_attempt: int | None = None,
    expected_lease_token: str | None = None,
    operation_progress: dict | None = None,
    submission_file_name: str | None = None,
    source_id: str | None = None,
    source_artifact_file_id: str | None = None,
    source_summary: dict[str, int] | None = None,
) -> int:
    """Publish and persist a parsed teacher batch in one transaction.

    Parsing/OCR happens before this boundary.  If any normalized student,
    enrollment, revision, answer, or presentation row fails, the assignment
    remains in its previous state and no half-import is visible.
    """
    now = time.time()
    with session_scope() as session:
        operation = None
        if operation_id is not None:
            if expected_operation_attempt is None:
                raise ValidationError("operation_attempt_required")
            operation = _cas_operation_attempt_for_write(
                session, task_id=task_id, owner_id=owner_id,
                operation_id=operation_id,
                expected_operation_attempt=expected_operation_attempt,
                expected_statuses=("running",),
                expected_lease_token=expected_lease_token,
            )
        source = None
        if source_id is not None:
            if operation is None or source_artifact_file_id is None:
                raise ValidationError("operation_source_outcome_required")
            source = session.scalar(select(
                source_outcome_repository.WorkflowSourceItemRecord
            ).where(
                source_outcome_repository.WorkflowSourceItemRecord.id == source_id,
                source_outcome_repository.WorkflowSourceItemRecord.owner_id == owner_id,
                source_outcome_repository.WorkflowSourceItemRecord.assignment_id == task_id,
                source_outcome_repository.WorkflowSourceItemRecord.operation_id == operation_id,
                source_outcome_repository.WorkflowSourceItemRecord.attempt
                == expected_operation_attempt,
            ))
            if source is None:
                raise NotFound("workflow_source")
            artifact = session.scalar(select(file_repository.StoredFileRecord.id).where(
                file_repository.StoredFileRecord.id == source_artifact_file_id,
                file_repository.StoredFileRecord.owner_id == owner_id,
                file_repository.StoredFileRecord.assignment_id == task_id,
            ))
            if artifact is None:
                raise NotFound("workflow_source")
            if session.get(
                source_outcome_repository.WorkflowSourceOutcomeRecord, source_id
            ) is not None:
                raise VersionConflict("Workflow source outcome already exists.")
        assignment = session.scalar(
            select(AssignmentRecord).where(
                AssignmentRecord.id == task_id,
                AssignmentRecord.teacher_id == owner_id,
            )
        )
        if assignment is None:
            raise NotFound("assignment")
        if assignment.status not in (
            *education.EDITABLE_ASSIGNMENT_STATUSES,
            education.AssignmentStatus.PUBLISHED.value,
        ):
            raise InvalidTransition("assignment_not_open")

        questions = session.scalars(
            select(AssignmentQuestionRecord).where(
                AssignmentQuestionRecord.assignment_id == task_id
            )
        ).all()
        question_map = {question.q_id: question for question in questions}
        if not question_map:
            raise InvalidTransition("problems_required")

        active_presentation = session.scalar(
            select(workflow_repository.AssignmentStudentPresentationRecord.id)
            .where(
                workflow_repository.AssignmentStudentPresentationRecord.assignment_id
                == task_id,
                workflow_repository.AssignmentStudentPresentationRecord.is_active.is_(True),
            )
            .limit(1)
        )
        if active_presentation is not None and not replace_existing:
            _raise_replacement_confirmation_required()

        if expected_workflow_revision is not None:
            claimed = session.execute(
                update(workflow_repository.AssignmentWorkflowRecord)
                .where(
                    workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
                    workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
                    workflow_repository.AssignmentWorkflowRecord.workflow_revision
                    == expected_workflow_revision,
                )
                .values(
                    workflow_revision=(
                        workflow_repository.AssignmentWorkflowRecord.workflow_revision + 1
                    ),
                    presentation_status="submissions_ready",
                    active_operation=None,
                    active_job_id=None,
                    submission_file_name=submission_file_name,
                    pending_submission_file_name=None,
                    grading_job_id=None,
                    last_failed_job_id=None,
                    analysis_status="not_generated",
                    analysis_result_version=None,
                    analysis_generated_at=None,
                    analysis_error_code=None,
                    error_code=None,
                    updated_at=now,
                )
            )
            if claimed.rowcount != 1:
                _raise_stale_revision()

        if replace_existing:
            session.execute(
                update(workflow_repository.AssignmentStudentPresentationRecord)
                .where(
                    workflow_repository.AssignmentStudentPresentationRecord.assignment_id
                    == task_id
                )
                .values(is_active=False, updated_at=now)
            )

        for student in students:
            source_id = str(student.get("source_id") or "").strip() or None
            if source_id is not None:
                if operation is None or expected_operation_attempt is None:
                    raise ValidationError("submission_source_operation_required")
                linked_outcome_status = session.scalar(
                    select(source_outcome_repository.WorkflowSourceOutcomeRecord.status)
                    .join(
                        source_outcome_repository.WorkflowSourceItemRecord,
                        source_outcome_repository.WorkflowSourceItemRecord.id
                        == source_outcome_repository.WorkflowSourceOutcomeRecord.source_id,
                    )
                    .where(
                        source_outcome_repository.WorkflowSourceItemRecord.id == source_id,
                        source_outcome_repository.WorkflowSourceItemRecord.owner_id == owner_id,
                        source_outcome_repository.WorkflowSourceItemRecord.assignment_id == task_id,
                        source_outcome_repository.WorkflowSourceItemRecord.operation_id
                        == operation.id,
                        source_outcome_repository.WorkflowSourceItemRecord.attempt
                        == expected_operation_attempt,
                        source_outcome_repository.WorkflowSourceOutcomeRecord.status.in_(
                            ("parsed", "identity_conflict")
                        ),
                    )
                )
                if linked_outcome_status is None:
                    raise NotFound("workflow_source")
            display_id = (
                str(student.get("stu_id") or "").strip()
                or f"unknown-{uuid.uuid4().hex[:6]}"
            )
            display_name = str(student.get("stu_name") or "").strip() or display_id
            digest = hashlib.sha256(
                f"{owner_id}\0{task_id}\0{display_id}".encode()
            ).hexdigest()[:16]
            student_id = f"imported_{digest}"
            user = session.get(UserRecord, student_id)
            if user is None:
                session.add(
                    UserRecord(
                        id=student_id,
                        username=(
                            f"imported-{digest}-{_safe_student_token(display_id)[:16]}"
                        ),
                        email=None,
                        role="student",
                        password_hash="!disabled-imported-account",
                        is_active=False,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.flush()

            enrollment = session.scalar(
                select(CourseEnrollmentRecord).where(
                    CourseEnrollmentRecord.course_id == course_id,
                    CourseEnrollmentRecord.student_id == student_id,
                )
            )
            if enrollment is None:
                session.add(
                    CourseEnrollmentRecord(
                        course_id=course_id,
                        student_id=student_id,
                        enrolled_at=now,
                    )
                )

            submission = session.scalar(
                select(SubmissionRecord).where(
                    SubmissionRecord.assignment_id == task_id,
                    SubmissionRecord.student_id == student_id,
                )
            )
            if submission is None:
                submission = SubmissionRecord(
                    id=f"sub_{uuid.uuid4().hex[:12]}",
                    assignment_id=task_id,
                    student_id=student_id,
                    current_revision_id=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(submission)
                session.flush()

            next_number = (
                session.scalar(
                    select(func.max(SubmissionRevisionRecord.revision_number)).where(
                        SubmissionRevisionRecord.submission_id == submission.id
                    )
                )
                or 0
            ) + 1
            revision_id = f"rev_{uuid.uuid4().hex[:12]}"
            revision = SubmissionRevisionRecord(
                id=revision_id,
                submission_id=submission.id,
                revision_number=next_number,
                source=education.SubmissionRevisionSource.TEACHER_IMPORT.value,
                file_name=str(student.get("source_filename") or ""),
                created_at=now,
            )
            session.add(revision)
            session.flush()

            for raw in student.get("stu_ans") or []:
                question = question_map.get(str(raw.get("q_id") or ""))
                if question is None:
                    continue
                session.add(
                    SubmissionAnswerRecord(
                        id=f"ans_{uuid.uuid4().hex[:12]}",
                        revision_id=revision_id,
                        question_id=question.id,
                        q_id=question.q_id,
                        number=str(raw.get("number") or question.number),
                        type=str(raw.get("type") or question.type),
                        content=str(raw.get("content") or ""),
                        flag=list(raw.get("flag") or []),
                        created_at=now,
                    )
                )
            submission.current_revision_id = revision_id
            submission.updated_at = now

            presentation = session.scalar(
                select(workflow_repository.AssignmentStudentPresentationRecord).where(
                    workflow_repository.AssignmentStudentPresentationRecord.assignment_id
                    == task_id,
                    workflow_repository.AssignmentStudentPresentationRecord.student_id
                    == student_id,
                )
            )
            if presentation is None:
                presentation = workflow_repository.AssignmentStudentPresentationRecord(
                    id=f"sp_{uuid.uuid4().hex[:12]}",
                    assignment_id=task_id,
                    student_id=student_id,
                    source_id=source_id,
                    display_student_id=display_id,
                    display_name=display_name,
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(presentation)
            presentation.source_id = source_id or presentation.source_id
            presentation.display_student_id = display_id
            presentation.display_name = display_name
            presentation.source_filename = str(student.get("source_filename") or "")
            presentation.identity_match_method = str(
                student.get("identity_match_method") or "filename"
            )
            presentation.identity_status = str(
                student.get("identity_status") or "needs_review"
            )
            presentation.is_active = True
            presentation.updated_at = now

        if assignment.status in education.EDITABLE_ASSIGNMENT_STATUSES:
            assignment.status = education.AssignmentStatus.PUBLISHED.value
            assignment.published_at = now
            assignment.version += 1
        assignment.updated_at = now
        if operation is not None:
            payload = dict(operation.payload or {})
            payload.update({
                "filename": submission_file_name,
                "student_count": len(students),
                "replace_confirmed": replace_existing,
                "source_summary": source_summary or {},
            })
            operation.status = "done"
            operation.progress = operation_progress or {}
            operation.payload = payload
            operation.error_code = None
            operation.completed_at = now
            operation.updated_at = now
            operation.lease_owner = None
            operation.lease_token = None
            operation.lease_expires_at = None
            operation.lease_heartbeat_at = None
        if source is not None:
            session.add(source_outcome_repository.WorkflowSourceOutcomeRecord(
                source_id=source.id,
                status="parsed",
                student_candidate=None,
                matched_answer_count=len(students),
                unknown_question_ids=[],
                stable_error_code=None,
                retryable=False,
                artifact_file_id=source_artifact_file_id,
                created_at=now,
            ))
        session.flush()
        return len(students)


def _fail_operation(
    task_id: str, owner_id: str, job_id: str, expected_operation_attempt: int,
    error_code: str, *, expected_lease_token: str | None = None,
    failed_source_id: str | None = None,
    operation_progress: dict | None = None,
) -> bool:
    safe = safe_background_error_code(error_code, "workflow_failed")
    now = time.time()
    # Keep the operation transition and workflow cleanup in one transaction.
    # Because retries reuse the operation id, splitting these writes would let
    # a newly claimed attempt become active between them and then be cleared by
    # the previous worker's late failure handler.
    with session_scope() as session:
        try:
            changes: dict[str, Any] = {
                "status": "error", "error_code": safe,
                "completed_at": now,
            }
            if operation_progress is not None:
                changes["progress"] = operation_progress
            operation = _cas_operation_attempt_for_write(
                session, task_id=task_id, owner_id=owner_id,
                operation_id=job_id,
                expected_operation_attempt=expected_operation_attempt,
                expected_statuses=("pending", "running", "ready"),
                expected_lease_token=expected_lease_token,
                changes=changes,
            )
        except (VersionConflict, InvalidTransition) as exc:
            if exc.code in {"stale_operation_attempt", "workflow_busy"}:
                return False
            raise
        workflow = session.scalar(select(
            workflow_repository.AssignmentWorkflowRecord
        ).where(
            workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
            workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
        ))
        if workflow is None:
            raise NotFound("workflow")
        if expected_lease_token is not None:
            operation.lease_owner = None
            operation.lease_token = None
            operation.lease_expires_at = None
            operation.lease_heartbeat_at = None
        if failed_source_id is not None:
            source = session.scalar(select(
                source_outcome_repository.WorkflowSourceItemRecord
            ).where(
                source_outcome_repository.WorkflowSourceItemRecord.id == failed_source_id,
                source_outcome_repository.WorkflowSourceItemRecord.owner_id == owner_id,
                source_outcome_repository.WorkflowSourceItemRecord.assignment_id == task_id,
                source_outcome_repository.WorkflowSourceItemRecord.operation_id == job_id,
                source_outcome_repository.WorkflowSourceItemRecord.attempt
                == expected_operation_attempt,
            ))
            if source is None:
                raise NotFound("workflow_source")
            if session.get(
                source_outcome_repository.WorkflowSourceOutcomeRecord,
                failed_source_id,
            ) is None:
                session.add(source_outcome_repository.WorkflowSourceOutcomeRecord(
                    source_id=failed_source_id,
                    status="parse_failed",
                    student_candidate=None,
                    matched_answer_count=0,
                    unknown_question_ids=[],
                    stable_error_code=safe,
                    retryable=True,
                    artifact_file_id=None,
                    created_at=now,
                ))
        if operation.operation_type in _AUXILIARY_QUESTION_OPERATION_TYPES:
            workflow.last_failed_job_id = job_id
            workflow.error_code = safe
            workflow.updated_at = now
            if workflow.active_job_id in {None, job_id}:
                workflow.presentation_status = "problems_ready"
            if workflow.active_job_id == job_id:
                workflow.active_operation = None
                workflow.active_job_id = None
        elif workflow.active_job_id == job_id:
            workflow.presentation_status = "error"
            workflow.active_operation = None
            workflow.active_job_id = None
            workflow.last_failed_job_id = job_id
            workflow.error_code = safe
            workflow.updated_at = now
        session.flush()
        return True


def task_state(*, task_id: str, owner_id: str) -> dict:
    payload = get_task(task_id=task_id, owner_id=owner_id, full=False)
    try:
        workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    except NotFound:
        return payload
    _source_summary, source_rows = _submission_source_projection(workflow, owner_id)
    progress: dict | None = None
    if workflow.active_job_id:
        reporter = get_reporter(workflow.active_job_id)
        if reporter is not None:
            # Snapshot is async; callers should use async_task_state.
            progress = None
        else:
            try:
                operation = workflow_repository.get_operation(
                    workflow.active_job_id, owner_id=owner_id
                )
                progress = dict(operation.progress or {}) or None
            except NotFound:
                progress = None
    elif workflow.extract_job_id or workflow.parse_job_id:
        job_id = workflow.parse_job_id or workflow.extract_job_id
        if job_id:
            try:
                operation = workflow_repository.get_operation(job_id, owner_id=owner_id)
                progress = dict(operation.progress or {}) or None
            except NotFound:
                pass
    payload.update({
        "progress": progress,
        "active_job_id": workflow.active_job_id,
        "active_operation": workflow.active_operation,
        "submission_sources": source_rows,
    })
    return payload


async def async_task_state(*, task_id: str, owner_id: str) -> dict:
    payload = task_state(task_id=task_id, owner_id=owner_id)
    active_job_id = payload.get("active_job_id")
    if active_job_id and (reporter := get_reporter(active_job_id)) is not None:
        payload["progress"] = (await reporter.snapshot()).model_dump(mode="json")
    grading_job_id = payload.get("grading_job_id")
    grading_owns_failure = bool(
        payload.get("status") == "error"
        and grading_job_id
        and (
            payload.get("last_failed_job_id") == grading_job_id
            or (
                payload.get("last_failed_job_id") is None
                and payload.get("active_operation") == "grading"
                and payload.get("active_job_id") == grading_job_id
            )
        )
    )
    if (
        grading_job_id
        and (payload.get("status") == "grading" or grading_owns_failure)
    ):
        grading_progress = _grading_progress(grading_job_id, owner_id)
        # A reporter can reach ``done`` before result persistence finishes.
        # Prefer the durable failed-run projection so clients never render a
        # stale success phase after the database marks the run failed.
        if payload.get("status") == "grading" or grading_progress.get("phase") == "error":
            payload["progress"] = grading_progress
            payload["error"] = payload.get("error") or grading_progress.get("error_detail")
            if payload.get("status") == "grading":
                payload["active_job_id"] = grading_job_id
                payload["active_operation"] = "grading"
    return payload


def _grading_progress(run_id: str, owner_id: str) -> dict:
    run = grading_repository.get_run(run_id, actor_id=owner_id)
    events = grading_repository.list_events(run_id, actor_id=owner_id)
    question_count = len(assignment_repository.get_questions_by_assignment(run.assignment_id))
    completed_units = run.completed_submissions * question_count
    for item in events:
        value = item.get("payload", {}).get("completed_units")
        if isinstance(value, int) and not isinstance(value, bool):
            completed_units = max(completed_units, value)
    messages = [
        {
            "ts": item["created_at"],
            "level": item["level"] if item["level"] in {"info", "warn", "error"} else "info",
            "message": item["message"],
        }
        for item in events[-100:]
    ]
    return {
        "contract_version": 1, "job_id": run_id,
        "phase": "done" if run.status in {"completed", "partial_failed"} else (
            "error" if run.status == "failed" else "grading"
        ),
        "total_students": run.total_submissions,
        "total_questions": question_count,
        "completed_units": completed_units,
        "active": [], "messages": messages,
        "error_detail": _grading_failure_code(run),
        "started_at": run.started_at or run.created_at,
        "workflow": "grading", "stage_sequence": [],
        "current_step": "completed" if run.status in {"completed", "partial_failed"} else "grading",
        "total_steps": None, "completed_steps": None, "stage_metrics": {},
    }


def grading_readiness(
    *,
    task_id: str,
    owner_id: str,
) -> dict[str, list[str] | bool]:
    """Authoritative, fail-closed gate shared by preflight and mutation."""
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    questions = assignment_repository.get_questions_by_assignment(task_id)
    submissions = _active_submissions(task_id, owner_id)
    presentations = workflow_repository.list_student_presentations(task_id)
    source_summary, source_rows = _submission_source_projection(workflow, owner_id)

    issues: list[str] = []
    warnings: list[str] = []
    if not questions:
        issues.append("questions_required")
    if not submissions:
        issues.append("submissions_required")
    if workflow.active_operation and workflow.active_operation != "grading":
        issues.append("workflow_busy")

    if submissions:
        if not workflow.parse_job_id or source_summary["uploaded"] == 0:
            issues.append("submission_source_evidence_missing")
        else:
            if source_summary["pending"]:
                issues.append("submission_sources_pending")
            if source_summary["failed"]:
                issues.append("submission_sources_failed")
            if source_summary["identity_needs_review"]:
                issues.append("submission_identities_unresolved")
            evidenced_submission_count = (
                source_summary["parsed"]
                + source_summary["identity_needs_review"]
            )
            if evidenced_submission_count != len(submissions):
                issues.append("submission_source_evidence_missing")

        for submission in submissions:
            presentation = presentations.get(submission.student_id)
            if presentation is None or not presentation.source_id:
                issues.append("submission_source_evidence_missing")
                continue
            if presentation.identity_status != "matched":
                issues.append("submission_identities_unresolved")

    if any(row.get("unknown_question_ids") for row in source_rows):
        warnings.append("submission_question_ids_unmatched")
    ordered_issues = list(dict.fromkeys(issues))
    return {
        "ready": not ordered_issues,
        "blocking_issues": ordered_issues,
        "warnings": list(dict.fromkeys(warnings)),
    }


def start_task_grading(
    *,
    task_id: str,
    owner_id: str,
    expected_workflow_revision: int,
) -> dict:
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    workflow = _reconcile_terminal_active_operation(
        task_id=task_id, owner_id=owner_id, workflow=workflow
    )
    if workflow.grading_setup is None:
        raise InvalidTransition("grading_setup_required")
    if workflow.active_job_id and workflow.active_operation != "grading":
        raise InvalidTransition("The task is busy.", code="workflow_busy")
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    active = next((run for run in reversed(runs) if run.status in {"queued", "running"}), None)
    if active:
        workflow_repository.bind_existing_active_grading_run(
            task_id,
            owner_id=owner_id,
            run_id=active.id,
        )
        return {"status": "already_running", "task_id": task_id, "job_id": active.id}
    if workflow.active_job_id:
        raise InvalidTransition("The task is busy.", code="workflow_busy")
    if workflow.workflow_revision != expected_workflow_revision:
        raise VersionConflict("workflow_revision_conflict")
    readiness = grading_readiness(task_id=task_id, owner_id=owner_id)
    blocking_issues = list(readiness["blocking_issues"])
    if blocking_issues:
        issue = blocking_issues[0]
        raise InvalidTransition(issue, code=issue)
    questions = assignment_repository.get_questions_by_assignment(task_id)
    submissions = _active_submissions(task_id, owner_id)
    presentations = workflow_repository.list_student_presentations(task_id)
    revisions = [
        submission_repository.get_revision(
            revision_id=submission.current_revision_id, actor_id=owner_id
        )
        for submission in submissions
        if submission.current_revision_id is not None
    ]
    answer_statuses = workflow_repository.answer_review_statuses([
        answer.id for revision in revisions for answer in revision.answers
    ])
    try:
        setup = TaskGradingSetup.model_validate(workflow.grading_setup)
    except PydanticValidationError as exc:
        raise ValidationError(
            "grading_setup_invalid", code="grading_setup_invalid"
        ) from exc
    from backend.services.grading_input_security import (
        provider_configuration_fingerprint,
    )

    input_manifest = {
        "questions": [question.model_dump(mode="json") for question in questions],
        "submission_revision_ids": sorted(
            submission.current_revision_id
            for submission in submissions
            if submission.current_revision_id is not None
        ),
        "knowledge_document_ids": sorted(_selected_knowledge(task_id, owner_id)),
        "provider_configuration_fingerprint": provider_configuration_fingerprint(
            owner_id=owner_id,
            selected_provider_ids=setup.selected_provider_ids,
        ),
        "student_presentations": [
            {
                "student_id": submission.student_id,
                "display_student_id": (
                    presentations[submission.student_id].display_student_id
                    if submission.student_id in presentations
                    else submission.student_id
                ),
                "display_name": (
                    presentations[submission.student_id].display_name
                    if submission.student_id in presentations
                    else submission.student_id
                ),
                "source_filename": (
                    presentations[submission.student_id].source_filename
                    if submission.student_id in presentations
                    else None
                ),
                "identity_match_method": (
                    presentations[submission.student_id].identity_match_method
                    if submission.student_id in presentations
                    else "filename"
                ),
                "identity_status": (
                    presentations[submission.student_id].identity_status
                    if submission.student_id in presentations
                    else "matched"
                ),
            }
            for submission in sorted(submissions, key=lambda item: item.student_id)
        ],
        "answer_review_statuses": {
            answer_id: answer_statuses[answer_id]
            for answer_id in sorted(answer_statuses)
        },
    }
    run_fingerprint = _hash_json({
        "grading_setup": workflow.grading_setup,
        "input_manifest": input_manifest,
    })
    latest = runs[-1] if runs else None
    if latest and latest.status in {"completed", "partial_failed"}:
        frozen = workflow_repository.get_run_setup(latest.id)
        if frozen is not None and frozen.fingerprint == run_fingerprint:
            return {"status": "already_done", "task_id": task_id, "job_id": latest.id}
    try:
        run = grading_runs.start_run(
            assignment_id=task_id,
            teacher_id=owner_id,
            grading_setup=dict(workflow.grading_setup),
            setup_fingerprint=run_fingerprint,
            input_manifest=input_manifest,
            workflow_expected_revision=expected_workflow_revision,
        )
    except (DuplicateActiveRun, VersionConflict):
        concurrent_runs = grading_repository.list_runs_for_assignment(
            task_id,
            actor_id=owner_id,
        )
        concurrent = next(
            (
                item
                for item in reversed(concurrent_runs)
                if item.status in {"queued", "running"}
            ),
            None,
        )
        if concurrent is None:
            raise
        workflow_repository.bind_existing_active_grading_run(
            task_id,
            owner_id=owner_id,
            run_id=concurrent.id,
        )
        return {
            "status": "already_running",
            "task_id": task_id,
            "job_id": concurrent.id,
        }
    return {"status": "started", "task_id": task_id, "job_id": run.id}


def task_results(*, task_id: str, owner_id: str) -> dict:
    task = get_task(task_id=task_id, owner_id=owner_id, full=True)
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    run = _current_grading_run(workflow, runs)
    if run is None:
        return {"status": task["status"], "task_id": task_id}
    if run.status not in {"completed", "partial_failed"}:
        return {
            "status": "not_found" if run.status == "failed" else task["status"],
            "task_id": task_id,
            "error": _grading_failure_code(run),
        }
    results = grading_repository.list_results_for_run(run.id)
    presentations = workflow_repository.list_student_presentations(task_id)
    grouped: dict[str, list] = defaultdict(list)
    for result in results:
        grouped[result.student_id].append(result)
    rendered = []
    student_data = task["student_data"]
    for internal_id, rows in grouped.items():
        presentation = presentations.get(internal_id)
        display_id = presentation.display_student_id if presentation else internal_id
        rendered.append({
            "student_id": display_id,
            "student_name": presentation.display_name if presentation else internal_id,
            "corrections": [_serialize_correction(row) for row in rows],
            "student_answers": student_data.get(display_id, {}).get("stu_ans", []),
        })
    return {
        "status": "completed", "task_id": task_id, "results": rendered,
        "problem_data": task["problem_data"],
        "student_data": task["student_data"], "timestamp": run.completed_at,
    }


def _serialize_correction(result) -> dict:
    review = result.teacher_review or {}
    score = result.effective_score
    review_status = (
        "confirmed" if review.get("confirmed") else "edited"
    ) if review else (
        "pending" if result.requires_review else "confirmed"
    )
    return {
        "result_id": result.id,
        "q_id": result.q_id,
        "type": "",
        "score": score,
        "provisional_score": result.ai_score,
        "max_score": result.ai_max_score,
        "confidence": result.ai_confidence or 0,
        "comment": result.ai_comment,
        "steps": result.ai_steps,
        "hits": None, "logs": None,
        "expert_results": result.ai_expert_results,
        "synthesis_method": result.ai_synthesis_method,
        "is_score": None,
        "requires_human_review": result.requires_review,
        "review_reasons": list(result.review_reasons or []),
        "initial_review_reasons": list(result.initial_review_reasons or []),
        "teacher_score": review.get("new_score") if review else None,
        "teacher_comment": review.get("new_comment", "") if review else "",
        "review_status": review_status,
        "reviewed_at": review.get("created_at") if review else None,
    }


def update_problem(
    *, task_id: str, owner_id: str, q_id: str, patch: dict,
    expected_revision: int | None = None,
) -> dict:
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    workflow = _reconcile_terminal_active_operation(
        task_id=task_id, owner_id=owner_id, workflow=workflow
    )
    if workflow.active_job_id:
        raise InvalidTransition("The task is busy.", code="workflow_busy")
    now = time.time()
    substantive = bool({
        "stem", "criterion", "max_score", "reference_answer",
        "solution_code", "test_cases",
    }.intersection(patch))
    with session_scope() as session:
        workflow_row = session.scalar(select(
            workflow_repository.AssignmentWorkflowRecord
        ).where(
            workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
            workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
        ).with_for_update())
        if workflow_row is None:
            raise NotFound("workflow")
        if workflow_row.active_job_id:
            raise InvalidTransition("The task is busy.", code="workflow_busy")
        if (
            expected_revision is not None
            and workflow_row.workflow_revision != expected_revision
        ):
            _raise_stale_revision()
        question = session.scalar(select(AssignmentQuestionRecord).where(
            AssignmentQuestionRecord.assignment_id == task_id,
            AssignmentQuestionRecord.q_id == q_id,
        ))
        if question is None:
            raise NotFound("question")
        source = dict(question.source or {})
        presentation = dict(source.get("presentation") or {})
        for key in (
            "review_status", "solution_code", "material_provenance",
            "ai_completion_provenance", "preparation_issues",
        ):
            if key in patch:
                presentation[key] = patch[key]
        if patch.get("review_status") == "confirmed":
            presentation["max_score_review_status"] = "confirmed"
            presentation["preparation_issues"] = [
                issue for issue in presentation.get("preparation_issues", [])
                if issue.get("field") != "max_score"
            ]
        if "max_score" in patch:
            max_score = float(patch["max_score"])
            if not math.isfinite(max_score) or not 0 < max_score <= 10_000:
                raise ValidationError(
                    "Question maximum score must be between 0 and 10000.",
                    code="invalid_max_score",
                )
            presentation["max_score_source"] = "teacher_edited"
            presentation["max_score_review_status"] = "confirmed"
            presentation["preparation_issues"] = [
                issue for issue in presentation.get("preparation_issues", [])
                if issue.get("field") != "max_score"
            ]
        source["presentation"] = presentation
        for key in (
            "stem", "criterion", "max_score", "reference_answer", "test_cases"
        ):
            if key in patch:
                setattr(question, key, patch[key])
        question.source = source
        question.version += 1
        question.updated_at = now
        if substantive:
            session.execute(update(
                workflow_repository.AssignmentStudentPresentationRecord
            ).where(
                workflow_repository.AssignmentStudentPresentationRecord.assignment_id
                == task_id
            ).values(is_active=False, updated_at=now))
            workflow_row.presentation_status = "problems_ready"
            workflow_row.parse_job_id = None
            workflow_row.grading_job_id = None
            workflow_row.last_failed_job_id = None
            workflow_row.submission_file_name = None
            workflow_row.pending_submission_file_name = None
            workflow_row.submission_roster_name = None
            workflow_row.submission_recognition_provider_id = None
            workflow_row.analysis_status = "not_generated"
            workflow_row.analysis_result_version = None
            workflow_row.analysis_generated_at = None
            workflow_row.analysis_error_code = None
            workflow_row.error_code = None
            assignment = session.scalar(select(AssignmentRecord).where(
                AssignmentRecord.id == task_id,
                AssignmentRecord.teacher_id == owner_id,
            ))
            if assignment is None:
                raise NotFound("assignment")
            assignment.status = education.AssignmentStatus.READY.value
            assignment.published_at = None
            assignment.version += 1
            assignment.updated_at = now
        workflow_row.workflow_revision += 1
        workflow_row.updated_at = now
        session.flush()
        workflow_revision = workflow_row.workflow_revision
    updated = next(
        item for item in assignment_repository.list_questions(
            task_id, teacher_id=owner_id
        ) if item.q_id == q_id
    )
    return {
        "status": "ok", "q_id": q_id,
        "problem": _serialize_problem(updated),
        "workflow_revision": workflow_revision,
    }


def update_student_answer(
    *, task_id: str, owner_id: str, display_student_id: str,
    q_id: str, patch: dict, expected_revision: int | None,
) -> dict:
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    workflow = _reconcile_terminal_active_operation(
        task_id=task_id, owner_id=owner_id, workflow=workflow
    )
    if workflow.active_job_id:
        raise InvalidTransition("The task is busy.", code="workflow_busy")
    now = time.time()
    target_payload: dict[str, Any] | None = None
    with session_scope() as session:
        workflow_row = session.scalar(
            select(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
                workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
            )
            .with_for_update()
        )
        if workflow_row is None:
            raise NotFound("workflow")
        if workflow_row.active_job_id:
            raise InvalidTransition("The task is busy.", code="workflow_busy")
        if (
            expected_revision is not None
            and workflow_row.workflow_revision != expected_revision
        ):
            _raise_stale_revision()

        presentation = session.scalar(
            select(workflow_repository.AssignmentStudentPresentationRecord).where(
                workflow_repository.AssignmentStudentPresentationRecord.assignment_id
                == task_id,
                workflow_repository.AssignmentStudentPresentationRecord.display_student_id
                == display_student_id,
                workflow_repository.AssignmentStudentPresentationRecord.is_active.is_(True),
            )
        )
        internal_id = presentation.student_id if presentation else display_student_id
        submission = session.scalar(select(SubmissionRecord).where(
            SubmissionRecord.assignment_id == task_id,
            SubmissionRecord.student_id == internal_id,
        ))
        if submission is None or submission.current_revision_id is None:
            raise NotFound("submission")
        assignment = session.scalar(select(AssignmentRecord).where(
            AssignmentRecord.id == task_id,
            AssignmentRecord.teacher_id == owner_id,
            AssignmentRecord.status == education.AssignmentStatus.PUBLISHED.value,
        ))
        if assignment is None:
            raise InvalidTransition("assignment_closed", code="assignment_closed")
        current_revision = session.get(
            SubmissionRevisionRecord, submission.current_revision_id
        )
        if current_revision is None:
            raise NotFound("submission_revision")
        answer_rows = session.scalars(
            select(SubmissionAnswerRecord)
            .where(SubmissionAnswerRecord.revision_id == current_revision.id)
            .order_by(SubmissionAnswerRecord.id)
        ).all()
        target = next((answer for answer in answer_rows if answer.q_id == q_id), None)
        question_exists = session.scalar(select(AssignmentQuestionRecord.id).where(
            AssignmentQuestionRecord.assignment_id == task_id,
            AssignmentQuestionRecord.q_id == q_id,
        ))
        if target is None or question_exists is None:
            raise NotFound("answer")

        next_number = (
            session.scalar(select(func.max(
                SubmissionRevisionRecord.revision_number
            )).where(
                SubmissionRevisionRecord.submission_id == submission.id
            ))
            or 0
        ) + 1
        new_revision = SubmissionRevisionRecord(
            id=f"rev_{uuid.uuid4().hex[:12]}",
            submission_id=submission.id,
            revision_number=next_number,
            source=education.SubmissionRevisionSource.TEACHER_IMPORT.value,
            file_name=current_revision.file_name,
            created_at=now,
        )
        session.add(new_revision)
        session.flush()
        target_answer_id: str | None = None
        for answer in answer_rows:
            is_target = answer.q_id == q_id
            content = (
                patch["content"]
                if is_target and patch.get("content") is not None
                else answer.content
            )
            flags = (
                patch["flag"]
                if is_target and patch.get("flag") is not None
                else answer.flag
            )
            answer_id = f"ans_{uuid.uuid4().hex[:12]}"
            session.add(SubmissionAnswerRecord(
                id=answer_id,
                revision_id=new_revision.id,
                question_id=answer.question_id,
                q_id=answer.q_id,
                number=answer.number,
                type=answer.type,
                content=content,
                flag=list(flags or []),
                created_at=now,
            ))
            if is_target:
                target_answer_id = answer_id
                target_payload = {
                    "q_id": answer.q_id,
                    "number": answer.number,
                    "type": answer.type,
                    "content": content,
                    "flag": list(flags or []),
                    "review_status": patch.get("review_status") or "pending",
                }
        assert target_answer_id is not None and target_payload is not None
        session.add(workflow_repository.SubmissionAnswerPresentationRecord(
            answer_id=target_answer_id,
            review_status=str(patch.get("review_status") or "pending"),
            updated_at=now,
        ))
        submission.current_revision_id = new_revision.id
        submission.updated_at = now

        # The answer edit and downstream invalidation are one transaction: a
        # stale browser cannot create a new current revision without also
        # detaching the obsolete grading generation.
        workflow_row.presentation_status = "submissions_ready"
        workflow_row.grading_job_id = None
        workflow_row.last_failed_job_id = None
        workflow_row.analysis_status = "not_generated"
        workflow_row.analysis_result_version = None
        workflow_row.analysis_generated_at = None
        workflow_row.analysis_error_code = None
        workflow_row.error_code = None
        workflow_row.workflow_revision += 1
        workflow_row.updated_at = now
        session.flush()
        workflow_revision = workflow_row.workflow_revision
    return {
        "status": "ok", "stu_id": display_student_id, "q_id": q_id,
        "answer": target_payload, "workflow_revision": workflow_revision,
    }


def update_student_identity(
    *, task_id: str, owner_id: str, current_display_id: str,
    new_display_id: str, new_display_name: str, expected_revision: int,
) -> dict:
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    if workflow.workflow_revision != expected_revision:
        _raise_stale_revision()
    normalized_display_id = new_display_id.strip()
    normalized_display_name = new_display_name.strip()
    presentations = workflow_repository.list_student_presentations(task_id)
    presentation = next(
        (item for item in presentations.values() if item.display_student_id == current_display_id),
        None,
    )
    if presentation is None:
        raise NotFound("student")
    duplicate = next(
        (item for item in presentations.values()
         if item.display_student_id == normalized_display_id
         and item.student_id != presentation.student_id),
        None,
    )
    if duplicate is not None:
        raise ValidationError(
            "The student ID is already used in this task.",
            code="student_identity_conflict",
        )
    # The revision CAS comes after every deterministic validation.  A rejected
    # identity edit must not silently advance the task revision.
    claimed_workflow = workflow_repository.update_workflow(
        task_id, owner_id=owner_id, expected_revision=expected_revision
    )
    updated = workflow_repository.upsert_student_presentation(
        assignment_id=task_id, student_id=presentation.student_id,
        display_student_id=normalized_display_id,
        display_name=normalized_display_name,
        source_filename=presentation.source_filename,
        identity_match_method=presentation.identity_match_method,
        identity_status="matched",
    )
    task = get_task(task_id=task_id, owner_id=owner_id, full=True)
    return {
        "status": "ok", "previous_student_id": current_display_id,
        "student": task["student_data"][updated.display_student_id],
        "workflow_revision": claimed_workflow.workflow_revision,
    }


def update_correction_review(
    *, task_id: str, owner_id: str, display_student_id: str, q_id: str,
    teacher_score: float, teacher_comment: str, confirm: bool,
    expected_revision: int,
) -> dict:
    claimed_workflow = workflow_repository.update_workflow(
        task_id, owner_id=owner_id, expected_revision=expected_revision
    )
    presentations = workflow_repository.list_student_presentations(task_id)
    presentation = next(
        (item for item in presentations.values() if item.display_student_id == display_student_id),
        None,
    )
    internal_id = presentation.student_id if presentation else display_student_id
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    target_run = _current_grading_run(workflow, runs)
    if target_run is None:
        raise NotFound("grading_run")
    if target_run.released_at is not None:
        target_run = grading_repository.clone_released_run_for_review(
            target_run.id, teacher_id=owner_id
        )
        workflow_repository.update_workflow(
            task_id, owner_id=owner_id, bump_revision=False,
            presentation_status="graded", grading_job_id=target_run.id,
            analysis_status="not_generated", analysis_result_version=None,
            analysis_generated_at=None, analysis_error_code=None,
        )
    rows = grading_repository.list_results_for_run(target_run.id)
    row = next((item for item in rows if item.student_id == internal_id and item.q_id == q_id), None)
    if row is None:
        raise NotFound("grade_result")
    previous_score = row.effective_score
    previous_comment = row.effective_comment
    unchanged = previous_score == teacher_score and previous_comment == teacher_comment
    if not unchanged or confirm:
        grading_runs.add_teacher_review(
            grade_result_id=row.id, teacher_id=owner_id,
            new_score=teacher_score, new_comment=teacher_comment,
            confirm=confirm,
        )
    refreshed = next(
        item for item in grading_repository.list_results_for_run(target_run.id)
        if item.id == row.id
    )
    return {
        "status": "ok", "unchanged": unchanged,
        "student_id": display_student_id, "q_id": q_id,
        "correction": _serialize_correction(refreshed),
        "workflow_revision": claimed_workflow.workflow_revision,
    }


def finalization(*, task_id: str, owner_id: str) -> dict:
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    run = _current_grading_run(workflow, runs)
    remaining = []
    required_review_count = 0
    confirmed_required_count = 0
    if run is not None:
        for result in grading_repository.list_results_for_run(run.id):
            if result.initial_requires_review:
                required_review_count += 1
                review = result.teacher_review or {}
                if (
                    result.result_status
                    not in education.NON_SCOREABLE_RESULT_STATUSES
                    and review.get("confirmed") is True
                ):
                    confirmed_required_count += 1
            if result.result_status in education.NON_SCOREABLE_RESULT_STATUSES:
                presentation = workflow_repository.list_student_presentations(task_id).get(result.student_id)
                remaining.append({
                    "student_id": presentation.display_student_id if presentation else result.student_id,
                    "q_id": result.q_id,
                    "reasons": list(result.review_reasons or []),
                    "confirmed": False,
                })
    released = bool(run and run.released_at is not None)
    status = "finalized" if released else _presentation_status(
        workflow,
        assignment_repository.get_questions_by_assignment(task_id),
        submission_repository.list_submissions(task_id, actor_id=owner_id),
        run,
    )
    return {
        "task_id": task_id, "task_status": status,
        "workflow_revision": workflow.workflow_revision,
        "ready_for_confirmation": bool(run and run.status in {"completed", "partial_failed"} and not remaining),
        "required_review_count": required_review_count,
        "confirmed_required_count": confirmed_required_count,
        "remaining_review_count": len(remaining), "remaining_reviews": remaining,
        "final_result_version": max(workflow.final_result_version, 1 if released else 0),
        "final_result_updated_at": workflow.final_result_updated_at or (run.released_at if run else None),
        "final_result_dirty": _final_result_is_dirty(runs, run),
        "analysis_status": workflow.analysis_status,
        "analysis_result_version": workflow.analysis_result_version,
        "analysis_generated_at": workflow.analysis_generated_at,
        "analysis_error": workflow.analysis_error_code,
        "available_result_versions": len(workflow_repository.list_artifact_manifests(task_id, owner_id=owner_id)),
    }


def confirm_finalization(
    *, task_id: str, owner_id: str, expected_revision: int
) -> dict:
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    run = _current_grading_run(workflow, runs)
    if run is None:
        raise NotFound("grading_run")
    _workflow, _released_at, changed = (
        workflow_repository.confirm_final_result_atomic(
            assignment_id=task_id,
            owner_id=owner_id,
            grading_run_id=run.id,
            expected_revision=expected_revision,
        )
    )
    return {
        "status": "ok" if changed else "already_done",
        "unchanged": not changed,
        **finalization(task_id=task_id, owner_id=owner_id),
    }


def _run_snapshot_payload(*, task_id: str, owner_id: str, run_id: str) -> dict:
    run = grading_repository.get_run(run_id, actor_id=owner_id)
    if run.assignment_id != task_id:
        raise NotFound("grading_run")
    rows = grading_repository.list_results_for_run(run_id)
    frozen_setup = workflow_repository.get_run_setup(run_id)
    questions = grading_runs._questions_for_run(
        assignment_id=task_id, frozen_setup=frozen_setup,
    )

    presentations = workflow_repository.list_student_presentations(task_id)
    frozen_manifest = (frozen_setup.input_manifest if frozen_setup else {}) or {}
    frozen_presentations = {
        str(item.get("student_id")): item
        for item in frozen_manifest.get("student_presentations", [])
        if isinstance(item, dict) and item.get("student_id")
    }
    student_data: dict[str, dict] = {}
    internal_to_display: dict[str, str] = {}
    frozen_rows = grading_repository.list_frozen_submissions(run_id)
    revisions = [
        (
            frozen,
            submission_repository.get_revision(
                revision_id=frozen.id, actor_id=owner_id
            ),
        )
        for frozen in frozen_rows
    ]
    answer_ids = [answer.id for _, revision in revisions for answer in revision.answers]
    frozen_answer_statuses = frozen_manifest.get("answer_review_statuses")
    answer_statuses = (
        {
            str(answer_id): str(status)
            for answer_id, status in frozen_answer_statuses.items()
        }
        if isinstance(frozen_answer_statuses, dict)
        else workflow_repository.answer_review_statuses(answer_ids)
    )
    for frozen, revision in revisions:
        presentation = presentations.get(frozen.student_id)
        frozen_presentation = frozen_presentations.get(frozen.student_id)
        display_id = (
            str(frozen_presentation.get("display_student_id"))
            if frozen_presentation is not None
            else (
                presentation.display_student_id
                if presentation else frozen.student_id
            )
        )
        internal_to_display[frozen.student_id] = display_id
        student_data[display_id] = {
            "stu_id": display_id,
            "stu_name": (
                str(frozen_presentation.get("display_name"))
                if frozen_presentation is not None
                else (
                    presentation.display_name
                    if presentation else frozen.student_id
                )
            ),
            "stu_ans": [
                {
                    "q_id": answer.q_id,
                    "number": answer.number,
                    "type": answer.type,
                    "content": answer.content,
                    "flag": list(answer.flag or []),
                    "review_status": answer_statuses.get(answer.id, "pending"),
                }
                for answer in revision.answers
            ],
            "source_filename": (
                frozen_presentation.get("source_filename")
                if frozen_presentation is not None
                else (
                    presentation.source_filename
                    if presentation else revision.file_name
                )
            ),
            "identity_match_method": (
                frozen_presentation.get("identity_match_method")
                if frozen_presentation is not None
                else (
                    presentation.identity_match_method
                    if presentation else "filename"
                )
            ),
            "identity_status": (
                frozen_presentation.get("identity_status")
                if frozen_presentation is not None
                else (
                    presentation.identity_status
                    if presentation else "matched"
                )
            ),
        }

    grouped: dict[str, list] = defaultdict(list)
    for row in rows:
        grouped[row.student_id].append(row)
    rendered_results = []
    for internal_id, result_rows in grouped.items():
        display_id = internal_to_display.get(internal_id, internal_id)
        student = student_data.get(display_id, {})
        rendered_results.append({
            "student_id": display_id,
            "student_name": student.get("stu_name", display_id),
            "corrections": [_serialize_correction(row) for row in result_rows],
            "student_answers": student.get("stu_ans", []),
        })
    return {
        "results": rendered_results,
        "problem_data": {
            question.q_id: _serialize_problem(question) for question in questions
        },
        "student_data": student_data,
    }


def result_snapshot(
    *, task_id: str, owner_id: str, result_version: int = 1,
    grading_run_id: str | None = None,
) -> dict:
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    run = (
        next((item for item in runs if item.id == grading_run_id), None)
        if grading_run_id is not None
        else _current_grading_run(workflow, runs)
    )
    if run is None or run.status not in {"completed", "partial_failed"}:
        raise NotFound("grading_run")
    payload = _run_snapshot_payload(
        task_id=task_id, owner_id=owner_id, run_id=run.id
    )
    fingerprint = _hash_json(payload)
    return {
        "version": result_version, "fingerprint": fingerprint,
        "created_at": run.released_at or run.completed_at or run.created_at,
        "payload": payload,
    }


def generate_artifacts(
    *, task_id: str, owner_id: str, expected_revision: int
) -> dict:
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    run = _current_grading_run(workflow, runs)
    if run is None or run.released_at is None:
        raise InvalidTransition("result_not_finalized")
    if workflow.workflow_revision != expected_revision:
        # An exact replay is accepted by the atomic persistence operation; a
        # stale request with no existing artifact still fails there.
        existing = workflow_repository.list_artifact_manifests(
            task_id, owner_id=owner_id
        )
        if not any(
            item.result_version == workflow.final_result_version
            and item.grading_run_id == run.id
            for item in existing
        ):
            raise VersionConflict("workflow_revision_conflict")
    version = workflow.final_result_version
    if version <= 0:
        raise InvalidTransition("result_not_finalized")
    snapshot = result_snapshot(
        task_id=task_id, owner_id=owner_id, result_version=version,
        grading_run_id=run.id,
    )
    task = get_task(task_id=task_id, owner_id=owner_id, full=False)
    generated_at = time.time()
    manifest = build_artifact_manifest(
        task_id=task_id, task_name=task["name"], snapshot=snapshot,
        generated_at=generated_at,
    )
    record, created, _updated_workflow = (
        workflow_repository.save_artifact_manifest_atomic(
            assignment_id=task_id,
            grading_run_id=run.id,
            owner_id=owner_id,
            result_version=version,
            result_fingerprint=snapshot["fingerprint"],
            manifest=manifest,
            expected_revision=expected_revision,
        )
    )
    return {
        "status": "ok" if created else "already_done",
        "unchanged": not created,
        **finalization(task_id=task_id, owner_id=owner_id),
        "artifacts": artifact_index(task_id=task_id, owner_id=owner_id),
    }


def artifact_index(*, task_id: str, owner_id: str) -> dict:
    workflow = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    records = workflow_repository.list_artifact_manifests(task_id, owner_id=owner_id)
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    current_run = _current_grading_run(workflow, runs)
    final_result_dirty = _final_result_is_dirty(runs, current_run)
    current = max(workflow.final_result_version, 1 if records else 0)
    versions = [
        {
            "version": record.result_version,
            "current": record.result_version == current,
            "status": (
                "stale"
                if record.result_version == current
                and (workflow.analysis_status == "stale" or final_result_dirty)
                else (
                    "ready"
                    if record.result_version == current
                    else "historical"
                )
            ),
            "confirmed_at": record.manifest.get("confirmed_at"),
            "generated_at": record.generated_at,
            "files": record.manifest.get("files", []),
        }
        for record in records
    ]
    if current > 0 and not any(item["version"] == current for item in versions):
        versions.insert(0, {
            "version": current,
            "current": True,
            "status": "not_generated",
            "confirmed_at": workflow.final_result_updated_at,
            "generated_at": None,
            "files": [],
        })
    return {
        "task_id": task_id, "current_result_version": current,
        "analysis_status": workflow.analysis_status,
        "analysis_result_version": workflow.analysis_result_version,
        "versions": versions,
    }


def artifact_bytes(
    *, task_id: str, owner_id: str, version: int, artifact_id: str
) -> tuple[bytes, str, str]:
    record = workflow_repository.get_artifact_manifest(
        task_id, version, owner_id=owner_id
    )
    snapshot = result_snapshot(
        task_id=task_id, owner_id=owner_id, result_version=version,
        grading_run_id=record.grading_run_id,
    )
    if snapshot["fingerprint"] != record.result_fingerprint:
        raise VersionConflict("artifact_source_changed")
    if record.manifest.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise InvalidTransition(
            "Artifact renderer version is unsupported.",
            code="artifact_schema_unsupported",
        )
    task_name = str(record.manifest.get("task_name") or "")
    if not task_name:
        task_name = get_task(
            task_id=task_id, owner_id=owner_id, full=False
        )["name"]
    files = build_artifact_files(
        task_id=task_id, task_name=task_name, snapshot=snapshot,
        generated_at=record.generated_at,
    )
    expected_files = {
        str(item.get("artifact_id")): item
        for item in record.manifest.get("files", [])
        if isinstance(item, dict) and item.get("artifact_id")
    }
    for item in files:
        expected = expected_files.get(item.artifact_id)
        if (
            expected is None
            or expected.get("sha256")
            != hashlib.sha256(item.content).hexdigest()
            or expected.get("size_bytes") != len(item.content)
        ):
            raise VersionConflict("artifact_renderer_changed")
    rebuilt_manifest = build_artifact_manifest(
        task_id=task_id,
        task_name=task_name,
        snapshot=snapshot,
        generated_at=record.generated_at,
    )
    if (
        rebuilt_manifest.get("artifact_fingerprint")
        != record.manifest.get("artifact_fingerprint")
    ):
        raise VersionConflict("artifact_manifest_changed")
    if artifact_id == "bundle":
        return (
            build_artifact_bundle(files, record.manifest),
            "application/zip",
            f"smartai_{task_id}_v{version}_reports.zip",
        )
    artifact = next((item for item in files if item.artifact_id == artifact_id), None)
    if artifact is None:
        raise NotFound("artifact")
    return artifact.content, artifact.media_type, artifact.filename


def _get_task_tags(task_id: str, owner_id: str) -> list[str]:
    try:
        from backend.db import tag_repository
        return tag_repository.list_assignment_tag_ids(
            assignment_id=task_id, owner_id=owner_id
        )
    except (ImportError, AttributeError):
        return []


def _set_task_tags(task_id: str, owner_id: str, tag_ids: list[str]) -> None:
    try:
        from backend.db import tag_repository
        tag_repository.set_assignment_tags(
            assignment_id=task_id, owner_id=owner_id, tag_ids=tag_ids
        )
    except (ImportError, AttributeError):
        if tag_ids:
            raise ValidationError("tags_unavailable")
