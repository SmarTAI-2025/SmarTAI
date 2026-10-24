"""Normalized implementation of the Figma task presentation contract.

``task_id`` is the normalized assignment id.  This service translates between
the presentation DTO expected by the React app and the existing
course/assignment/submission/grading repositories.  It intentionally does not
recreate the removed TaskStore or JobStore.
"""
from __future__ import annotations
from backend.llm.image_capability import can_attempt_images

import asyncio
import hashlib
import json
import logging
import math
import re
import time
import uuid
from collections import defaultdict
from copy import deepcopy
from dataclasses import replace
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from backend.agents.ingest_agent import (
    SubmissionSourceParseResult,
    normalize_submission_source_identities,
    extract_problems,
    parse_student_answer_sources,
    parse_student_answer_sources_from_ocr_markdown,
)
from backend.db import (
    assignment_repository,
    course_repository,
    file_repository,
    grading_repository,
    source_outcome_repository,
    source_storage_repository,
    submission_repository,
    workflow_repository,
)
from backend.db.models import (
    AssignmentQuestionRecord,
    AssignmentRecord,
    AssignmentTagRecord,
    CourseRecord,
    GradingRunRecord,
    SubmissionAnswerRecord,
    SubmissionRecord,
    SubmissionRevisionRecord,
    UserRecord,
    TagRecord,
)
from backend.db.file_repository import get_file
from backend.db.session import session_scope
from backend.domain import education
from backend.domain.confidence_policy import confidence_needs_attention, confidence_value, question_issue_requires_confirmation
from backend.domain.errors import (
    DomainError,
    DuplicateActiveRun,
    InvalidTransition,
    LeaseLost,
    NotFound,
    ResultNotReleasable,
    ValidationError,
    VersionConflict,
)
from backend.domain.source_outcomes import safe_source_diagnostic
from backend.domain.source_storage import (
    DELAYED_SOURCE_OPERATION_TYPES,
    TASK_DELETE_OPERATION,
)
from backend.models import TaskGradingSetup
from backend.models import User
from backend.llm.registry import (
    resolve_owner_default_provider,
    resolve_owner_default_provider_id,
)
from backend.progress.tracker import get_or_create_reporter, get_reporter, remove_reporter
from backend.services import grading_runs
from backend.services.question_sources import read_question_source
from backend.services.recognition_timeouts import SOURCE_TIMEOUT_PROFILE
from backend.services.background_errors import (
    SAFE_BACKGROUND_ERROR_CODES,
    classify_background_error,
    is_retryable_background_error,
    safe_background_error_code,
)
from backend.services.result_artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    artifact_download_filename,
    artifact_fingerprint,
    build_artifact_bundle,
    build_artifact_files,
    build_artifact_manifest,
    is_artifact_enabled,
    is_current_artifact_manifest,
)
from backend.services.question_structure import (
    MajorQuestionStructureV1,
    QuestionRubricValidationError,
    build_major_question_structure,
    summarize_rubric_points,
    validate_rubric_points,
)
from backend.services.submission_source_pipeline import (
    PreparedSubmissionSource,
    attach_submission_recognition,
    failure_phase_for_code,
    prepare_submission_sources,
)
from backend.services.stage_provider_routing import (
    assert_grading_routes_supported,
    build_owner_baidu_ocr_skill,
    resolve_stage_provider_route,
    stage_provider_configuration_fingerprint,
)
from backend.skills.ocr_ingest import LLMVisionOCRSkill
from backend.storage import get_storage
from backend.tools.file_processing import (
    ARCHIVE_EXTENSIONS,
    SUBMISSION_UPLOAD_MAX_BYTES,
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

_QUESTION_PREPARATION_RETRY_FROZEN_FIELDS = (
    "contract_version",
    "owner_id",
    "task_id",
    "operation_type",
    "input_hash",
    "source_tokens",
    "source_refs",
    "source_content_hashes",
    "source_text_hashes",
    "requested_workflow_revision",
    "replace_confirmed",
    "generation_policy",
    "score_policy",
    "recognition_provider_id",
    "provider_configuration_fingerprint",
    "provider_capability",
    "prepared_source_provider_ids",
)


def _hash_json(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _registry_provider(registry, provider_id: str, owner_id: str):
    getter = getattr(registry, "get", None)
    if callable(getter):
        return getter(provider_id)
    default_id = resolve_owner_default_provider_id(owner_id, registry)
    return (
        resolve_owner_default_provider(owner_id, registry)
        if default_id == provider_id
        else None
    )


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
    expected_metadata: dict | None = None,
) -> dict:
    assignment_repository.get_assignment(task_id, actor_id=owner_id)
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
    with session_scope() as session:
        # SQLite has no SELECT FOR UPDATE. Serialize this small metadata write
        # before reading the baseline, as the row lock does on PostgreSQL.
        if session.get_bind().dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        # Match deletion/report generation's workflow -> assignment lock order.
        workflow = session.scalar(select(workflow_repository.AssignmentWorkflowRecord).where(
            workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
            workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
        ).with_for_update())
        if workflow is None:
            raise NotFound("workflow")
        row = session.scalar(select(AssignmentRecord).where(
            AssignmentRecord.id == task_id,
            AssignmentRecord.teacher_id == owner_id,
            AssignmentRecord.deletion_requested_at.is_(None),
        ).with_for_update())
        if row is None:
            raise NotFound("assignment")
        current_tags = sorted(session.scalars(select(AssignmentTagRecord.tag_id).where(
            AssignmentTagRecord.assignment_id == task_id,
        )))
        course = session.get(CourseRecord, row.course_id)
        current = dict(name=row.name, semester_id=workflow.semester_id,
                       course_id=None if course.code == SYSTEM_COURSE_CODE else row.course_id,
                       tag_ids=current_tags)
        touched = set(changes)
        if semester_id is not ...:
            touched.add("semester_id")
        if tag_ids is not None:
            touched.add("tag_ids")
        if expected_metadata is not None:
            expected = {**expected_metadata, "tag_ids": sorted(set(expected_metadata["tag_ids"]))}
            if any(expected.get(key) != current[key] for key in touched):
                raise VersionConflict("Task details changed. Review the latest values before saving.", code="task_metadata_conflict")
        if tag_ids is not None:
            unique_tags = sorted(set(tag_ids))
            valid = set(session.scalars(select(TagRecord.id).where(
                TagRecord.owner_id == owner_id, TagRecord.id.in_(unique_tags),
            )))
            if valid != set(unique_tags):
                raise ValidationError("tags_unavailable")
            session.execute(delete(AssignmentTagRecord).where(AssignmentTagRecord.assignment_id == task_id))
            session.add_all([AssignmentTagRecord(assignment_id=task_id, tag_id=tag_id, assigned_at=time.time()) for tag_id in unique_tags])
        for key, value in changes.items():
            setattr(row, key, value)
        if semester_id is not ...:
            workflow.semester_id = semester_id
            workflow.updated_at = time.time()
        # Metadata must not invalidate running recognition or grading work.
        row.version += 1
        row.updated_at = time.time()
    return get_task(task_id=task_id, owner_id=owner_id, full=False)


def delete_task(*, task_id: str, owner_id: str) -> dict[str, Any]:
    """Logically remove a task and enqueue restart-safe physical cleanup.

    The request is idempotent and is accepted for every workflow state.  The
    parent assignment remains only as an internal tombstone until task-owned
    files and outstanding upload reservations are confirmed absent; its final
    database cascade then removes questions, submissions, answers, grading
    results, reviews, workflow state, and report manifests.
    """
    now = time.time()
    reporter_ids: set[str] = set()
    with session_scope() as session:
        workflow = session.scalar(
            select(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id
                == task_id,
                workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
            )
            .with_for_update()
        )
        assignment = session.scalar(
            select(AssignmentRecord)
            .where(
                AssignmentRecord.id == task_id,
                AssignmentRecord.teacher_id == owner_id,
            )
            .with_for_update()
        )
        if assignment is None:
            raise NotFound("assignment")
        reporter_ids.update(str(operation_id) for operation_id in session.scalars(
            select(workflow_repository.WorkflowOperationRecord.id).where(
                workflow_repository.WorkflowOperationRecord.assignment_id
                == task_id,
                workflow_repository.WorkflowOperationRecord.owner_id == owner_id,
            )
        ))
        reporter_ids.update(str(run_id) for run_id in session.scalars(
            select(GradingRunRecord.id).where(
                GradingRunRecord.assignment_id == task_id,
                GradingRunRecord.teacher_id == owner_id,
            )
        ))
        existing = session.scalar(
            select(workflow_repository.WorkflowOperationRecord)
            .where(
                workflow_repository.WorkflowOperationRecord.assignment_id
                == task_id,
                workflow_repository.WorkflowOperationRecord.owner_id == owner_id,
                workflow_repository.WorkflowOperationRecord.operation_type
                == TASK_DELETE_OPERATION,
            )
            .limit(1)
        )
        if assignment.deletion_requested_at is None:
            assignment.deletion_requested_at = now
            assignment.updated_at = now
            assignment.version += 1
            if workflow is not None:
                # Fence any raw upload reserved against the previous epoch.
                workflow.source_lifecycle_epoch += 1
                workflow.updated_at = now
        if existing is None:
            operation_id = f"op_{uuid.uuid4().hex}"
            existing = workflow_repository.WorkflowOperationRecord(
                id=operation_id,
                assignment_id=task_id,
                owner_id=owner_id,
                operation_type=TASK_DELETE_OPERATION,
                input_hash=hashlib.sha256(
                    f"task-delete:{task_id}".encode("utf-8")
                ).hexdigest(),
                attempt=1,
                status="pending",
                payload={"schema": 1},
                progress={"state": "pending", "retry_count": 0},
                expires_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(existing)
            session.flush()
        reporter_ids.add(existing.id)
        response = {
            "status": "deletion_pending",
            "task_id": task_id,
            "cleanup_operation_id": existing.id,
        }
    # Reporters are rebuildable views over durable operation/run rows and may
    # contain student ids or question ids. Purge every known task reporter only
    # after the tombstone commits, so an accepted delete leaves no local RAM
    # copy even while physical object cleanup continues in the background.
    for reporter_id in reporter_ids:
        remove_reporter(reporter_id)
    return response


def list_tasks(*, owner_id: str) -> dict[str, dict]:
    with session_scope() as session:
        assignments = session.scalars(
            select(AssignmentRecord)
            .where(
                AssignmentRecord.teacher_id == owner_id,
                AssignmentRecord.deletion_requested_at.is_(None),
            )
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
        workflow = workflow_repository.get_live_workflow(
            task_id, owner_id=owner_id
        )
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
    paused_status = _manual_pause_status(workflow)
    grading_error_code = _workflow_grading_failure_code(workflow, latest_run)
    status = paused_status or ("error" if grading_error_code else _presentation_status(workflow, questions, submissions, latest_run))
    selected_docs = _selected_knowledge(task_id, owner_id)
    tag_ids = _get_task_tags(task_id, owner_id)
    source_summary, source_rows = _submission_source_projection(workflow, owner_id)
    incomplete_submissions = status in {"submissions_ready", "grading_setup"} and bool(source_summary["failed"] or source_summary["pending"])
    submission_error_code = next((row["reason_code"] for row in source_rows if row.get("reason_code")), "submission_parse_failed") if incomplete_submissions else None
    if incomplete_submissions:
        status = "error"
    attention = not paused_status and bool(
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
            latest_run.id if grading_error_code and latest_run else workflow.parse_job_id if incomplete_submissions else workflow.last_failed_job_id
        ),
        "problem_file_name": workflow.problem_file_name,
        "submission_file_name": workflow.submission_file_name,
        "pending_submission_file_name": workflow.pending_submission_file_name,
        "submission_identity_mode": workflow.submission_identity_mode,
        "submission_roster_name": workflow.submission_roster_name,
        "question_recognition_provider_id": workflow.question_recognition_provider_id,
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
        "manual_question_count_required": not questions and _manual_count_required(workflow, "question"),
        "manual_student_count_required": not submissions and _manual_count_required(workflow, "student"),
        "submission_source_summary": source_summary,
        "kb_docs": selected_docs,
        "kb_doc_count": len(selected_docs),
        "error": None if paused_status else grading_error_code or submission_error_code or workflow.error_code,
        "created_at": assignment.created_at,
        "updated_at": max(assignment.updated_at, workflow.updated_at),
    }
    if full:
        math_plans = _math_plans_for_projection(
            latest_run.id if latest_run else None, questions,
            grading_setup=workflow.grading_setup or {},
        )
        payload["problem_data"] = {
            question.q_id: _serialize_problem(
                question, math_plan=math_plans.get(question.q_id),
                feedback_language=(workflow.grading_setup or {}).get("feedback_language", "zh"),
            ) for question in questions
        }
        payload["student_data"] = {} if incomplete_submissions else _serialize_student_data(
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
    inactive_sources = {item.source_id for item in presentations.values() if not item.is_active and item.source_id}
    inactive_sources -= {item.source_id for item in presentations.values() if item.is_active and item.source_id}
    resolved_identity_sources = {
        presentation.source_id
        for presentation in presentations.values()
        if presentation.source_id and presentation.identity_status == "matched"
    }
    manual_review = operation.operation_type == "submission_manual_review"
    from backend.services.submission_source_state import load_source_states
    source_states = load_source_states(operation)
    manually_resolved_sources = set()
    unresolved_sources = set()
    if manual_review:
        for student in _serialize_student_data(workflow.assignment_id, owner_id, _active_submissions(workflow.assignment_id, owner_id)).values():
            if student.get("identity_status") == "matched" and not any(
                _answer_recognition_failures(answer.get("flag"))
                or (_REQUIRED_ANSWER_CONFIRMATION_FLAGS.intersection(answer.get("flag") or []) and answer.get("review_status") != "confirmed")
                for answer in student["stu_ans"]
            ):
                manually_resolved_sources.add(student.get("source_id"))
            else:
                unresolved_sources.add(student.get("source_id"))
        manually_resolved_sources -= unresolved_sources
    rows: list[dict] = []
    summary = dict(empty)
    for result in results:
        if result.source.id in inactive_sources:
            continue
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
        if manual_review and outcome is not None and result.source.id not in manually_resolved_sources:
            internal_status = outcome.status
            external_status = "manual_review"
            summary["manual_review"] = summary.get("manual_review", 0) + 1
        elif manual_review and outcome is not None:
            external_status = "parsed"
            internal_status = outcome.status
            summary["parsed"] += 1
        elif terminal_without_outcome:
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
            "page_diagnostics": source_states.get(result.source.stored_file_id, {}).get("page_diagnostics"),
            "resolution_status": (
                "identity_resolved"
                if outcome
                and outcome.status == "identity_conflict"
                and result.source.id in resolved_identity_sources
                else ("manual_resolved" if result.source.id in manually_resolved_sources else "manual_review") if manual_review
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


def _manual_pause_status(workflow) -> str | None:
    """Keep durable cancellation/retry records, but present a manual stop as paused."""
    if workflow.active_job_id or workflow.error_code != "operation_cancelled":
        return None
    job_id = workflow.last_failed_job_id
    if not job_id:
        return None
    if job_id == workflow.extract_job_id:
        return "problems_paused"
    if job_id == workflow.parse_job_id:
        return "submissions_paused"
    if job_id == workflow.grading_job_id:
        return "grading_paused"
    return None


def _manual_count_required(workflow, stage: str) -> bool:
    """Only an explicit current handoff can keep an empty review matrix open."""
    question = stage == "question"
    if (workflow.active_job_id or workflow.last_failed_job_id
            or workflow.presentation_status != ("problems_ready" if question else "submissions_ready")):
        return False
    job_id = workflow.extract_job_id if question else workflow.parse_job_id
    if not job_id:
        return False
    try:
        operation = workflow_repository.get_operation(job_id, owner_id=workflow.owner_id)
    except NotFound:
        return False
    return (operation.assignment_id == workflow.assignment_id
        and operation.operation_type == ("question_preparation" if question else "submission_manual_review")
        and (operation.checkpoint or {}).get(f"manual_{stage}_count_required") is True)


def _presentation_status(workflow, questions, submissions, latest_run) -> str:
    if workflow.presentation_status in {
        "extracting_problems", "parsing_submissions", "error"
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
    if not submissions and workflow.presentation_status in {"submissions_ready", "grading_setup"}:
        return "submissions_ready"
    if submissions:
        return "grading_setup" if workflow.grading_setup is not None else "submissions_ready"
    if questions or workflow.presentation_status == "problems_ready":
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
        "draft", "grading", "graded", "finalized",
        # Read historical rows through the current run; never emit old stages.
        "review_confirmed", "generating_analysis",
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
        return repaired or workflow_repository.get_live_workflow(
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
    return repaired or workflow_repository.get_live_workflow(
        task_id, owner_id=owner_id
    )


def _grading_failure_code(run) -> str | None:
    if run is None or run.status not in {"failed", "partial_failed", "cancelled"}:
        return None
    if run.error_message:
        return safe_background_error_code(run.error_message, "grading_failed")
    missing = [result for result in grading_repository.list_results_for_run(run.id)
               if result.effective_score is None or not math.isfinite(result.effective_score)]
    if run.status == "partial_failed" and not missing:
        return None
    kinds = [expert.get("error_kind") for result in missing
             for expert in (result.ai_expert_results or []) if isinstance(expert, dict)]
    if kinds and all(kind == "daily_quota_exhausted" for kind in kinds):
        return "provider_daily_quota_exceeded"
    # Compatibility for already persisted runs from before this stable code.
    legacy_execution_unavailable = any(
        expert.get("error_kind") == "general" and expert.get("comment") ==
        "该题未执行：生产环境禁止宿主代码执行。隔离执行尚未接通，请人工复核。"
        for result in missing for expert in (result.ai_expert_results or []) if isinstance(expert, dict)
    )
    if "programming_execution_unavailable" in kinds or legacy_execution_unavailable:
        return "programming_execution_unavailable"
    if "quota_exhausted" in kinds:
        return "provider_rate_limited"
    if "parse_failed" in kinds:
        return "provider_response_invalid"
    if "transient_llm" in kinds:
        return "grading_provider_transient"
    return "grading_failed"


def _workflow_grading_failure_code(workflow, run) -> str | None:
    """Project a grading failure only while that run owns the task failure.

    A confirmed upstream restart can fail after an older grading run failed.
    The historical run remains queryable for audit, but it must not replace the
    newer OCR/import reason shown on the task and submission progress pages.
    """
    if run is None:
        return None
    # Failed execution remains in the run for audit, but an explicit handoff
    # makes its missing scores review work. Never infer this from "graded":
    # ordinary partial failures also have that status and a null failure marker.
    if (workflow.presentation_status == "grading_manual_review"
            and workflow.grading_job_id == run.id and run.status == "partial_failed"
            and not workflow.active_job_id and not workflow.last_failed_job_id
            and not workflow.error_code):
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


def _math_plans_for_projection(run_id, questions, *, grading_setup=None) -> dict:
    """Only the selected run's unchanged inputs may explain current questions."""
    from backend.db.models import GradingRunEventRecord
    from backend.services.math_grading_context import ENGINE_EVENT, PLAN_EVENT, matching_math_plans, plans_from_snapshots

    if run_id is None:
        return {}
    frozen = workflow_repository.get_run_setup(run_id)
    if frozen is None or (grading_setup is not None and frozen.setup != grading_setup):
        return {}
    # A question read must not fetch every student's private evidence trace.
    with session_scope() as session:
        events = [{"message": row.message, "payload": row.payload} for row in session.scalars(
            select(GradingRunEventRecord).where(
                GradingRunEventRecord.grading_run_id == run_id,
                GradingRunEventRecord.message.in_([ENGINE_EVENT, PLAN_EVENT]),
            ).order_by(GradingRunEventRecord.sequence)
        )]
    return matching_math_plans(questions, plans_from_snapshots(frozen.input_manifest or {}, events))


def _math_rubric_advisory(question, plan, language: str) -> list[dict]:
    """Project readable shared rules, never the private scoring graph or source."""
    if plan is None or not any(text.strip() for text in plan.advisories):
        return []
    from backend.services.math_rubric_plan import _FALLBACK_ADVISORY, _FOLLOW_THROUGH, _NUMERICAL_POLICY

    zh = language == "zh"
    presentation = (question.source or {}).get("presentation") or {}
    generated = "criterion" in (presentation.get("ai_completion_provenance") or {})
    original_source = ("AI生成" if generated else "提供的材料") if zh else (
        "AI-generated material" if generated else "provided material"
    )
    advice = [
        ("未能可靠拆分评分项，本轮按原评分标准整体判断；所有学生使用同一解释，无需额外确认。"
         if zh else text) if text == _FALLBACK_ADVISORY else text
        for text in plan.advisories if text.strip()
    ]
    # The original rubric remains in its existing field. Remove only our own
    # envelope, keeping the model's actual shared interpretation verbatim.
    _, separator, explanation = plan.effective_criterion.partition("\n\nAI interpretation:\n")
    explanation = explanation.partition("\n\nExplicit original rules take precedence")[0].strip() if separator else ""
    fallback = _FALLBACK_ADVISORY in plan.advisories
    if fallback and zh:
        explanation = (
            "保留原标准的明确分值、方法限制、扣分、上限及有条件保底；不按学生步骤数平均分配。"
            "标准简略时按题目要求统一判断部分得分；分值冲突时在题目满分内保留原标准的相对重点。"
        )
    lines = [
        *advice,
        (f"本轮 AI 评分解释（原标准来自{original_source}；原有明确规则优先，并非教师已确认的新标准）："
         if zh else f"AI scoring interpretation for this run (original rubric from {original_source}; "
         "explicit original rules take precedence; AI additions are not teacher-confirmed):"),
        explanation,
    ]
    if not fallback:
        for route in plan.routes:
            mode = ({"points": "得分制", "deductions": "扣分制", "holistic": "整体评分"} if zh else
                    {"points": "Points awarded", "deductions": "Points deducted", "holistic": "Holistic"})[route.mode]
            items = "; ".join(
                f"{item.description}: {item.points:g} "
                + (f"分（{'原文' if item.source == 'provided' else 'AI解释'}）" if zh else
                   f"points ({'original rule' if item.source == 'provided' else 'AI interpretation'})")
                for item in route.items
            )
            lines.append(f"{mode}: {route.description} — {route.applies_when}" + (f": {items}" if items else ""))
            if route.minimum_score:
                condition = ("非空作答" if route.minimum_condition == "nonblank" else "所有作答") if zh else (
                    "nonblank answers" if route.minimum_condition == "nonblank" else "all answers"
                )
                lines.append(f"{condition}最低 {route.minimum_score:g} 分" if zh else
                             f"Minimum {route.minimum_score:g} points for {condition}.")
    follow_through = (
        "按原标准区分首次错误、沿错误中间值继续正确推导和新的独立错误，不统一规定后续得满分或半分；明确要求正确结果的评分项仍须满足。"
        if zh and plan.follow_through == _FOLLOW_THROUGH else plan.follow_through
    )
    numerical = (
        "保留题目的定义域、变量假设、单位、精度及积分常数要求；允许等价方法，除非原标准指定方法。"
        "π、e、i 可为数值常数；不自行添加容差，工具无法核验不等于答错。"
        if zh and plan.numerical_policy == _NUMERICAL_POLICY else plan.numerical_policy
    )
    lines.extend([
        follow_through, numerical,
        "如需调整，请在本题原有评分标准中修改。" if zh else
        "To change these rules, edit this question's existing rubric.",
    ])
    return [{
        "issue_id": f"math-rubric:{question.q_id}:{plan.plan_id}",
        "q_id": question.q_id, "field": "rubric", "code": "math_rubric_advisory",
        "severity": "warning", "status": "open",
        "details": {"message": "\n".join(line for line in lines if line)},
    }]


def _serialize_problem(question, *, math_plan=None, feedback_language="zh") -> dict:
    presentation = dict((question.source or {}).get("presentation") or {})
    # Older manually entered drafts used a value outside the public contract.
    # Normalize on read so their details remain usable without rewriting data.
    if presentation.get("max_score_source") == "teacher_input":
        presentation["max_score_source"] = "teacher_edited"
    max_score = float(question.max_score)
    structure = presentation.get("question_structure")
    if structure:
        structure = MajorQuestionStructureV1.model_validate(structure).model_dump()
    else:
        structure = build_major_question_structure(
            {"number": question.number, "stem": question.stem},
            major_order=question.order_index,
            structure_source="legacy_single_question",
            review_status="needs_review",
        ).model_dump()
    rubric_summary = summarize_rubric_points(
        question.criterion or "", max_score, structure
    ).model_dump()
    return {
        "q_id": question.q_id,
        "number": question.number,
        "type": question.type,
        "stem": question.stem,
        "criterion": question.criterion,
        "recognition_confidence": confidence_value(presentation.get("recognition_confidence")),
        "max_score": max_score,
        "max_score_source": presentation.get("max_score_source") or (
            "default_10" if max_score == 10 else "legacy"
        ),
        "max_score_review_status": presentation.get(
            "max_score_review_status", "needs_review"
        ),
        "question_structure": structure,
        "rubric_point_summary": rubric_summary,
        "review_status": presentation.get("review_status", "needs_review"),
        "reference_answer": question.reference_answer,
        "solution_code": presentation.get("solution_code"),
        "test_cases": question.test_cases,
        "material_provenance": presentation.get("material_provenance", {}),
        "ai_completion_provenance": presentation.get("ai_completion_provenance", {}),
        "preparation_issues": [
            *_effective_question_issues(presentation),
            *_math_rubric_advisory(question, math_plan, feedback_language),
        ],
    }


def _serialize_student_data(task_id: str, owner_id: str, submissions) -> dict[str, dict]:
    questions = assignment_repository.list_questions(task_id, teacher_id=owner_id)
    presentations = workflow_repository.list_student_presentations(task_id)
    container_source = None
    workflow = workflow_repository.get_live_workflow(task_id, owner_id=owner_id)
    if workflow.parse_job_id:
        try:
            operation = workflow_repository.get_operation(workflow.parse_job_id, owner_id=owner_id)
            if operation.operation_type == "submission_manual_review":
                container_source = (operation.checkpoint or {}).get("manual_student_container_source_id")
        except NotFound:
            pass
    revision_sources = defaultdict(list)
    for stored in file_repository.list_current_submission_originals(assignment_id=task_id, teacher_id=owner_id):
        if stored.content_type in {"application/pdf", "image/png", "image/jpeg", "image/webp", "image/bmp", "image/tiff"}:
            revision_sources[stored.submission_revision_id].append({
                "source_id": f"revision-{stored.submission_revision_id}-{stored.id}", "filename": stored.original_name})
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
                    "flag": _public_answer_flags(answer.flag, content=answer.content),
                    "recognition_confidence": _answer_recognition_confidence(answer.flag),
                    "review_status": review_statuses.get(answer.id, "pending"),
                }
                for answer in revision.answers
            ] + [{"q_id": question.q_id, "number": question.number, "type": question.type,
                  "content": "", "flag": ["recognition_manual_pending", "recognition_failed"],
                  "recognition_confidence": None, "review_status": "pending"}
                 for question in questions if question.q_id not in {answer.q_id for answer in revision.answers}],
            "source_filename": (
                presentation.source_filename if presentation else revision.file_name
            ),
            "identity_match_method": (
                presentation.identity_match_method if presentation else "filename"
            ),
            "identity_status": presentation.identity_status if presentation else "matched",
            "source_id": (revision_sources[revision.id][0]["source_id"] if revision_sources[revision.id]
                          else (presentation.source_id or container_source if presentation.identity_match_method == "manual_review" else presentation.source_id) if presentation else None),
            "source_choices": revision_sources[revision.id],
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
    for document in list_selected_documents(task_id, owner_id, include_pending=True):
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
            "status": document.status,
            "ingestion": document.ingestion_summary or {},
            "content_version": document.active_version or "legacy",
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
        checkpoint = dict(getattr(operation, "checkpoint", None) or {})
        if (
            operation.operation_type == "submission_recognition"
            and checkpoint.get("ocr_inflight_source_id")
        ):
            # A provider task may already exist. Exact replay must never
            # create a second potentially billable OCR submission.
            return False
        if (
            operation.operation_type == "question_preparation"
            and (
                checkpoint.get("base_provider_inflight_stage")
                or checkpoint.get("provider_inflight_question_ids")
            )
        ):
            # At least one per-major-question request may have reached the
            # provider without a verified result artifact. Reusing the same
            # input hash must not create another potentially billable call.
            return False
        if operation.error_code == "provider_submit_uncertain":
            return False
        return True
    if (
        operation.operation_type == "question_preparation"
        and operation.status in {"pending", "running"}
    ):
        # Published work is reclaimed by the durable worker through its lease,
        # so an HTTP replay can never erase its checkpoint or uncertain-submit
        # proof. An expired pre-publication ``preparing`` row remains safe to
        # retry because no worker or provider call can observe that status.
        return False
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
    expected_checkpoint_revision: int | None = None,
    changes: dict[str, Any] | None = None,
):
    """Lock one operation generation through an attempt-and-status CAS.

    The first write deliberately happens before related workflow/domain writes.
    A retry that increments ``attempt`` therefore cannot race an already
    validated worker and then be overwritten by that worker's ORM flush.
    """
    now = time.time()
    predicates = [
        workflow_repository.WorkflowOperationRecord.id == operation_id,
        workflow_repository.WorkflowOperationRecord.assignment_id == task_id,
        workflow_repository.WorkflowOperationRecord.owner_id == owner_id,
        workflow_repository.WorkflowOperationRecord.attempt
        == expected_operation_attempt,
        workflow_repository.WorkflowOperationRecord.status.in_(expected_statuses),
        workflow_repository._lease_write_predicate(expected_lease_token, now),
    ]
    if expected_checkpoint_revision is not None:
        predicates.append(
            workflow_repository.WorkflowOperationRecord.checkpoint_revision
            == expected_checkpoint_revision
        )
    claimed = session.execute(
        update(workflow_repository.WorkflowOperationRecord)
        .where(*predicates)
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
        if (
            expected_checkpoint_revision is not None
            and current.checkpoint_revision != expected_checkpoint_revision
        ):
            raise VersionConflict(
                "The workflow operation checkpoint changed.",
                code="stale_checkpoint_revision",
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
        workflow_repository._lock_live_assignment(
            session, assignment_id=task_id, owner_id=owner_id
        )
        session.flush()
        return expected_workflow_revision + 1


def activate_workflow_operation_atomic(
    *, task_id: str, owner_id: str, operation_id: str,
    expected_operation_attempt: int, expected_workflow_revision: int,
    operation_payload: dict[str, Any], workflow_changes: dict[str, Any],
    replacement_file_ids: tuple[str, ...] = (),
    replacement_keep_file_ids: tuple[str, ...] = (),
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
        workflow_repository._lock_live_assignment(
            session, assignment_id=task_id, owner_id=owner_id
        )
        source_storage_repository.enqueue_replaced_source_cleanup_in_session(
            session,
            assignment_id=task_id,
            owner_id=owner_id,
            producer_operation_id=operation_id,
            producer_operation_attempt=expected_operation_attempt,
            source_file_ids=replacement_file_ids,
            keep_file_ids=replacement_keep_file_ids,
        )
        return expected_workflow_revision + 1


def _question_preparation_retry_checkpoint(
    operation,
    *,
    next_attempt: int,
    next_payload: dict[str, Any],
) -> tuple[str, dict[str, Any], list[str]]:
    """Carry only verified-success checkpoint references into a new attempt.

    The artifacts remain immutable records of the attempts that created them.
    ``artifact_attempts`` is explicit lineage: the next worker must validate
    every inherited envelope against that exact prior attempt before it may
    skip provider work. Failed and in-flight units are deliberately reset.
    """

    previous_payload = dict(operation.payload or {})
    checkpoint = workflow_repository._validate_json_object(
        dict(operation.checkpoint or {}),
        field="checkpoint",
        max_bytes=workflow_repository.MAX_OPERATION_CHECKPOINT_BYTES,
    )
    if any(
        previous_payload.get(field) != next_payload.get(field)
        for field in _QUESTION_PREPARATION_RETRY_FROZEN_FIELDS
    ):
        raise ValidationError(
            "The question-preparation retry changed its frozen input contract.",
            code="question_preparation_contract_invalid",
        )
    if (
        checkpoint.get("operation_id") != operation.id
        or checkpoint.get("attempt") != operation.attempt
        or checkpoint.get("provider_record_id")
        != previous_payload.get("recognition_provider_id")
        or checkpoint.get("source_content_hashes")
        != previous_payload.get("source_content_hashes")
        or checkpoint.get("source_text_hashes")
        != previous_payload.get("source_text_hashes")
        or checkpoint.get("base_workflow_revision")
        != previous_payload.get("base_workflow_revision")
        or checkpoint.get("claimed_workflow_revision")
        != previous_payload.get("claimed_workflow_revision")
    ):
        raise ValidationError(
            "The question-preparation retry checkpoint is not frozen to its operation.",
            code="question_preparation_contract_invalid",
        )
    previous_retry_contract = checkpoint.get("retry_frozen_contract")
    if operation.attempt > 1:
        expected_retry_contract = {
            "contract_version": 1,
            "operation_id": operation.id,
            "from_attempt": operation.attempt - 1,
            "to_attempt": operation.attempt,
            "input_hash": operation.input_hash,
            "provider_record_id": previous_payload.get(
                "recognition_provider_id"
            ),
            "source_content_hashes": previous_payload.get(
                "source_content_hashes"
            ),
            "source_text_hashes": previous_payload.get(
                "source_text_hashes"
            ),
        }
        if previous_retry_contract != expected_retry_contract:
            raise ValidationError(
                "The question-preparation retry lineage changed.",
                code="question_preparation_contract_invalid",
            )
    elif previous_retry_contract is not None:
        raise ValidationError(
            "The first question-preparation attempt cannot inherit retry lineage.",
            code="question_preparation_contract_invalid",
        )
    if (
        checkpoint.get("base_provider_inflight_stage")
        or checkpoint.get("incremental_inflight_units")
        or list(checkpoint.get("provider_inflight_question_ids") or [])
        or operation.error_code == "provider_submit_uncertain"
    ):
        raise InvalidTransition(
            "The provider submission state must be verified before retry.",
            code="provider_submit_uncertain",
        )

    def question_ids(field: str) -> list[str]:
        value = checkpoint.get(field) or []
        if (
            not isinstance(value, list)
            or len(value) > 200
            or any(
                not isinstance(q_id, str)
                or re.fullmatch(r"q[1-9][0-9]{0,2}", q_id) is None
                for q_id in value
            )
            or len(value) != len(set(value))
        ):
            raise ValidationError(
                "The question-preparation retry question set is invalid.",
                code="question_preparation_contract_invalid",
            )
        return list(value)

    all_question_ids = question_ids("question_ids")
    generation_question_ids = question_ids("generation_question_ids")
    completed_question_ids = question_ids("completed_question_ids")
    failed_question_ids = question_ids("failed_question_ids")
    if (
        not set(generation_question_ids) <= set(all_question_ids)
        or not set(completed_question_ids) <= set(generation_question_ids)
        or not set(failed_question_ids) <= set(generation_question_ids)
    ):
        raise ValidationError(
            "The question-preparation retry progress is inconsistent.",
            code="question_preparation_contract_invalid",
        )

    question_artifact_ids = checkpoint.get("question_artifact_ids") or {}
    if (
        not isinstance(question_artifact_ids, dict)
        or any(
            not isinstance(q_id, str)
            or not isinstance(artifact_id, str)
            or not artifact_id
            for q_id, artifact_id in question_artifact_ids.items()
        )
        or set(question_artifact_ids) != set(completed_question_ids)
    ):
        raise ValidationError(
            "A completed retry question has no exact artifact.",
            code="question_preparation_contract_invalid",
        )

    artifact_fields = {
        field: checkpoint.get(field)
        for field in (
            "questions_extracted_artifact_id",
            "aligned_base_artifact_id",
            "final_artifact_id",
        )
    }
    if any(
        artifact_id is not None
        and (not isinstance(artifact_id, str) or not artifact_id)
        for artifact_id in artifact_fields.values()
    ):
        raise ValidationError(
            "A question-preparation retry artifact reference is invalid.",
            code="question_preparation_contract_invalid",
        )
    if question_artifact_ids and artifact_fields["aligned_base_artifact_id"] is None:
        raise ValidationError(
            "Question artifacts require a verified aligned base.",
            code="question_preparation_contract_invalid",
        )

    previous_refs = workflow_repository._validate_artifact_refs(
        list(operation.artifact_refs or [])
    )
    incremental_refs = checkpoint.get("incremental_unit_artifacts") or {}
    empty_units = checkpoint.get("incremental_empty_units") or []
    if (not isinstance(incremental_refs, dict) or not isinstance(empty_units, list)
            or any(not isinstance(key, str) or not key or not isinstance(value, str) or not value
                   for key, value in incremental_refs.items())
            or any(not isinstance(key, str) or not key for key in empty_units)):
        raise ValidationError("Invalid incremental preparation references.", code="question_preparation_contract_invalid")
    inherited_ids = {
        artifact_id
        for artifact_id in artifact_fields.values()
        if isinstance(artifact_id, str)
    } | set(question_artifact_ids.values()) | set(incremental_refs.values())
    if not inherited_ids <= set(previous_refs):
        raise ValidationError(
            "A retry artifact is outside the previous operation manifest.",
            code="question_preparation_contract_invalid",
        )

    raw_attempts = checkpoint.get("artifact_attempts") or {}
    if not isinstance(raw_attempts, dict):
        raise ValidationError(
            "Question-preparation artifact lineage is invalid.",
            code="question_preparation_contract_invalid",
        )
    artifact_attempts: dict[str, int] = {}
    for artifact_id, attempt in raw_attempts.items():
        if (
            not isinstance(artifact_id, str)
            or artifact_id not in previous_refs
            or isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or not 1 <= attempt <= operation.attempt
        ):
            raise ValidationError(
                "Question-preparation artifact lineage is invalid.",
                code="question_preparation_contract_invalid",
            )
        artifact_attempts[artifact_id] = attempt
    for artifact_id in inherited_ids:
        artifact_attempts.setdefault(artifact_id, operation.attempt)

    frozen_contract = {
        "contract_version": 1,
        "operation_id": operation.id,
        "from_attempt": operation.attempt,
        "to_attempt": next_attempt,
        "input_hash": operation.input_hash,
        "provider_record_id": previous_payload.get("recognition_provider_id"),
        "source_content_hashes": previous_payload.get("source_content_hashes"),
        "source_text_hashes": previous_payload.get("source_text_hashes"),
    }
    updates: dict[str, Any] = {
        "stage": "sources_validated",
        "retry_frozen_contract": frozen_contract,
        "artifact_attempts": artifact_attempts,
        "question_ids": all_question_ids,
        "generation_question_ids": generation_question_ids,
        "completed_question_ids": completed_question_ids,
        "failed_question_ids": [],
        "provider_inflight_question_ids": [],
        "question_error_codes": {},
        "base_provider_inflight_stage": None,
        "base_error_code": None,
        "question_artifact_ids": dict(question_artifact_ids),
    }
    updates.update(artifact_fields)
    if checkpoint.get("incremental_version") == 1:
        updates.update(incremental_version=1, incremental_unit_artifacts=dict(incremental_refs),
                       incremental_empty_units=list(empty_units), incremental_inflight_units=[],
                       incremental_incomplete_units=list(checkpoint.get("incremental_incomplete_units") or []),
                       incremental_saving_units=dict(checkpoint.get("incremental_saving_units") or {}),
                       incremental_source_invalidations=dict(checkpoint.get("incremental_source_invalidations") or {}),
                       incremental_completed_sources=list(checkpoint.get("incremental_completed_sources") or []),
                       incremental_view_from_units=bool(checkpoint.get("incremental_view_from_units")),
                       incremental_source_plans=dict(checkpoint.get("incremental_source_plans") or {}),
                       incremental_errors={}, preparation_counts=dict(checkpoint.get("preparation_counts") or {}))
    if artifact_fields["final_artifact_id"] is not None:
        updates["stage"] = "question_packages_prepared"
    elif completed_question_ids:
        updates["stage"] = "solution_units_generated"
    elif artifact_fields["aligned_base_artifact_id"] is not None:
        updates["stage"] = "uploaded_materials_aligned"
    elif artifact_fields["questions_extracted_artifact_id"] is not None:
        updates["stage"] = "questions_extracted"
    return str(updates["stage"]), updates, previous_refs


def publish_checkpointed_operation_atomic(
    *,
    task_id: str,
    owner_id: str,
    operation_type: str,
    input_hash: str,
    expected_workflow_revision: int,
    operation_payload: dict[str, Any],
    initial_checkpoint_stage: str,
    initial_checkpoint: dict[str, Any],
    artifact_refs: list[str],
    workflow_changes: dict[str, Any],
    workflow_job_id_fields: tuple[str, ...] = (),
    retry_observed_operation_id: str | None = None,
    retry_observed_attempt: int | None = None,
    replacement_file_ids: tuple[str, ...] = (),
) -> tuple[Any, bool, int]:
    """Atomically publish a complete durable operation and workflow claim.

    A retry may advance an existing generation only when the caller supplies
    the exact id and attempt it already reviewed as safe. A row discovered
    inside this transaction is always treated as an idempotent replay; this
    prevents a concurrent uncertain provider failure from being reset.
    """

    validated_payload = workflow_repository._validate_json_object(
        operation_payload,
        field="payload",
        max_bytes=workflow_repository.MAX_OPERATION_PAYLOAD_BYTES,
    )
    initial_checkpoint_stage = workflow_repository._validate_checkpoint_stage(
        initial_checkpoint_stage
    )
    assert initial_checkpoint_stage is not None
    validated_refs = workflow_repository._validate_artifact_refs(artifact_refs)
    now = time.time()
    allowed_workflow_fields = {
        column.name
        for column in workflow_repository.AssignmentWorkflowRecord.__table__.columns
        if column.name not in {
            "assignment_id",
            "owner_id",
            "created_at",
            "updated_at",
            "workflow_revision",
        }
    }
    workflow_values = {
        key: value
        for key, value in workflow_changes.items()
        if key in allowed_workflow_fields
    }
    if any(
        field not in {"active_job_id", "extract_job_id", "parse_job_id"}
        for field in workflow_job_id_fields
    ):
        raise ValidationError(
            "Unsupported workflow operation pointer.",
            code="invalid_operation_pointer",
        )
    selector = (
        workflow_repository.WorkflowOperationRecord.assignment_id == task_id,
        workflow_repository.WorkflowOperationRecord.owner_id == owner_id,
        workflow_repository.WorkflowOperationRecord.operation_type
        == operation_type,
        workflow_repository.WorkflowOperationRecord.input_hash == input_hash,
    )

    creating_new = False
    try:
        with session_scope() as session:
            # Existing producers are always locked before their workflow row,
            # matching supersede/failure/publication paths. The no-op UPDATE
            # also establishes SQLite's write gate, where FOR UPDATE is ignored.
            session.execute(
                update(workflow_repository.WorkflowOperationRecord)
                .where(*selector)
                .values(
                    updated_at=(
                        workflow_repository.WorkflowOperationRecord.updated_at
                    )
                )
            )
            operation = session.scalar(
                select(workflow_repository.WorkflowOperationRecord)
                .where(*selector)
                .with_for_update()
            )
            workflow = session.scalar(
                select(workflow_repository.AssignmentWorkflowRecord)
                .where(
                    workflow_repository.AssignmentWorkflowRecord.assignment_id
                    == task_id,
                    workflow_repository.AssignmentWorkflowRecord.owner_id
                    == owner_id,
                )
                .with_for_update()
            )
            if workflow is None:
                raise NotFound("workflow")
            workflow_repository._lock_live_assignment(
                session, assignment_id=task_id, owner_id=owner_id
            )
            retry_checkpoint_updates: dict[str, Any] = {}
            inherited_refs: list[str] = []
            if operation is not None:
                authorized_retry = (
                    retry_observed_operation_id == operation.id
                    and retry_observed_attempt == operation.attempt
                    and _operation_is_retryable(operation, now=now)
                )
                if not authorized_retry:
                    return (
                        workflow_repository._detach_operation(operation),
                        False,
                        workflow.workflow_revision,
                    )
                next_attempt = operation.attempt + 1
                if operation_type == "question_preparation":
                    (
                        initial_checkpoint_stage,
                        retry_checkpoint_updates,
                        inherited_refs,
                    ) = _question_preparation_retry_checkpoint(
                        operation,
                        next_attempt=next_attempt,
                        next_payload=validated_payload,
                    )
            else:
                creating_new = True
                next_attempt = 1
                operation = workflow_repository.WorkflowOperationRecord(
                    id=workflow_repository._new_id("op"),
                    assignment_id=task_id,
                    owner_id=owner_id,
                    operation_type=operation_type,
                    input_hash=input_hash,
                    created_at=now,
                )
                session.add(operation)

            combined_refs = workflow_repository._validate_artifact_refs(
                list(dict.fromkeys([*validated_refs, *inherited_refs]))
            )
            if combined_refs:
                matched_refs = set(session.scalars(
                    select(file_repository.StoredFileRecord.id)
                    .where(
                        file_repository.StoredFileRecord.id.in_(combined_refs),
                        file_repository.StoredFileRecord.owner_id == owner_id,
                        file_repository.StoredFileRecord.assignment_id == task_id,
                    )
                    .with_for_update()
                ))
                if matched_refs != set(combined_refs):
                    raise NotFound("stored_file")

            checkpoint = {
                **initial_checkpoint,
                **retry_checkpoint_updates,
                "operation_id": operation.id,
                "attempt": next_attempt,
            }
            validated_checkpoint = workflow_repository._validate_json_object(
                checkpoint,
                field="checkpoint",
                max_bytes=workflow_repository.MAX_OPERATION_CHECKPOINT_BYTES,
            )
            operation.attempt = next_attempt
            operation.status = "pending"
            operation.payload = validated_payload
            operation.progress = ({"workflow": "question_preparation", "stage_sequence": [],
                "preparation_counts": dict(checkpoint.get("preparation_counts") or {})}
                if operation_type == "question_preparation" and validated_payload.get("question_preparation_version") == 2 else {})
            operation.checkpoint_revision = 1
            operation.checkpoint_stage = initial_checkpoint_stage
            operation.checkpoint = validated_checkpoint
            operation.artifact_refs = combined_refs
            operation.terminal_summary = None
            operation.error_code = None
            operation.completed_at = None
            operation.expires_at = now + _OPERATION_RUNTIME_TTL_SECONDS
            operation.lease_owner = None
            operation.lease_token = None
            operation.lease_expires_at = None
            operation.lease_heartbeat_at = None
            operation.updated_at = now

            atomic_workflow_values = dict(workflow_values)
            for field in workflow_job_id_fields:
                atomic_workflow_values[field] = operation.id
            claimed = session.execute(
                update(workflow_repository.AssignmentWorkflowRecord)
                .where(
                    workflow_repository.AssignmentWorkflowRecord.assignment_id
                    == task_id,
                    workflow_repository.AssignmentWorkflowRecord.owner_id
                    == owner_id,
                    workflow_repository.AssignmentWorkflowRecord.workflow_revision
                    == expected_workflow_revision,
                    workflow_repository.AssignmentWorkflowRecord.active_operation
                    .is_(None),
                    workflow_repository.AssignmentWorkflowRecord.active_job_id
                    .is_(None),
                )
                .values(
                    **atomic_workflow_values,
                    workflow_revision=(
                        workflow_repository.AssignmentWorkflowRecord.workflow_revision
                        + 1
                    ),
                    updated_at=now,
                )
            )
            if claimed.rowcount != 1:
                session.expire_all()
                current_workflow = session.scalar(
                    select(workflow_repository.AssignmentWorkflowRecord).where(
                        workflow_repository.AssignmentWorkflowRecord.assignment_id
                        == task_id,
                        workflow_repository.AssignmentWorkflowRecord.owner_id
                        == owner_id,
                    )
                )
                if current_workflow is None:
                    raise NotFound("workflow")
                if (
                    current_workflow.active_operation is not None
                    or current_workflow.active_job_id is not None
                ):
                    raise InvalidTransition(
                        "Another workflow operation is active.",
                        code="workflow_busy",
                    )
                _raise_stale_revision()
            source_storage_repository.enqueue_replaced_source_cleanup_in_session(
                session,
                assignment_id=task_id,
                owner_id=owner_id,
                producer_operation_id=operation.id,
                producer_operation_attempt=next_attempt,
                source_file_ids=replacement_file_ids,
                keep_file_ids=tuple(combined_refs),
            )
            session.flush()
            return (
                workflow_repository._detach_operation(operation),
                True,
                expected_workflow_revision + 1,
            )
    except IntegrityError:
        # A concurrent same-input publisher may win the unique insert. It was
        # not part of the caller's reviewed retry snapshot, so replay it
        # without ever advancing its attempt.
        if not creating_new:
            raise
        operation = find_task_operation(
            task_id=task_id,
            owner_id=owner_id,
            operation_type=operation_type,
            input_hash=input_hash,
        )
        if operation is None:
            raise
        workflow = workflow_repository.get_live_workflow(
            task_id, owner_id=owner_id
        )
        return operation, False, workflow.workflow_revision


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
    workflow = workflow_repository.get_live_workflow(
        task_id, owner_id=owner_id
    )
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
            return workflow_repository.get_live_workflow(
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
            .where(assignment_repository.active_question_clause(), AssignmentQuestionRecord.assignment_id == task_id)
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
    has_draft_questions = _has_draft_questions(task_id)
    workflow = workflow_repository.get_live_workflow(
        task_id, owner_id=owner_id
    )
    replacement_file_ids = (
        source_storage_repository.replacement_source_file_ids(
            assignment_id=task_id,
            owner_id=owner_id,
            operation_id=workflow.extract_job_id,
            family="problem",
        )
        if replace_confirmed or not has_draft_questions
        else ()
    )
    base_revision = (
        workflow.workflow_revision
        if expected_workflow_revision is None
        else expected_workflow_revision
    )
    recognition_provider_id = resolve_owner_default_provider_id(owner_id, registry)
    if recognition_provider_id is None or resolve_owner_default_provider(
        owner_id,
        registry,
    ) is None:
        raise ValidationError(
            "No enabled provider is available.", code="no_provider_configured"
        )
    digest = _hash_json({
        "source": input_hash or hashlib.sha256(content).hexdigest(),
        "replace_confirmed": replace_confirmed,
        "extraction_options": extraction_options or {},
        "recognition_provider_id": recognition_provider_id,
    })
    replacement_group_id = source_storage_repository.replacement_claim_group_id(
        assignment_id=task_id,
        family="problem",
        replacement_file_ids=replacement_file_ids,
    )
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
    if has_draft_questions and not replace_confirmed:
        _raise_replacement_confirmation_required()
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
            if item.kind == "problem_source"
            and item.sha256 == source_sha256
            and item.availability_status == "available"
        ), None)
        if stored is not None:
            source_storage_repository.renew_replacement_claim_for_staged_file(
                owner_id=owner_id,
                assignment_id=task_id,
                staged_file_id=stored.id,
                replacement_file_ids=replacement_file_ids,
                replacement_group_id=replacement_group_id,
            )
        if stored is None:
            stored = file_repository.save_file(
                storage=get_storage(), owner_id=owner_id,
                kind="problem_source", original_name=filename, content=content,
                content_type=content_type or "application/octet-stream",
                assignment_id=task_id,
                replacement_file_ids=replacement_file_ids,
                replacement_group_id=replacement_group_id,
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
                "recognition_provider_id": recognition_provider_id,
            },
            workflow_changes={
                "presentation_status": "extracting_problems",
                "active_operation": "problem_extraction",
                "active_job_id": operation.id, "extract_job_id": operation.id,
                "problem_file_name": filename, "error_code": None,
                "question_recognition_provider_id": recognition_provider_id,
            },
            replacement_file_ids=replacement_file_ids,
            replacement_keep_file_ids=(stored.id,),
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
        "_recognition_provider_id": recognition_provider_id,
    }


async def run_task_problem_extraction(
    *, task_id: str, owner_id: str, job_id: str, filename: str,
    content: bytes, registry, job_attempt: int,
    claimed_workflow_revision: int,
    replace_confirmed: bool, extraction_options: dict[str, Any] | None = None,
    recognition_provider_id: str | None = None,
) -> None:
    """Run a previously claimed extraction job and durably record its outcome."""
    try:
        provider = (
            _registry_provider(registry, recognition_provider_id, owner_id)
            if recognition_provider_id
            else None
        )
        if provider is None:
            raise ValidationError(
                "No enabled provider is available.", code="no_provider_configured"
            )
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
        from backend.services.question_sources import read_question_source, attach_recognition_review
        from backend.services.stage_provider_routing import StageProviderRoute
        read = await read_question_source(
            owner_id=owner_id, task_id=task_id, content=content, filename=filename,
            route=StageProviderRoute(route_id=recognition_provider_id, kind="llm", provider=provider),
            registry=registry, extraction_hint=str((extraction_options or {}).get("extraction_hint") or ""),
            options=(extraction_options or {}).get("recognition_options"), reporter=reporter,
            text_reader=extract_text_from_upload,
        )
        text = read.text
        problem_data: dict[str, dict] = {}
        await extract_problems(
            text, provider, problem_data, reporter=reporter,
            structure_mode=str((extraction_options or {}).get("structure_mode") or "organized"),
            extraction_hint=str((extraction_options or {}).get("extraction_hint") or ""),
            confirmed_candidates=list((extraction_options or {}).get("confirmed_candidates") or []),
            manage_progress_lifecycle=False,
        )
        attach_recognition_review(problem_data, [{"recognition": read.recognition}])
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
            recognition_provider_id=recognition_provider_id,
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
    finally:
        # The operation row stores the terminal snapshot. Do not retain a
        # second in-memory copy of task/question progress after the worker.
        remove_reporter(job_id)


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
    expected_checkpoint_revision: int | None = None,
    expected_active_operation: str | None = None,
    operation_progress: dict | None = None,
    operation_checkpoint: dict | None = None,
    operation_artifact_refs: list[str] | None = None,
    operation_checkpoint_stage: str = "completed",
    recognition_provider_id: str | None = None,
    manual_recovery: bool = False,
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
                expected_statuses=("error",) if manual_recovery else ("running",),
                expected_lease_token=expected_lease_token,
                expected_checkpoint_revision=expected_checkpoint_revision,
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
        workflow = session.scalar(
            select(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id
                == task_id,
                workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
            )
            .with_for_update()
        )
        if workflow is None:
            raise NotFound("workflow")
        assignment = workflow_repository._lock_live_assignment(
            session, assignment_id=task_id, owner_id=owner_id
        )
        allowed_statuses = list(education.EDITABLE_ASSIGNMENT_STATUSES)
        if replace_confirmed:
            allowed_statuses.append(education.AssignmentStatus.PUBLISHED.value)
        if assignment.status not in allowed_statuses:
            raise InvalidTransition("assignment_not_editable")
        retained_versions = dict(session.execute(select(
            AssignmentQuestionRecord.q_id, func.max(AssignmentQuestionRecord.version)
        ).where(AssignmentQuestionRecord.assignment_id == task_id,
            AssignmentQuestionRecord.source["removed_from_review"].as_boolean().is_(True)
        ).group_by(AssignmentQuestionRecord.q_id)).all())
        existing = session.scalars(select(AssignmentQuestionRecord).where(
            assignment_repository.active_question_clause(),
            AssignmentQuestionRecord.assignment_id == task_id
        )).all()
        if existing and not replace_confirmed:
            _raise_replacement_confirmation_required()
        expected = (
            workflow.workflow_revision
            if expected_workflow_revision is None
            else expected_workflow_revision
        )
        if workflow.workflow_revision != expected:
            _raise_stale_revision()
        if expected_active_operation is not None and (
            workflow.active_operation != expected_active_operation
            or workflow.active_job_id != operation_id
        ):
            _raise_stale_revision()
        if manual_recovery:
            awaiting_count = (operation is not None
                and (operation.checkpoint or {}).get("manual_question_count_required") is True
                and workflow.presentation_status == "problems_ready"
                and workflow.extract_job_id == operation_id
                and workflow.last_failed_job_id is None and not existing)
            if operation is None or workflow.active_job_id or (
                workflow.last_failed_job_id != operation_id and not awaiting_count
            ):
                _raise_stale_revision()
        workflow.workflow_revision += 1
        workflow.presentation_status = "problems_ready"
        workflow.active_operation = None
        workflow.active_job_id = None
        workflow.problem_file_name = filename
        workflow.question_recognition_provider_id = recognition_provider_id
        workflow.parse_job_id = None
        workflow.grading_job_id = None
        workflow.last_failed_job_id = None
        workflow.submission_file_name = None
        workflow.pending_submission_file_name = None
        workflow.submission_roster_name = None
        workflow.submission_recognition_provider_id = None
        workflow.reference_file_name = None
        workflow.test_cases_file_name = None
        workflow.analysis_status = "not_generated"
        workflow.analysis_generated_at = None
        workflow.analysis_error_code = None
        workflow.error_code = None
        workflow.updated_at = now
        session.execute(delete(AssignmentQuestionRecord).where(
            assignment_repository.active_question_clause(),
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
            structure = MajorQuestionStructureV1.model_validate(
                raw.get("question_structure")
                or build_major_question_structure(
                    raw,
                    major_order=index,
                    structure_source="deterministic",
                    review_status="needs_review",
                ).model_dump()
            )
            try:
                rubric_summary = validate_rubric_points(
                    str(raw.get("criterion") or ""),
                    float(raw.get("max_score") or 10),
                    structure,
                )
            except QuestionRubricValidationError as exc:
                if manual_recovery:
                    # An incomplete saved rubric is an editable draft. Keep its
                    # literal content; existing review gates still reject it.
                    rubric_summary = exc.summary
                else:
                    raise ValidationError(
                        "Explicit subpart rubric points must add up to the major-question maximum.",
                        code=exc.summary.issue_code or "rubric_subpart_points_mismatch",
                    ) from exc
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
                    "question_structure": structure.model_dump(),
                    "rubric_point_summary": rubric_summary.model_dump(),
                    "solution_code": raw.get("solution_code"),
                    "recognition_confidence": raw.get("recognition_confidence"),
                    "material_provenance": raw.get("material_provenance", {}),
                    "ai_completion_provenance": raw.get("ai_completion_provenance", {}),
                    "preparation_issues": raw.get("preparation_issues", []),
                },
            }
            session.add(AssignmentQuestionRecord(
                id=f"q_{uuid.uuid4().hex[:12]}", assignment_id=task_id,
                q_id=str(raw.get("q_id") or q_id), order_index=index,
                number=raw["number"] if isinstance(raw.get("number"), str) else str(index + 1),
                type=raw["type"] if isinstance(raw.get("type"), str) else "其他",
                stem=str(raw.get("stem") or ""),
                criterion=str(raw.get("criterion") or ""),
                max_score=float(raw.get("max_score") or 10),
                reference_answer=raw.get("reference_answer"),
                test_cases=raw.get("test_cases"), source=source,
                version=retained_versions.get(str(raw.get("q_id") or q_id), 0) + 1, created_at=now, updated_at=now,
            ))
        assignment.status = education.AssignmentStatus.READY.value
        assignment.published_at = None
        assignment.version += 1
        assignment.updated_at = now
        if operation is not None and manual_recovery:
            # Preserve the failed attempt and its diagnostic evidence. Only the
            # teacher's workflow moves forward; this is not model success.
            operation.checkpoint = validated_checkpoint
            operation.checkpoint_revision += 1
            operation.updated_at = now
            return expected + 1
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
            operation.checkpoint_stage = operation_checkpoint_stage
            operation.checkpoint = validated_checkpoint
            operation.artifact_refs = validated_refs
            operation.terminal_summary = terminal_summary
            operation.lease_owner = None
            operation.lease_token = None
            operation.lease_expires_at = None
            operation.lease_heartbeat_at = None
        return expected + 1


def require_current_submission_pipeline(payload):
    if "manual_saved_sources" in (payload or {}) or (payload or {}).get("submission_structure_version") != 2:
        raise InvalidTransition("Saved text AI recovery has been retired.", code="saved_text_recovery_retired")


def queue_task_submission_parsing(
    *, task_id: str, owner_id: str, filename: str, content: bytes,
    content_type: str | None, registry, identity_mode: str = "filename",
    roster_entries: list[dict[str, str]] | None = None,
    roster_name: str | None = None, recognition_provider_id: str | None = None,
    replace_confirmed: bool = False,
    acknowledged_restart_from: str | None = None,
    retry_from: dict | None = None,
    expected_workflow_revision: int | None = None,
    manual_saved_sources: dict | None = None,
) -> dict:
    if manual_saved_sources is not None:
        raise InvalidTransition("Saved text AI recovery has been retired.", code="saved_text_recovery_retired")
    assignment = assignment_repository.get_assignment(task_id, actor_id=owner_id)
    questions = assignment_repository.list_questions(task_id, teacher_id=owner_id)
    if not questions:
        raise InvalidTransition("problems_required")
    question_issues = _question_review_blockers(questions)
    if question_issues:
        raise InvalidTransition(question_issues[0], code=question_issues[0])
    active_submissions = _active_submissions(task_id, owner_id)
    if active_submissions and not replace_confirmed:
        _raise_replacement_confirmation_required()
    route = resolve_stage_provider_route(
        owner_id=owner_id,
        registry=registry,
        requested_route_id=recognition_provider_id,
    )
    resolved_provider_id = route.route_id
    route_fingerprint = stage_provider_configuration_fingerprint(owner_id=owner_id, route=route, registry=registry)
    question_snapshot = [(q.id, q.version) for q in questions]
    workflow = workflow_repository.get_live_workflow(
        task_id, owner_id=owner_id
    )
    if expected_workflow_revision is not None and workflow.workflow_revision != expected_workflow_revision:
        _raise_stale_revision()
    replacement_file_ids = (
        source_storage_repository.replacement_source_file_ids(
            assignment_id=task_id,
            owner_id=owner_id,
            operation_id=workflow.parse_job_id,
            family="submission",
        )
        if replace_confirmed or not active_submissions
        else ()
    )
    digest = _hash_json({
        "submission_structure_version": 2,
        "sha256": hashlib.sha256(content).hexdigest(),
        "identity_mode": identity_mode,
        "roster": roster_entries or [],
        "provider": resolved_provider_id,
        "provider_configuration_fingerprint": route_fingerprint,
        "question_snapshot": question_snapshot,
        "replace_confirmed": replace_confirmed,
        **({"acknowledged_restart_from": acknowledged_restart_from} if acknowledged_restart_from else {}),
        **({"retry_from": retry_from} if retry_from else {}),
    })
    replacement_group_id = source_storage_repository.replacement_claim_group_id(
        assignment_id=task_id,
        family="submission",
        replacement_file_ids=replacement_file_ids,
    )
    replay = find_task_operation(
        task_id=task_id,
        owner_id=owner_id,
        operation_type="submission_recognition",
        input_hash=digest,
    )
    if replay is not None and not _operation_is_retryable(replay):
        return {
            "status": _operation_state(replay),
            "task_id": task_id,
            "job_id": replay.id,
            "workflow_revision": workflow.workflow_revision,
        }
    if replay is not None or retry_from:
        # Exact-input retry reuses registered originals and per-source results.
        # Retiring those files here would invalidate OCR caches before recovery.
        replacement_file_ids = ()
        replacement_group_id = None
    # Freeze this choice on the parent operation. Old or retried work must
    # keep its child OCR identity and any uncertain-submission checkpoint.
    timeout_profile = SOURCE_TIMEOUT_PROFILE
    if replay is not None:
        if (replay.checkpoint or {}).get("structure_inflight") and not acknowledged_restart_from:
            raise InvalidTransition("A repeated model call may be billed.", code="provider_submit_uncertain")
        timeout_profile = (replay.payload or {}).get("recognition_timeout_profile")
    elif retry_from or acknowledged_restart_from:
        if retry_from:
            origin_id, origin_attempt = retry_from.get("operation_id"), retry_from.get("attempt")
        else:
            try:
                origin_id, attempt_text = acknowledged_restart_from.rsplit(":", 1)
                origin_attempt = int(attempt_text)
            except (ValueError, AttributeError):
                raise ValidationError("Recognition inputs changed.", code="recognition_plan_changed") from None
        origin = workflow_repository.get_operation(origin_id, owner_id=owner_id)
        if (origin.assignment_id != task_id or origin.operation_type != "submission_recognition"
                or origin.attempt != origin_attempt):
            raise ValidationError("Recognition inputs changed.", code="recognition_plan_changed")
        require_current_submission_pipeline(origin.payload)
        if (origin.checkpoint or {}).get("structure_inflight") and not acknowledged_restart_from:
            raise InvalidTransition("A repeated model call may be billed.", code="provider_submit_uncertain")
        timeout_profile = (origin.payload or {}).get("recognition_timeout_profile")
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
        "submission_recognition_provider_id": resolved_provider_id,
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
            and item.availability_status == "available"
        ), None)
        if stored is not None:
            source_storage_repository.renew_replacement_claim_for_staged_file(
                owner_id=owner_id,
                assignment_id=task_id,
                staged_file_id=stored.id,
                replacement_file_ids=replacement_file_ids,
                replacement_group_id=replacement_group_id,
            )
        if stored is None:
            stored = file_repository.save_file(
                storage=get_storage(), owner_id=owner_id,
                kind=source_kind, original_name=filename,
                content=content,
                content_type=infer_upload_content_type(
                    filename, content_type, content
                ),
                assignment_id=task_id,
                replacement_file_ids=replacement_file_ids,
                replacement_group_id=replacement_group_id,
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
                "submission_structure_version": 2,
                "base_workflow_revision": workflow.workflow_revision,
                "identity_mode": identity_mode,
                "roster_entries": roster_entries or [],
                "roster_name": roster_name,
                "recognition_provider_id": resolved_provider_id,
                "recognition_timeout_profile": timeout_profile,
                "provider_configuration_fingerprint": route_fingerprint,
                "question_snapshot": question_snapshot,
                "replace_confirmed": replace_confirmed,
                **({"acknowledged_restart_from": acknowledged_restart_from} if acknowledged_restart_from else {}),
                **({"retry_from": retry_from} if retry_from else {}),
            },
            workflow_changes=workflow_changes,
            replacement_file_ids=replacement_file_ids,
            replacement_keep_file_ids=(stored.id,),
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
        "_recognition_provider_id": resolved_provider_id,
    }


def load_submission_retry_upload(
    *,
    task_id: str,
    owner_id: str,
    job_id: str,
) -> dict[str, Any]:
    """Load a failed submission upload from owner-scoped durable storage."""
    operation = workflow_repository.get_operation(job_id, owner_id=owner_id)
    if (
        operation.assignment_id != task_id
        or operation.operation_type != "submission_recognition"
    ):
        raise NotFound("submission_recognition")
    require_current_submission_pipeline(operation.payload)
    if operation.status not in {"error", "done"}:
        raise InvalidTransition(
            "Only a failed recognition operation can reuse its originals.",
            code="submission_retry_not_available",
        )
    payload = dict(operation.payload or {})
    stored = None
    for file_id in operation.artifact_refs:
        candidate = get_file(file_id=file_id, owner_id=owner_id)
        if (
            candidate is not None
            and candidate.assignment_id == task_id
            and candidate.kind == "submission_container"
        ):
            stored = candidate
            break
    if stored is None:
        sources = source_outcome_repository.list_sources(
            operation_id=operation.id,
            owner_id=owner_id,
            attempt=operation.attempt,
        )
        if len(sources) == 1:
            candidate = get_file(
                file_id=sources[0].stored_file_id,
                owner_id=owner_id,
            )
            if (
                candidate is not None
                and candidate.assignment_id == task_id
                and candidate.kind == "submission_source"
            ):
                stored = candidate
    if stored is None or stored.size_bytes > SUBMISSION_UPLOAD_MAX_BYTES:
        raise InvalidTransition(
            "The original upload is not available for retry.",
            code="submission_retry_source_unavailable",
        )
    storage = get_storage()
    with storage.open(stored.storage_key) as stream:
        content = stream.read(SUBMISSION_UPLOAD_MAX_BYTES + 1)
    if not content or len(content) > SUBMISSION_UPLOAD_MAX_BYTES:
        raise InvalidTransition(
            "The original upload is not available for retry.",
            code="submission_retry_source_unavailable",
        )
    return {
        "filename": str(payload.get("filename") or stored.original_name),
        "content": content,
        "content_type": payload.get("content_type") or stored.content_type,
        "identity_mode": str(payload.get("identity_mode") or "filename"),
        "roster_entries": list(payload.get("roster_entries") or []),
        "roster_name": payload.get("roster_name"),
        "replace_confirmed": bool(payload.get("replace_confirmed")),
        "retry_from": {"operation_id": operation.id, "attempt": operation.attempt},
    }


SUBMISSION_RESULT_ARTIFACT_MAX_BYTES = 16 * 1024 * 1024


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
    limit = SUBMISSION_RESULT_ARTIFACT_MAX_BYTES
    if artifact.availability_status != "available" or artifact.size_bytes > limit:
        raise RuntimeError("recognition_artifact_unavailable")
    with get_storage().open(artifact.storage_key) as stream:
        body = stream.read(limit + 1)
    if len(body) != artifact.size_bytes or hashlib.sha256(body).hexdigest() != artifact.sha256:
        raise RuntimeError("recognition_artifact_invalid")
    payload = json.loads(body.decode("utf-8"))
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


def _reusable_submission_results(*, owner_id, task_id, payload, questions, include_incomplete=False):
    """Load verified source results from an explicit failed-item retry lineage.

    Incomplete results are opt-in for per-question retry or manual recovery.
    Match the immutable original bytes, not a student name or model choice.
    A fresh upload has no retry_from and runs every source again.
    """
    origin = payload.get("retry_from")
    if not origin:
        return {}
    results = []
    artifacts = file_repository.list_files(owner_id=owner_id, assignment_id=task_id)
    visited = set()
    while origin:
        identity = (origin["operation_id"], int(origin["attempt"]))
        if identity in visited:
            raise ValidationError("Recognition retry cycle.", code="recognition_plan_changed")
        visited.add(identity)
        previous = workflow_repository.get_operation(identity[0], owner_id=owner_id)
        if (previous.assignment_id != task_id or previous.operation_type != "submission_recognition"
                or (previous.payload or {}).get("question_snapshot") != [[q.id, q.version] for q in questions]):
            raise ValidationError("Recognition inputs changed.", code="recognition_plan_changed")
        require_current_submission_pipeline(previous.payload)
        artifact = _submission_result_artifact(owner_id=owner_id, task_id=task_id,
            job_id=previous.id, job_attempt=identity[1])
        previous_results = []
        if artifact is not None:
            previous_results.extend(_load_submission_results(artifact))
        # A batch write can fail after each paid parse was durably saved.
        # Follow only the explicit retry lineage; unrelated runs must not be reused.
        source_files = {source.id: source.stored_file_id for source in
            source_outcome_repository.list_sources(operation_id=previous.id,
                owner_id=owner_id, attempt=identity[1])}
        for unit_artifact in ([] if artifact is not None else reversed(artifacts)):
            if unit_artifact.kind != "submission_source_parse_v1":
                continue
            if not unit_artifact.original_name.startswith(f"{previous.id}-attempt-{identity[1]}-"):
                continue
            for result in _load_submission_results(unit_artifact):
                if source_files.get(result.source_id) == result.stored_file_id:
                    previous_results.append(result)
        from backend.services.submission_incremental import invalidate_answer_units
        from backend.services.submission_source_state import load_source_states
        invalidations = {state["source_id"]: state.get("invalidated_question_ids", [])
                         for state in load_source_states(previous).values()}
        results.extend(invalidate_answer_units(result, invalidations.get(result.source_id, []))
                       for result in previous_results)
        origin = (previous.payload or {}).get("retry_from")
    reusable = {}
    for result in results:
        if result.student is None:
            continue
        stored = file_repository.get_file(file_id=result.stored_file_id, owner_id=owner_id)
        if stored is not None and stored.assignment_id == task_id:
            key = (result.filename, stored.sha256)
            newer = reusable.get(key)
            if newer is None:
                reusable[key] = result
                continue
            # Units may finish independently before the aggregate exists. Newest
            # evidence owns each question; older attempts only fill absent units.
            answers = {answer["q_id"]: answer for answer in result.student.get("stu_ans", [])}
            newer_answers = {answer["q_id"]: answer for answer in newer.student.get("stu_ans", [])}
            answers.update(newer_answers)
            # Review provenance belongs to the selected answer version, not the
            # first unit in the recovery scan or a union of superseded attempts.
            reviews = set((result.student.get("recognition") or {}).get("model_review_question_ids", [])) - set(newer_answers)
            reviews.update(set((newer.student.get("recognition") or {}).get("model_review_question_ids", [])) & set(newer_answers))
            student = {**newer.student, "stu_ans": [answers[q.q_id] for q in questions if q.q_id in answers],
                "recognition": {**(newer.student.get("recognition") or {}),
                    "model_review_question_ids": sorted(q.q_id for q in questions if q.q_id in reviews)}}
            reusable[key] = replace(newer, student=student, matched_answer_count=len(answers))
    normalized = {}
    for key, result in reusable.items():
        missing = {question.q_id for question in questions} - {
            answer.get("q_id") for answer in result.student.get("stu_ans", [])}
        if (result.student.get("recognition") or {}).get("missing_question_ids") is not None:
            result = replace(result, student={**result.student, "recognition": {
                **result.student["recognition"], "missing_question_ids": sorted(missing)}})
        incomplete = bool(missing or _submission_result_incomplete(result))
        if not include_incomplete and incomplete:
            continue
        if not incomplete and result.status in {"parse_failed", "no_matching_answer"}:
            matched = result.student.get("identity_status") == "matched"
            result = replace(result, status="parsed" if matched else "identity_conflict",
                stable_error_code=None if matched else "identity_needs_review",
                failure_phase=None if matched else "identity", retryable=False)
        normalized[key] = result
    return normalized


def _submission_result_incomplete(result):
    student = result.student or {}
    recognition = student.get("recognition") or {}
    coverage = recognition.get("coverage") or {}
    return bool(any(coverage.get(key) for key in ("failed_pages", "unprocessed_pages", "missing_targets"))
                or recognition.get("error_code")
                or recognition.get("missing_question_ids")
                or any(_answer_recognition_failures(answer.get("flag")) for answer in student.get("stu_ans", [])))


def _merge_submission_retry_answers(result, saved, target_ids, questions):
    """Carry only verified answers from this explicit retry's immutable source."""
    if saved is None:
        return result
    previous = saved.student
    current = result.student or {}
    old_answers = {answer["q_id"]: answer for answer in previous.get("stu_ans", [])}
    incoming = {answer["q_id"]: answer for answer in current.get("stu_ans", []) if answer["q_id"] in target_ids}
    answers = []
    for question in questions:
        q_id = question.q_id
        if q_id not in target_ids:
            answer = old_answers[q_id]
        elif q_id in incoming:
            answer = incoming[q_id]
            if _answer_recognition_failures(answer.get("flag")):
                old_text = str(old_answers.get(q_id, {}).get("content") or "")
                new_text = str(answer.get("content") or "")
                # Failed attempts are evidence for manual review, not replacements.
                # Keep distinct fragments in attempt order without inferring gaps;
                # merging the same checkpoint again must not duplicate its text.
                if old_text.strip():
                    if not new_text.strip() or new_text in old_text:
                        new_text = old_text
                    elif old_text not in new_text:
                        new_text = old_text + "\n\n" + new_text
                    answer = {**answer, "content": new_text}
        else:
            # A second failure must retain partially recognized text as evidence.
            answer = dict(old_answers.get(q_id) or {"q_id": q_id, "content": ""})
            answer["flag"] = list(dict.fromkeys([*answer.get("flag", []), "recognition_manual_pending"]))
        answers.append(answer)
    student = {**previous, **current, "source_id": result.source_id, "stored_file_id": result.stored_file_id,
               "source_filename": result.filename, "stu_ans": answers}
    # A successful retry supersedes old document errors; individual saved
    # answers and confirmed source identity are not regenerated by that retry.
    if result.student is not None:
        student.pop("recognition", None)
        if current.get("recognition") is not None:
            student["recognition"] = current["recognition"]
    if result.stable_error_code != "duplicate_student_identity":
        for field in ("stu_id", "stu_name", "identity_status", "identity_match_method"):
            if field in previous:
                student[field] = previous[field]
    changes = dict(student=student, student_candidate=student["stu_id"],
                   matched_answer_count=sum(not _answer_recognition_failures(answer.get("flag")) for answer in answers))
    if result.status in {"parsed", "identity_conflict"} and any(_answer_recognition_failures(answer.get("flag")) for answer in answers):
        changes.update(status="parse_failed", stable_error_code="submission_parse_failed", failure_phase="recognition", retryable=True)
    elif result.status in {"parsed", "identity_conflict"}:
        matched = student.get("identity_status") == "matched"
        changes.update(status="parsed" if matched else "identity_conflict",
                       stable_error_code=None if matched else (result.stable_error_code if result.failure_phase == "identity"
                           else saved.stable_error_code if saved.failure_phase == "identity" else "identity_needs_review"),
                       failure_phase=None if matched else "identity")
    return replace(result, **changes)


def _submission_ocr_artifact_name(
    job_id: str,
    job_attempt: int,
    source_id: str,
) -> str:
    return f"{job_id}-attempt-{job_attempt}-ocr-{source_id}.md"


def _load_submission_ocr_artifacts(
    *, owner_id: str, task_id: str, job_id: str, job_attempt: int,
) -> tuple[dict[str, str], list[str]]:
    prefix = f"{job_id}-attempt-{job_attempt}-ocr-"
    recovered: dict[str, str] = {}
    artifact_ids: list[str] = []
    for artifact in file_repository.list_files(
        owner_id=owner_id,
        assignment_id=task_id,
    ):
        if (
            artifact.kind != "submission_ocr_text"
            or not artifact.original_name.startswith(prefix)
            or not artifact.original_name.endswith(".md")
        ):
            continue
        source_id = artifact.original_name[len(prefix):-3]
        if not source_id:
            continue
        with get_storage().open(artifact.storage_key) as stream:
            body = stream.read(10 * 1024 * 1024 + 1)
        if not body or len(body) > 10 * 1024 * 1024:
            continue
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if text.strip():
            recovered[source_id] = text
            artifact_ids.append(artifact.id)
    return recovered, artifact_ids


async def run_task_submission_parsing(
    *, task_id: str, owner_id: str, job_id: str, filename: str,
    content: bytes, content_type: str | None, registry, job_attempt: int,
    identity_mode: str,
    roster_entries: list[dict[str, str]] | None, recognition_provider_id: str,
    replace_confirmed: bool, claimed_workflow_revision: int,
    leased_operation=None,
) -> None:
    reporter = get_or_create_reporter(job_id)
    current_failure_phase = "source_persistence"
    parsed_artifact = None
    document_ocr_skill = None
    try:
        await reporter.set_phase("parsing")
        await reporter.set_current_step("preparing_submission_files", message="Preparing submission files.")
        frozen_input = (leased_operation.payload or {}) if leased_operation else workflow_repository.get_operation(job_id, owner_id=owner_id).payload or {}
        require_current_submission_pipeline(frozen_input)
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
        await reporter.set_totals(students=0, questions=len(questions))
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
                # Recovery must not publish an old partial parse or dispatch
                # another paid read. Leave it failed for the explicit retry.
                results = [replace(result, status="parse_failed",
                    stable_error_code=(result.student.get("recognition") or {}).get("error_code") or "submission_parse_failed",
                    failure_phase="recognition", retryable=False)
                    if _submission_result_incomplete(result) else result for result in results]
        if results is None:
            route = resolve_stage_provider_route(
                owner_id=owner_id,
                registry=registry,
                requested_route_id=recognition_provider_id,
            )
            provider = route.provider
            route_fingerprint = stage_provider_configuration_fingerprint(owner_id=owner_id, route=route, registry=registry)
            frozen = workflow_repository.get_operation(job_id, owner_id=owner_id).payload or {}
            if (frozen.get("provider_configuration_fingerprint") not in {None, route_fingerprint}
                    or (frozen.get("question_snapshot") is not None and
                        frozen["question_snapshot"] != [[q.id, q.version] for q in questions])):
                raise ValidationError("Recognition inputs changed.", code="recognition_plan_changed")
            vision = provider if can_attempt_images(provider) else None
            source_states = {}

            async def save_source_state(source_id, stored_file_id, **changes):
                if leased_operation is None:
                    return
                from backend.services.submission_source_state import save_source_state as save
                state = {**source_states.get(stored_file_id, {}), "source_id": source_id,
                         "stored_file_id": stored_file_id, **changes}
                state_id = await run_in_threadpool(save, owner_id=owner_id, task_id=task_id,
                    operation_id=job_id, attempt=job_attempt, lease_token=leased_operation.lease_token, state=state)
                source_states[stored_file_id] = state
                checkpoint = dict(leased_operation.checkpoint_data)
                checkpoint["source_state_refs"] = {
                    **checkpoint.get("source_state_refs", {}), stored_file_id: state_id}
                await leased_operation.checkpoint(stage="reading_sources", checkpoint=checkpoint,
                    artifact_refs=leased_operation.artifact_refs)

            recovered_ocr_text: dict[str, str] = {}
            ocr_artifact_ids: list[str] = []
            blocked_source_ids: set[str] = set()
            if leased_operation is not None and route.is_baidu_ocr:
                recovered_ocr_text, ocr_artifact_ids = _load_submission_ocr_artifacts(
                    owner_id=owner_id,
                    task_id=task_id,
                    job_id=job_id,
                    job_attempt=job_attempt,
                )
                inflight = str(
                    leased_operation.checkpoint_data.get("ocr_inflight_source_id")
                    or ""
                )
                if inflight and inflight not in recovered_ocr_text:
                    blocked_source_ids.add(inflight)

            async def read_submission_source(raw, stored_file_id):
                async def save_pages(diagnostics):
                    if diagnostics is not None:
                        await save_source_state(source_contexts[(stored_file_id, raw.filename)], stored_file_id,
                            page_diagnostics=diagnostics)

                try:
                    read = await read_question_source(
                        owner_id=owner_id, task_id=task_id, content=raw.content, filename=raw.filename,
                        content_type=raw.content_type, stored_file_id=stored_file_id,
                        route=route, registry=registry, purpose="submissions", reporter=reporter,
                        retry_key=(f"{job_id}:{job_attempt}" if
                                   frozen.get("retry_from") or frozen.get("acknowledged_restart_from") else None),
                        acknowledge_pending_retry=bool(frozen.get("acknowledged_restart_from")),
                        timeout_profile=frozen.get("recognition_timeout_profile"),
                        allow_partial=True,
                        on_pages=lambda pages: on_saved_pages(raw, stored_file_id, pages),
                    )
                except DomainError as exc:
                    await save_pages((exc.details or {}).get("page_diagnostics"))
                    raise
                await save_pages((read.recognition or {}).get("page_diagnostics"))
                return read

            reusable = _reusable_submission_results(
                owner_id=owner_id, task_id=task_id, payload=frozen, questions=questions)
            retry_results = _reusable_submission_results(
                owner_id=owner_id, task_id=task_id, payload=frozen, questions=questions, include_incomplete=True)
            reused, partials, source_question_ids, source_contexts = {}, {}, {}, {}

            def reuse_source(raw, source_id, stored_file_id):
                source_contexts[(stored_file_id, raw.filename)] = source_id
                result = reusable.get((raw.filename, hashlib.sha256(raw.content).hexdigest()))
                if result is None:
                    saved = retry_results.get((raw.filename, hashlib.sha256(raw.content).hexdigest()))
                    if saved is not None:
                        good_ids = {answer["q_id"] for answer in saved.student.get("stu_ans", [])
                                    if "content" in answer and not _answer_recognition_failures(answer.get("flag"))}
                        targets = [q.q_id for q in questions if q.q_id not in good_ids]
                        # Global missing-page evidence cannot prove which answer
                        # is complete when there are no per-question boundaries.
                        if targets:
                            partials[source_id] = saved
                            source_question_ids[source_id] = targets
                    return False
                reused[source_id] = replace(result, source_id=source_id, stored_file_id=stored_file_id,
                    student={**result.student, "source_id": source_id, "stored_file_id": stored_file_id})
                return True

            from backend.services.submission_incremental import (
                invalidate_answer_units, merge_parse_repair, merge_source_answer_units, split_submission_question_units,
            )
            problem_data = {q.q_id: _serialize_problem(q) for q in questions}
            from backend.agents.question_preparation_agent import _major_question_generation_concurrency
            parse_capacity = asyncio.Semaphore(_major_question_generation_concurrency(provider) if provider else 4)
            boundary_invalidations = {}
            progress_answers, progress_students = {}, {}
            completed_sources = set()

            async def report_saved_answers(result, *, source_complete=False):
                student = result.student
                if student is not None:
                    progress_students[result.source_id] = student
                    progress_answers.setdefault(result.source_id, {}).update({
                        answer["q_id"]: answer for answer in student.get("stu_ans", [])})
                if source_complete:
                    completed_sources.add(result.source_id)
                    await reporter.set_completed_units(len(completed_sources))
                await reporter.update_stage_metrics(
                    submissions_recognized=len(progress_students),
                    identities_matched=sum(value.get("identity_status") == "matched" for value in progress_students.values()),
                    identities_needing_review=sum(value.get("identity_status") != "matched" for value in progress_students.values()),
                    answers_split=sum(len(value) for value in progress_answers.values()))
                await reporter.set_current_step("recognizing_submissions", message="Recognized submission answers saved.")

            async def durable_source_parse(source, invoke, target_ids, *, repair_index=0):
                if leased_operation is None:
                    return attach_submission_recognition([await invoke()], [source])[0]
                async with parse_capacity:
                    # Local failures have no paid dispatch to recover.
                    if source.pre_error_code or not source.text:
                        return await invoke()
                    await run_in_threadpool(source_outcome_repository.assert_source_write_fence,
                        owner_id=owner_id, assignment_id=task_id, operation_id=job_id,
                        expected_attempt=job_attempt, expected_lease_token=leased_operation.lease_token)
                    key = _hash_json(dict(version=3, operation_id=job_id, attempt=job_attempt,
                        file_id=source.stored_file_id, filename=source.filename,
                        text_sha256=hashlib.sha256(source.text.encode()).hexdigest(), questions=problem_data,
                        route=route_fingerprint, identity_mode=identity_mode, roster=roster_entries or [],
                        target_question_ids=sorted(target_ids), repair_index=repair_index))
                    child, _ = await run_in_threadpool(workflow_repository.create_operation,
                        assignment_id=task_id, owner_id=owner_id, operation_type="submission_structure_v1",
                        input_hash=key, retry_existing=False)
                    name = f"{job_id}-attempt-{job_attempt}-{child.id}-result.json"
                    saved = await run_in_threadpool(file_repository.find_latest_assignment_file,
                        owner_id=owner_id, assignment_id=task_id,
                        kind="submission_source_parse_v1", original_name_prefix=name)
                    if saved is not None:
                        restored = await run_in_threadpool(_load_submission_results, saved)
                        if len(restored) != 1 or restored[0].stored_file_id != source.stored_file_id:
                            raise RuntimeError("recognition_artifact_invalid")
                        student = None if restored[0].student is None else {
                            **restored[0].student, "source_id": source.source_id, "stored_file_id": source.stored_file_id}
                        result = replace(restored[0], source_id=source.source_id, student=student)
                        await report_saved_answers(result)
                        return result
                    worker = "submission-parser-" + uuid.uuid4().hex
                    child = await run_in_threadpool(workflow_repository.claim_operation, child.id,
                        owner_id=owner_id, worker_id=worker, lease_seconds=960)
                    try:
                        if child.checkpoint:
                            return SubmissionSourceParseResult(
                                source.source_id, source.stored_file_id, source.filename,
                                "parse_failed", None, None, 0, (), "provider_submit_uncertain",
                                "recognition", False,
                            )
                        child = await run_in_threadpool(workflow_repository.save_operation_checkpoint, child.id,
                            owner_id=owner_id, expected_attempt=child.attempt,
                            expected_checkpoint_revision=child.checkpoint_revision,
                            expected_lease_token=child.lease_token, stage="parser_pending", checkpoint={"pending": True})
                        if not route.is_baidu_ocr:
                            checkpoint = dict(leased_operation.checkpoint_data)
                            checkpoint["structure_inflight"] = {**checkpoint.get("structure_inflight", {}),
                                child.id: {"file_id": source.stored_file_id, "question_ids": sorted(target_ids)}}
                            await leased_operation.checkpoint(stage="recognizing_submissions", checkpoint=checkpoint,
                                artifact_refs=leased_operation.artifact_refs)
                        try:
                            async with asyncio.timeout(900):
                                from backend.llm.request_timeouts import response_timeout_scope
                                with response_timeout_scope(900, response_only=True) as response_scope:
                                    result = attach_submission_recognition([await invoke()], [source])[0]
                                    if response_scope.submission_may_exist:
                                        result = replace(result, status="parse_failed",
                                            stable_error_code="provider_submit_uncertain", failure_phase="recognition", retryable=False)
                        except TimeoutError:
                            # This unit may have reached its provider. Retain the
                            # explicit retry guard while independent units finish.
                            result = SubmissionSourceParseResult(source.source_id, source.stored_file_id,
                                source.filename, "parse_failed", None, None, 0, (),
                                "provider_submit_uncertain", "recognition", False)
                        result = invalidate_answer_units(result, boundary_invalidations.get(source.source_id, ()))
                        await run_in_threadpool(source_outcome_repository.assert_source_write_fence,
                            owner_id=owner_id, assignment_id=task_id, operation_id=job_id,
                            expected_attempt=job_attempt, expected_lease_token=leased_operation.lease_token)
                        saved = await run_in_threadpool(file_repository.save_file, storage=get_storage(),
                            owner_id=owner_id, assignment_id=task_id, kind="submission_source_parse_v1",
                            original_name=name, content=_serialize_submission_results([result]),
                            content_type="application/json", fence_operation_id=job_id,
                            fence_operation_attempt=job_attempt, fence_lease_token=leased_operation.lease_token)
                        await run_in_threadpool(workflow_repository.save_operation_checkpoint, child.id,
                            owner_id=owner_id, expected_attempt=child.attempt,
                            expected_checkpoint_revision=child.checkpoint_revision,
                            expected_lease_token=child.lease_token, stage="parser_saved", checkpoint={"artifact_id": saved.id},
                            artifact_refs=[saved.id], terminal_status="completed", terminal_summary={"artifact_id": saved.id})
                        checkpoint = dict(leased_operation.checkpoint_data)
                        pending = dict(checkpoint.get("structure_inflight", {}))
                        pending.pop(child.id, None)
                        checkpoint["structure_inflight"] = pending
                        await leased_operation.checkpoint(stage="recognizing_submissions", checkpoint=checkpoint,
                            # The unit's child operation owns its artifact. A
                            # parent with 500 students must not collect one ref
                            # per question and exceed the 2,048-ref limit.
                            artifact_refs=leased_operation.artifact_refs)
                        await report_saved_answers(result)
                        return result
                    finally:
                        try:
                            await asyncio.shield(run_in_threadpool(workflow_repository.release_operation, child.id,
                                owner_id=owner_id, worker_id=worker, lease_token=child.lease_token))
                        except DomainError:
                            pass


            async def parse_unit(unit, target_ids):
                from backend.tools.structured_llm import llm_attempt_budget
                pending, retained = set(target_ids), None
                previous = partials.get(unit.source_id)
                if previous is not None:
                    recognition = previous.student.get("recognition") or {}
                    coverage = recognition.get("coverage") or {}
                    # A valid JSON field does not prove a failed source page
                    # complete. Keep that case on the existing fragment path.
                    reusable_fields = [answer for answer in previous.student.get("stu_ans", [])
                        if answer["q_id"] in target_ids and any(flag.startswith("recognition_invalid_fields:")
                            for flag in answer.get("flag", []))]
                    if reusable_fields and not recognition.get("error_code") and not any(
                            coverage.get(key) for key in ("failed_pages", "unprocessed_pages", "missing_targets")):
                        retained = replace(previous, student={**previous.student, "stu_ans": reusable_fields})
                # Each response is saved before a repair is dispatched. Repairs
                # see only unfinished questions and cannot overwrite valid fields.
                with llm_attempt_budget():
                    for repair_index in range(3):
                        async def invoke():
                            parser_kwargs = dict(identity_mode=identity_mode, roster_entries=roster_entries,
                                source_question_ids={unit.source_id: list(pending)})
                            if route.is_baidu_ocr:
                                parsed = await parse_student_answer_sources_from_ocr_markdown(
                                    [unit], problem_data, **parser_kwargs)
                            else:
                                parsed = await parse_student_answer_sources([unit], problem_data, provider,
                                    single_attempt=True, independent_answers=True, **parser_kwargs)
                            return merge_parse_repair(retained, parsed[0], target_ids, problems=problem_data)
                        retained = await durable_source_parse(unit, invoke, pending, repair_index=repair_index)
                        if route.is_baidu_ocr or retained.stable_error_code == "provider_submit_uncertain":
                            break
                        pending = set(((retained.student or {}).get("recognition") or {}).get("repair_question_ids", []))
                        if not pending or not retained.retryable:
                            break
                return retained

            page_texts, incremental_tasks = {}, {}
            async def schedule_pages(source_id, stored_file_id, source_filename, source_content_type,
                                     *, complete=False, end_page=None):
                from backend.services.source_question_units import QUESTION_HEADING, source_question_units, source_page_groups
                from backend.services.question_matching import question_number_aliases, resolve_question_match
                saved_pages = page_texts.get(source_id, {})
                number_ids = {alias: q_id for q_id, aliases in question_number_aliases(problem_data).items()
                              for alias in aliases}
                targets = set(source_question_ids.get(source_id, problem_data))
                dispatched = incremental_tasks.setdefault(source_id, {})
                prefix_parts, number = [], 1
                while number in saved_pages:
                    first_heading = QUESTION_HEADING.search(saved_pages[number])
                    if first_heading:
                        prefix_parts.append(saved_pages[number][:first_heading.start()])
                        break
                    prefix_parts.append(saved_pages[number])
                    number += 1
                else:
                    # Identity/header text may arrive later than an answer page.
                    if not complete:
                        return
                prefix = "\n\n".join(prefix_parts).strip()
                page_units = source_question_units(saved_pages, known_numbers=set(number_ids), number_key=number_ids.get)
                invalid = {number_ids[unit.number] for unit in page_units if unit.closure == "repeated_heading"}
                if invalid - boundary_invalidations.get(source_id, set()):
                    boundary_invalidations.setdefault(source_id, set()).update(invalid)
                    # A later repeated heading can revoke an earlier closed
                    # unit even if that unit was already durably saved.
                    await save_source_state(source_id, stored_file_id,
                        invalidated_question_ids=sorted(boundary_invalidations[source_id]))
                for unit in source_page_groups(saved_pages, source_complete=complete,
                                               source_end_page=end_page, numbered=False, number_key=number_ids.get):
                    headings = list(QUESTION_HEADING.finditer(unit.text))
                    if any(match.group("number") not in number_ids for match in headings):
                        # Ambiguous or unknown labels cannot prove an independent
                        # answer boundary. Retain the full source for matching.
                        continue
                    matched = [resolve_question_match(match.group("number"),
                        unit.text[match.start():headings[index + 1].start() if index + 1 < len(headings) else len(unit.text)],
                        problem_data) for index, match in enumerate(headings)]
                    if not matched or any(q_id is None for q_id in matched):
                        continue  # Keep all candidates for the existing semantic parse.
                    ids = set(matched) & targets
                    # A repeated identity across a gap is not a complete
                    # independent answer. Leave it for the existing partial-source
                    # parse so every available segment remains visible for review.
                    ids -= set(dispatched) | boundary_invalidations.get(source_id, set())
                    if not unit.complete or not ids:
                        continue
                    source = PreparedSubmissionSource(source_id, stored_file_id, source_filename, source_content_type,
                        "\n\n".join(part for part in (prefix, unit.text) if part),
                        recognition={"complete_question_ids": sorted(ids), "source_pages": list(unit.pages)})
                    task = asyncio.create_task(parse_unit(source, ids))
                    dispatched.update({q_id: task for q_id in ids})

            async def on_saved_pages(raw, stored_file_id, pages):
                source_id = source_contexts[(stored_file_id, raw.filename)]
                page_texts.setdefault(source_id, {}).update({page["page_number"]: page["text"] for page in pages})
                await schedule_pages(source_id, stored_file_id, raw.filename, raw.content_type)

            async def parse_ready_source(source):
                targets = set(source_question_ids.get(source.source_id, problem_data))
                coverage = (source.recognition or {}).get("coverage") or {}
                complete = not (source.pre_error_code or (source.recognition or {}).get("error_code") or
                                coverage.get("failed_pages") or coverage.get("unprocessed_pages"))
                requested = coverage.get("requested_pages") or []
                await schedule_pages(source.source_id, source.stored_file_id, source.filename, source.content_type,
                    complete=complete, end_page=max(requested) if requested else coverage.get("total_pages"))
                dispatched = incremental_tasks.get(source.source_id, {})
                remaining = targets - set(dispatched)
                # PDFs use saved page groups. Other input formats retain their
                # existing conservative, explicitly labelled source handling.
                units = ([(source, remaining)] if source.source_id in page_texts else
                         split_submission_question_units(source, problem_data, remaining)) if remaining else []
                unit_tasks = [*dict.fromkeys(dispatched.values()), *[
                    asyncio.create_task(parse_unit(unit, targets)) for unit, targets in units]]
                try:
                    values = await asyncio.gather(*unit_tasks)
                finally:
                    for task in unit_tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*unit_tasks, return_exceptions=True)
                result = merge_source_answer_units(values, source,
                    {q_id: problem_data[q_id] for q_id in problem_data if q_id in targets})
                result = _merge_submission_retry_answers(result, partials.get(source.source_id), targets, questions)
                invalid = boundary_invalidations.get(source.source_id, set())
                result = invalidate_answer_units(result, invalid)
                previous = partials.get(source.source_id)
                verified = (set(dispatched) | {answer["q_id"] for answer in
                    (previous.student.get("stu_ans", []) if previous else [])
                    if not _answer_recognition_failures(answer.get("flag"))}) - invalid
                result = attach_submission_recognition([result], [source], verified_question_ids=verified)[0]
                # Count a source once after all of its units and repairs finish,
                # including failed outcomes; a saved page alone is not a file.
                await report_saved_answers(result, source_complete=True)
                return result

            source_tasks = {}
            async def on_source_ready(source):
                if source.source_id not in reused:
                    # Start structure extraction now, while the next independent
                    # file is still being read. Each complete unit saves itself.
                    source_tasks[source.source_id] = asyncio.create_task(parse_ready_source(source))
                else:
                    await report_saved_answers(reused[source.source_id], source_complete=True)
                await reporter.set_current_step("recognizing_submissions",
                    message="Saving answers as each submission becomes available.")

            try:
                all_sources = await prepare_submission_sources(
                    content=content, filename=filename, content_type=content_type,
                    owner_id=owner_id, task_id=task_id, job_id=job_id, job_attempt=job_attempt,
                    operation_lease_token=leased_operation.lease_token if leased_operation is not None else None,
                    ocr_skill=None, recognition_reader=read_submission_source, reuse_source=reuse_source,
                    recovered_ocr_text_by_source=recovered_ocr_text, blocked_ocr_source_ids=blocked_source_ids,
                    vision_unavailable_code=None if route.is_baidu_ocr or vision is not None else "provider_vision_not_supported",
                    reporter=reporter, on_source_ready=on_source_ready,
                    source_concurrency=_major_question_generation_concurrency(provider) if provider else 4,
                )
                current_failure_phase = "recognition"
                values = await asyncio.gather(*source_tasks.values())
            finally:
                pending_tasks = [*source_tasks.values(), *[
                    task for units in incremental_tasks.values() for task in units.values()]]
                for task in pending_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending_tasks, return_exceptions=True)
            by_source = {**reused, **{result.source_id: result for result in values}}
            results = normalize_submission_source_identities([by_source[source.source_id] for source in all_sources])
            await reporter.set_current_step("consolidating_submission_results", message="Saving submission results.")
            if leased_operation is not None:
                current_failure_phase = "outcome_persistence"
                aggregate = _serialize_submission_results(results)
                # The aggregate is a read optimization; independently saved
                # units remain authoritative. Never create an aggregate that
                # the bounded recovery reader would subsequently reject.
                if len(aggregate) <= SUBMISSION_RESULT_ARTIFACT_MAX_BYTES:
                    parsed_artifact = file_repository.save_file(
                        storage=get_storage(), owner_id=owner_id, kind="submission_recognition_result",
                        original_name=f"{job_id}-attempt-{job_attempt}-parsed.json",
                        content=aggregate, content_type="application/json", assignment_id=task_id,
                        fence_operation_id=leased_operation.operation_id,
                        fence_operation_attempt=leased_operation.attempt,
                        fence_lease_token=leased_operation.lease_token)
                await leased_operation.checkpoint(
                    stage="submissions_parsed",
                    checkpoint={**leased_operation.checkpoint_data,
                        "parsed_artifact_id": parsed_artifact.id if parsed_artifact else None},
                    artifact_refs=list(dict.fromkeys([
                        *leased_operation.artifact_refs,
                        *([parsed_artifact.id] if parsed_artifact else []),
                    ])),
                )

        # Reconcile both a fresh run and an aggregate restored after a worker
        # restart. Use final normalized identities, not pre-merge candidates.
        recognized = [result for result in results if result.student is not None]
        await reporter.set_totals(students=len(results), questions=len(questions))
        await reporter.set_completed_units(len(results))
        await reporter.update_stage_metrics(files_total=len(results), files_processed=len(results),
            submissions_recognized=len(recognized),
            identities_matched=sum(result.student.get("identity_status") == "matched" for result in recognized),
            identities_needing_review=sum(result.student.get("identity_status") != "matched" for result in recognized),
            answers_split=sum(result.matched_answer_count for result in results),
            parse_failures=sum(result.status in {"parse_failed", "no_matching_answer"} for result in results))

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
                expected_attempt=job_attempt,
                expected_lease_token=(
                    leased_operation.lease_token
                    if leased_operation is not None
                    else None
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
        if not students or summary.failed_count or summary.pending_count:
            # A different file's earlier error must not hide an uncertain paid
            # request from the explicit-retry acknowledgement boundary.
            code = "provider_submit_uncertain" if any(
                result.stable_error_code == "provider_submit_uncertain" for result in results
            ) else next(
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
    finally:
        remove_reporter(job_id)
        if document_ocr_skill is not None:
            close = getattr(document_ocr_skill.client, "aclose", None)
            if close is not None:
                await close()


async def run_durable_submission_recognition(operation) -> None:
    """Recover one persisted submission archive and commit it under its lease."""
    try:
        require_current_submission_pipeline(operation.payload)
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
        "review_status", "solution_code", "material_provenance", "recognition_confidence",
        "ai_completion_provenance", "preparation_issues",
        "max_score_source", "max_score_review_status",
    }
    with session_scope() as session:
        operation = _cas_operation_attempt_for_write(
            session, task_id=task_id, owner_id=owner_id,
            operation_id=operation_id,
            expected_operation_attempt=expected_operation_attempt,
            expected_statuses=(required_operation_status,),
            expected_lease_token=expected_lease_token,
        )
        if operation.expires_at is not None and operation.expires_at <= now:
            raise InvalidTransition("The workflow job expired.", code="stale_revision")

        workflow = session.scalar(
            select(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id
                == task_id,
                workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
            )
            .with_for_update()
        )
        if workflow is None:
            raise NotFound("workflow")
        if workflow.workflow_revision != expected_workflow_revision:
            _raise_stale_revision()
        assignment = workflow_repository._lock_live_assignment(
            session, assignment_id=task_id, owner_id=owner_id
        )
        if assignment.status not in education.EDITABLE_ASSIGNMENT_STATUSES:
            raise InvalidTransition("assignment_not_editable")

        questions = session.scalars(select(AssignmentQuestionRecord).where(
            assignment_repository.active_question_clause(),
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
            current_presentation = dict(
                (question.source or {}).get("presentation") or {}
            )
            if "stem" in fields:
                structure = build_major_question_structure(
                    {
                        "number": question.number,
                        "stem": fields["stem"],
                    },
                    major_order=question.order_index,
                    review_status="needs_review",
                )
            else:
                structure = MajorQuestionStructureV1.model_validate(
                    current_presentation.get("question_structure")
                    or build_major_question_structure(
                        {"number": question.number, "stem": question.stem},
                        major_order=question.order_index,
                        structure_source="legacy_single_question",
                        review_status="needs_review",
                    ).model_dump()
                )
            try:
                rubric_summary = validate_rubric_points(
                    str(fields.get("criterion", question.criterion) or ""),
                    fields.get("max_score", question.max_score),
                    structure,
                )
            except QuestionRubricValidationError as exc:
                raise ValidationError(
                    "Explicit subpart rubric points must add up to the major-question maximum.",
                    code=exc.summary.issue_code or "rubric_subpart_points_mismatch",
                ) from exc
            presentation_updates["question_structure"] = structure.model_dump()
            presentation_updates["rubric_point_summary"] = rubric_summary.model_dump()
            if require_missing:
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

        workflow.workflow_revision += 1
        workflow.error_code = None
        workflow.updated_at = now
        if operation.operation_type in _AUXILIARY_QUESTION_OPERATION_TYPES:
            workflow.presentation_status = "problems_ready"
        if workflow.active_job_id == operation_id:
            workflow.active_job_id = None
            workflow.active_operation = None

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
            # A successful completion/import repairs only the supplied target;
            # unrelated recognition failures remain evidence for teacher review.
            filled = {key for key, value in fields.items()
                      if value and (not isinstance(value, str) or value.strip())}
            issues = _effective_question_issues(presentation)
            for issue in issues:
                target = (issue.get("details") or {}).get("target") or {
                    "rubric": "criterion", "answer": "reference_answer", "stem": "stem",
                }.get(issue.get("field"))
                if issue.get("code") == "generation_failed" and target in filled:
                    issue.update(status="resolved", severity="warning")
                if issue.get("code") == "manual_completion_required":
                    remaining = [field for field in (issue.get("details") or {}).get("required_fields", [])
                                 if field not in filled]
                    issue["details"] = {**(issue.get("details") or {}), "required_fields": remaining}
                    if not remaining:
                        issue.update(status="resolved", severity="warning")
            presentation["preparation_issues"] = issues
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
        workflow = session.scalar(
            select(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id == task_id,
                workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
            )
            .with_for_update()
        )
        if workflow is None:
            raise NotFound("workflow")
        if (
            workflow.workflow_revision != expected_workflow_revision
            or workflow.active_job_id != operation_id
        ):
            _raise_stale_revision()
        workflow_repository._lock_live_assignment(
            session, assignment_id=task_id, owner_id=owner_id
        )
        workflow.active_job_id = None
        workflow.active_operation = None
        workflow.error_code = None
        if operation.operation_type in _AUXILIARY_QUESTION_OPERATION_TYPES:
            workflow.presentation_status = "problems_ready"
        workflow.updated_at = now
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
    manual_recovery: bool = False,
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
        workflow = session.scalar(
            select(workflow_repository.AssignmentWorkflowRecord)
            .where(
                workflow_repository.AssignmentWorkflowRecord.assignment_id
                == task_id,
                workflow_repository.AssignmentWorkflowRecord.owner_id == owner_id,
            )
            .with_for_update()
        )
        if workflow is None:
            raise NotFound("workflow")
        count_handoff = False
        if manual_recovery:
            previous = session.get(workflow_repository.WorkflowOperationRecord, workflow.parse_job_id) if workflow.parse_job_id else None
            count_handoff = (operation is not None and previous is not None
                and (operation.payload or {}).get("manual_origin_id") == previous.id
                and previous.owner_id == owner_id and previous.assignment_id == task_id
                and previous.operation_type == "submission_manual_review" and previous.status == "done"
                and (previous.checkpoint or {}).get("manual_student_count_required") is True
                and workflow.presentation_status == "submissions_ready" and workflow.last_failed_job_id is None)
            if (operation is None or operation.operation_type != "submission_manual_review"
                    or workflow.active_job_id or (not count_handoff
                    and workflow.last_failed_job_id != (operation.payload or {}).get("manual_origin_id"))):
                raise InvalidTransition("Manual recognition recovery is no longer current.", code="workflow_busy")
        if (
            expected_workflow_revision is not None
            and workflow.workflow_revision != expected_workflow_revision
        ):
            _raise_stale_revision()
        assignment = workflow_repository._lock_live_assignment(
            session, assignment_id=task_id, owner_id=owner_id
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
        if assignment.status not in (
            *education.EDITABLE_ASSIGNMENT_STATUSES,
            education.AssignmentStatus.PUBLISHED.value,
        ):
            raise InvalidTransition("assignment_not_open")

        questions = session.scalars(
            select(AssignmentQuestionRecord).where(
                assignment_repository.active_question_clause(),
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
        if count_handoff and active_presentation is not None:
            raise InvalidTransition("Student rows already exist; edit them in review.")
        if active_presentation is not None and not replace_existing:
            _raise_replacement_confirmation_required()

        if expected_workflow_revision is not None:
            workflow.workflow_revision += 1
            workflow.presentation_status = "submissions_ready"
            workflow.active_operation = None
            workflow.active_job_id = None
            workflow.submission_file_name = submission_file_name
            workflow.pending_submission_file_name = None
            if manual_recovery:
                workflow.parse_job_id = operation.id
            workflow.grading_job_id = None
            workflow.last_failed_job_id = None
            workflow.analysis_status = "not_generated"
            workflow.analysis_generated_at = None
            workflow.analysis_error_code = None
            workflow.error_code = None
            workflow.updated_at = now

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
                            ("parsed", "identity_conflict", "parse_failed", "no_matching_answer")
                            if manual_recovery else ("parsed", "identity_conflict")
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
            # This is a task-local identity, not an authentication user. Keep
            # the stable ID for retries/history; names and school IDs already
            # live in AssignmentStudentPresentationRecord below.

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

            answers = list(student.get("stu_ans") or [])
            present_ids = {str(answer.get("q_id") or "") for answer in answers}
            # An omitted model item is not evidence that the student left it blank.
            answers.extend({"q_id": q_id, "content": "", "flag": ["recognition_manual_pending"]}
                           for q_id in question_map if q_id not in present_ids)
            for raw in answers:
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
        workflow = workflow_repository.get_live_workflow(
            task_id, owner_id=owner_id
        )
    except NotFound:
        return payload
    _source_summary, source_rows = _submission_source_projection(workflow, owner_id)
    progress: dict | None = None
    active_operation_status: str | None = None
    if workflow.active_job_id:
        if workflow.active_operation == "grading":
            run = grading_repository.get_run(workflow.active_job_id, actor_id=owner_id)
            active_operation_status = "pending" if run.status == "queued" else run.status
        else:
            try:
                operation = workflow_repository.get_operation(
                    workflow.active_job_id, owner_id=owner_id
                )
                progress = dict(operation.progress or {}) or None
                if progress is not None:
                    progress.setdefault("job_id", operation.id)
                active_operation_status = operation.status
            except NotFound:
                progress = None
    elif workflow.extract_job_id or workflow.parse_job_id:
        job_id = (workflow.last_failed_job_id if payload["status"] in {
            "problems_paused", "submissions_paused",
        } else workflow.parse_job_id or workflow.extract_job_id)
        if job_id:
            try:
                operation = workflow_repository.get_operation(job_id, owner_id=owner_id)
                # Manual recovery retains failed recognition for audit, not as
                # the current task's progress after its failure marker is cleared.
                if operation.status != "error" or workflow.last_failed_job_id == job_id:
                    progress = dict(operation.progress or {}) or None
                    if progress is not None:
                        progress.setdefault("job_id", operation.id)
            except NotFound:
                pass
    if progress and payload["status"] in {"problems_paused", "submissions_paused"}:
        progress.update(phase="paused", error_detail=None, model_waits=[], active=[])
    payload.update({
        "progress": progress,
        "recovering_saved_text": False,
        "active_job_id": workflow.active_job_id,
        "active_operation": workflow.active_operation,
        "active_operation_status": active_operation_status,
        "submission_sources": source_rows,
    })
    payload["queue_reason"] = None
    if active_operation_status == "pending":
        from backend.db.execution_admission import owner_is_running
        with session_scope() as session:
            payload["queue_reason"] = "user_busy" if owner_is_running(
                session, owner_id, exclude_id=workflow.active_job_id,
            ) else "server_busy"
    _project_recognition_progress(payload)
    return payload


def _project_recognition_progress(payload):
    progress = payload.get("progress")
    if progress is not None and (payload.get("status") in {"extracting_problems", "parsing_submissions"}
            or payload.get("active_operation") in {"question_preparation", "problem_extraction", "submission_recognition"}
            or (isinstance(progress, dict) and isinstance(progress.get("job_id"), str) and progress["job_id"] in {
                payload.get("extract_job_id"), payload.get("parse_job_id")
            } - {None})):
        from backend.progress.recognition_activity import public_progress
        payload["progress"] = public_progress(progress, terminal=payload.get("status") == "error" or payload.get("active_operation_status") == "pending")


async def async_task_state(*, task_id: str, owner_id: str) -> dict:
    payload = await asyncio.to_thread(task_state, task_id=task_id, owner_id=owner_id)
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
        and (payload.get("status") in {"grading", "grading_paused"} or grading_owns_failure)
    ):
        grading_progress = await asyncio.to_thread(_grading_progress, grading_job_id, owner_id)
        # Borrow only a deduplicated running count from live units. Student,
        # skill and internal step identifiers must stay server-side. Phase
        # stays durable because the reporter finishes before persistence.
        if (reporter := get_reporter(grading_job_id)) is not None:
            live = (await reporter.snapshot()).model_dump(mode="json")
            if grading_progress.get("phase") == "grading":
                grading_progress["running_units"] = len({
                    (unit["student_id"], unit["q_id"]) for unit in live.get("active", [])
                })
            live_completed = live.get("completed_units")
            if (
                isinstance(live_completed, int)
                and not isinstance(live_completed, bool)
                and live_completed > grading_progress.get("completed_units", 0)
            ):
                grading_progress["completed_units"] = live_completed
        # A reporter can reach ``done`` before result persistence finishes.
        # Prefer the durable failed-run projection so clients never render a
        # stale success phase after the database marks the run failed.
        if payload.get("status") in {"grading", "grading_paused"} or grading_owns_failure:
            payload["progress"] = grading_progress
            payload["error"] = payload.get("error") or grading_progress.get("error_detail")
            if payload.get("status") == "grading":
                payload["active_job_id"] = grading_job_id
                payload["active_operation"] = "grading"
    _project_recognition_progress(payload)
    return payload


def _grading_progress(run_id: str, owner_id: str) -> dict:
    from backend.progress.grading_activity import public_models, public_event, public_waits, model_eta_seconds
    run = grading_repository.get_run(run_id, actor_id=owner_id)
    paused = run.status == "cancelled" and run.error_message == "operation_cancelled"
    events = grading_repository.list_events(run_id, actor_id=owner_id)
    model_waits = next((item.get("payload", {}).get("model_waits", [])
                        for item in reversed(events) if "model_waits" in item.get("payload", {})), [])
    question_count = len(assignment_repository.get_questions_by_assignment(run.assignment_id))
    completed_units = run.completed_submissions * question_count
    saved_pairs = set()
    from backend.services.grading_checkpoints import saved_experts
    from backend.models import ExpertResult
    from backend.agents.multi_expert import _is_successful_expert
    for item in saved_experts(run_id):
        if _is_successful_expert(ExpertResult.model_validate(item["result"])):
            saved_pairs.add((item["student_id"], item["q_id"]))
    for result in grading_repository.list_results_for_run(run_id):
        if result.effective_score is not None:
            saved_pairs.add((result.student_id, result.q_id))
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
        if item["message"] in {"grading_started", "grading_progress", "run_completed", "run_failed"}
    ]
    models = next((item["payload"]["model_progress"] for item in reversed(events)
                   if "model_progress" in item.get("payload", {})), [])
    latest = next((event for item in reversed(events) if (event := public_event(item))), None)
    # Older run payloads are also projected through a field allowlist. Internal
    # active-unit names and free-form messages never cross the grading boundary.
    safe_waits = public_waits(model_waits)
    models = public_models(models, terminal=run.status in {"completed", "partial_failed", "failed", "cancelled"})
    return {
        "contract_version": 1, "job_id": run_id,
        "phase": "paused" if paused else "done" if run.status in {"completed", "partial_failed"} else (
            "error" if run.status in {"failed", "cancelled"} else "grading"
        ),
        "total_students": run.total_submissions,
        "total_questions": question_count,
        "completed_units": completed_units,
        "saved_units": len(saved_pairs),
        "active": [], "messages": messages,
        "model_waits": safe_waits if run.status == "running" else [],
        "model_progress": models,
        "model_eta_seconds": model_eta_seconds(models, now=time.time()) if run.status == "running" else None,
        "activity_latest": latest,
        "activity_cursor": events[-1]["sequence"] if events else 0,
        "running_units": 0,
        "error_detail": None if paused else _grading_failure_code(run),
        "started_at": run.started_at or run.created_at,
        "workflow": "grading", "stage_sequence": [],
        "current_step": "completed" if run.status in {"completed", "partial_failed"} else "grading",
        "total_steps": None, "completed_steps": None, "stage_metrics": {},
    }


def grading_activity_history(*, task_id: str, owner_id: str, run_id: str, after: int, limit: int) -> dict:
    from backend.progress.grading_activity import public_event
    workflow_repository.get_live_workflow(task_id, owner_id=owner_id)
    run = grading_repository.get_run(run_id, actor_id=owner_id)
    if run.assignment_id != task_id:
        raise NotFound("grading_run")
    rows = grading_repository.list_events(run_id, actor_id=owner_id, after_sequence=after, limit=limit + 1)
    page = rows[:limit]
    return {"run_id": run_id,
            "events": [event for row in page if (event := public_event(row))],
            "cursor": page[-1]["sequence"] if page else after,
            "has_more": len(rows) > limit}


def grading_readiness(
    *,
    task_id: str,
    owner_id: str,
) -> dict[str, list[str] | bool]:
    """Separate unusable inputs from review advice for preflight and grading."""
    workflow = workflow_repository.get_live_workflow(
        task_id, owner_id=owner_id
    )
    questions = assignment_repository.get_questions_by_assignment(task_id)
    submissions = _active_submissions(task_id, owner_id)
    presentations = workflow_repository.list_student_presentations(task_id)
    source_summary, source_rows = _submission_source_projection(workflow, owner_id)

    issues: list[str] = []
    warnings: list[str] = []
    if not questions:
        issues.append("questions_required")
    issues.extend(_question_review_blockers(questions))
    if not submissions:
        issues.append("submissions_required")
    if workflow.active_operation and workflow.active_operation != "grading":
        issues.append("workflow_busy")

    recognition_review_ids = []
    repaired_answer_ids = []
    if submissions:
        # Older tasks can have complete normalized questions and answers without
        # the source-evidence rows introduced by the newer ingestion workflow.
        # Missing evidence alone must not force a teacher to upload the same
        # files again. Pending/failed recognition still blocks grading, while
        # usable inputs awaiting teacher review remain available for grading.
        if source_summary["pending"]:
            issues.append("submission_sources_pending")
        if source_summary["failed"]:
            issues.append("submission_sources_failed")
        if source_summary.get("manual_review"):
            issues.append("submission_recognition_blocked")
        if source_summary["identity_needs_review"]:
            issues.append("submission_identities_unresolved")

        for submission in submissions:
            revision = None
            if submission.current_revision_id:
                try:
                    revision = submission_repository.get_revision(
                        revision_id=submission.current_revision_id,
                        actor_id=owner_id,
                    )
                except NotFound:
                    pass
            if revision is None or not revision.answers:
                issues.append("answers_required")
            elif revision is not None:
                if {question.q_id for question in questions} - {answer.q_id for answer in revision.answers}:
                    issues.append("submission_recognition_blocked")
                recognition_review_ids.extend(answer.id for answer in revision.answers
                                              if confidence_needs_attention(_answer_recognition_confidence(answer.flag)))
                for answer in revision.answers:
                    if _answer_recognition_failures(answer.flag):
                        issues.append("submission_recognition_blocked")
                    if _REQUIRED_ANSWER_CONFIRMATION_FLAGS.intersection(answer.flag or []):
                        repaired_answer_ids.append(answer.id)

            presentation = presentations.get(submission.student_id)
            # ``source_id`` was added after legacy structured submissions were
            # already persisted.  A legacy presentation can therefore retain
            # ``needs_review`` even though its normalized answers are usable.
            # Only source-backed presentation conflicts are current evidence.
            # Actual source-backed conflicts need explicit teacher resolution.
            if (
                presentation is not None
                and (
                    _student_identity_invalid(presentation.display_student_id, presentation.display_name)
                    or (presentation.source_id is not None and presentation.identity_status != "matched")
                )
            ):
                issues.append("submission_identities_unresolved")

    review_ids = recognition_review_ids + repaired_answer_ids
    reviewed = workflow_repository.answer_review_statuses(review_ids) if review_ids else {}
    if any(reviewed.get(answer_id) != "confirmed" for answer_id in repaired_answer_ids):
        issues.append("submission_recognition_blocked")
    if any(reviewed.get(answer_id) != "confirmed" for answer_id in recognition_review_ids):
        warnings.append("submission_recognition_needs_review")
    if any(row.get("unknown_question_ids") for row in source_rows):
        warnings.append("submission_question_ids_unmatched")
    ordered_issues = list(dict.fromkeys(issues))
    return {
        "ready": not ordered_issues,
        "blocking_issues": ordered_issues,
        "warnings": list(dict.fromkeys(warnings)),
    }


_FAILED_ANSWER_REVIEW_FLAGS = frozenset({"recognition_failed", "parse_failed", "no_matching_answer", "recognition_manual_pending"})
_REQUIRED_ANSWER_CONFIRMATION_FLAGS = frozenset({
    "recognition_repaired_needs_confirmation",
})


def _answer_recognition_failures(flags):
    """Recognize persisted page failures as well as the original fixed flags."""
    return {flag for flag in (flags or []) if flag in _FAILED_ANSWER_REVIEW_FLAGS
            or flag.startswith(("recognition_failed_pages:", "recognition_unprocessed_pages:", "recognition_invalid_fields:"))}


def _answer_recognition_confidence(flags):
    for flag in flags or []:
        if flag.startswith("answer_recognition_confidence:"):
            try:
                return confidence_value(float(flag.split(":", 1)[1]))
            except ValueError:
                pass
    return None


def _metadata_only_recognition_fields(flags, content):
    """A teacher may confirm verified text when only bounded metadata failed."""
    flags = list(flags or [])
    fields = {field for flag in flags if flag.startswith("recognition_invalid_fields:")
              for field in flag.split(":", 1)[1].split(",")}
    if (not str(content or "").strip() or not fields
            or not fields <= {"number", "type", "flag", "recognition_confidence"}
            or any(flag in {"recognition_failed", "parse_failed", "no_matching_answer",
                            "recognition_incomplete", "recognition_paused", "provider_submit_uncertain"}
                   or flag.startswith(("recognition_failed_pages:", "recognition_unprocessed_pages:"))
                   for flag in flags)):
        return set()
    return fields


def _public_answer_flags(flags, *, content=None):
    # Existing clients understand recognition_failed. Preserve the precise
    # source evidence too; projecting it must not mutate the stored answer.
    result = [flag for flag in flags or []
              if not flag.startswith(("recognition_confidence:", "answer_recognition_confidence:"))
              and flag not in {"recognition_metadata_review", "recognition_low_confidence", "recognition_very_low_confidence",
                               "low_confidence", "very_low_confidence", "recognition_needs_review"}]
    if _metadata_only_recognition_fields(flags, content):
        result.append("recognition_metadata_review")
    if _answer_recognition_failures(result) and "recognition_failed" not in result:
        result.append("recognition_failed")
    return result


def _effective_question_issues(presentation):
    issues = []
    for issue in presentation.get("preparation_issues", []):
        coverage = (issue.get("details") or {}).get("coverage") or {}
        # Old source scores/labels were broadcast to every question. They are
        # not per-question assessments; keep actual failure evidence visible,
        # including incomplete question fragments from saved-text recovery.
        if (issue.get("code") in {"recognition_partial", "recognition_needs_review"}
                and not (issue.get("code") == "recognition_partial" and issue.get("severity") == "blocking")
                and (issue.get("details") or {}).get("confidence_scope") != "question"
                and not (issue.get("details") or {}).get("error_code")
                and not (issue.get("details") or {}).get("incomplete")
                and not any(coverage.get(key) for key in ("failed_pages", "unprocessed_pages", "missing_targets"))):
            continue
        if (issue.get("field") != "answer" and issue.get("code") in {"recognition_partial", "recognition_needs_review"} and issue.get("status") != "resolved"
                and ((issue.get("details") or {}).get("error_code")
                     or (issue.get("details") or {}).get("incomplete")
                     or any(coverage.get(key) for key in ("failed_pages", "unprocessed_pages", "missing_targets")))):
            issue = {**issue, "severity": "blocking", "status": "open"}
        if issue.get("code") == "manual_completion_required":
            issue = {**issue, "details": {**(issue.get("details") or {}), "required_fields": [
                field for field in (issue.get("details") or {}).get("required_fields", []) if field in {"stem", "criterion"}]}}
        if issue.get("field") == "answer" or (issue.get("code") == "generation_failed" and issue.get("field") == "programming_tests") or (
            issue.get("code") in {"low_confidence", "recognition_needs_review"}
            and not (issue.get("details") or {}).get("error_code")
            and not any(coverage.get(key) for key in ("failed_pages", "unprocessed_pages", "missing_targets"))
        ):
            issue = {**issue, "severity": "warning"}
        issues.append(issue)
    return issues


def _question_review_blockers(questions) -> list[str]:
    issues = []
    for question in questions:
        presentation = (question.source or {}).get("presentation") or {}
        review_issues = _effective_question_issues(presentation)
        if presentation.get("review_status") != "confirmed" and any(
            question_issue_requires_confirmation(issue, presentation) for issue in review_issues
        ):
            issues.append("question_review_blocked")
        if (any(issue.get("code") == "manual_completion_required" and (issue.get("details") or {}).get("required_fields") for issue in review_issues)
                and (presentation.get("review_status") != "confirmed"
                     or _manual_question_fields_missing(question, presentation))):
            issues.append("question_manual_completion_required")
        problem = _serialize_problem(question)
        if (not str(question.stem or "").strip() or not str(question.criterion or "").strip()
                or not problem["rubric_point_summary"]["is_valid"]
                or any(issue.get("severity") == "blocking"
                       and (issue.get("status") == "open"
                            or presentation.get("review_status") != "confirmed")
                       for issue in review_issues)):
            issues.append("question_review_blocked")
    return list(dict.fromkeys(issues))


def _student_identity_invalid(student_id: str, student_name: str) -> bool:
    # Only the ingestion default is reserved. A teacher's edited, nonempty
    # value (including "Unknown Student") must not be guessed to be a placeholder.
    return any(not str(value or "").strip() or str(value).strip() == "[Unknown Student]"
               for value in (student_id, student_name))


def _manual_question_fields_missing(question, presentation, patch=None):
    patch = patch or {}
    fields = {field for issue in presentation.get("preparation_issues", [])
              if issue.get("code") == "manual_completion_required"
              for field in (issue.get("details") or {}).get("required_fields", []) if field in {"stem", "criterion"}}
    for field in fields:
        value = patch.get(field, presentation.get(field) if field == "solution_code" else getattr(question, field, None))
        if not value or (isinstance(value, str) and not value.strip()):
            return True
    return False


def start_task_grading(
    *,
    task_id: str,
    owner_id: str,
    expected_workflow_revision: int,
    request_id: str | None = None,
    retry_scope: str = "full_batch",
) -> dict:
    workflow = workflow_repository.get_live_workflow(
        task_id, owner_id=owner_id
    )
    workflow = _reconcile_terminal_active_operation(
        task_id=task_id, owner_id=owner_id, workflow=workflow
    )
    # Legacy callers retain revision-based deduplication. New clients give one
    # click a stable id, independent of the reusable input/configuration hash.
    explicit_request = request_id is not None
    request_id = request_id or f"workflow-{expected_workflow_revision}"
    replay = grading_repository.bind_grading_request(
        assignment_id=task_id, teacher_id=owner_id, request_id=request_id,
        request_revision=expected_workflow_revision,
    )
    if replay is not None:
        if replay.status in {"queued", "running"} and (
            workflow.grading_job_id != replay.id or workflow.active_job_id != replay.id
        ):
            try:
                workflow_repository.bind_existing_active_grading_run(
                    task_id, owner_id=owner_id, run_id=replay.id,
                )
            except NotFound:
                # A fast worker may finish between binding the request and
                # repairing a legacy pointer. The durable intent still wins.
                replay = grading_repository.bind_grading_request(
                    assignment_id=task_id, teacher_id=owner_id, request_id=request_id,
                    request_revision=expected_workflow_revision,
                )
        return _grading_request_response(task_id, replay)
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
        bound = grading_repository.bind_grading_request(
            assignment_id=task_id, teacher_id=owner_id, request_id=request_id,
            request_revision=expected_workflow_revision, run_id=active.id,
        )
        return _grading_request_response(task_id, bound)
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
    assert_grading_routes_supported(setup)
    from backend.services.grading_input_security import (
        provider_configuration_fingerprint,
    )

    frozen_revision_ids = tuple(sorted(
        submission.current_revision_id
        for submission in submissions
        if submission.current_revision_id is not None
    ))
    source_file_ids = source_storage_repository.grading_source_file_ids(
        assignment_id=task_id,
        owner_id=owner_id,
        problem_operation_id=workflow.extract_job_id,
        submission_operation_id=workflow.parse_job_id,
        frozen_revision_ids=frozen_revision_ids,
    )
    from backend.knowledge.snapshots import freeze_documents
    knowledge_ids = sorted(_selected_knowledge(task_id, owner_id)) if setup.knowledge_scope != "none" else []
    knowledge_versions = freeze_documents(owner_id, knowledge_ids)
    input_manifest = {
        "questions": [question.model_dump(mode="json") for question in questions],
        "submission_revision_ids": list(frozen_revision_ids),
        "source_file_ids": list(source_file_ids),
        "knowledge_document_ids": knowledge_ids,
        "knowledge_content_versions": knowledge_versions,
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
    from backend.services.math_grading_context import (
        MATH_GRADING_ENGINE_VERSION, problem_from_question, uses_math_grading,
    )
    math_qids = {question.q_id for question in questions if uses_math_grading(question.type)}
    if math_qids:
        from backend.services.math_rubric_plan import math_rubric_fingerprint
        input_manifest["math_grading_engine_version"] = MATH_GRADING_ENGINE_VERSION
        input_manifest["math_rubric_input_fingerprints"] = {
            question.q_id: math_rubric_fingerprint(problem_from_question(question))
            for question in questions if question.q_id in math_qids
        }
    run_fingerprint = _hash_json({
        "grading_setup": workflow.grading_setup,
        "input_manifest": input_manifest,
    })
    latest = runs[-1] if runs else None
    if latest and latest.status == "completed" and not (explicit_request and retry_scope == "full_batch"):
        frozen = workflow_repository.get_run_setup(latest.id)
        if (frozen is not None and frozen.fingerprint == run_fingerprint
                and (not math_qids or
                     (frozen.input_manifest or {}).get("math_grading_engine_version") == MATH_GRADING_ENGINE_VERSION)):
            cached = grading_repository.bind_grading_request(
                assignment_id=task_id, teacher_id=owner_id, request_id=request_id,
                request_revision=expected_workflow_revision, run_id=latest.id,
            )
            if cached.status in {"queued", "running"}:
                return _grading_request_response(task_id, cached)
            return {"status": "already_done", "task_id": task_id, "job_id": latest.id}
    if retry_scope == "failed_only":
        previous = workflow_repository.get_run_setup(latest.id) if latest else None
        if previous is None or latest.status not in {"partial_failed", "failed", "completed", "cancelled"}:
            raise ValidationError("No failed grading run to retry.", code="grading_retry_unavailable")
        old = previous.input_manifest or {}
        # Review metadata can change without changing the grading inputs.
        def content_questions(items):
            keys = ("id", "q_id", "number", "type", "stem", "criterion", "max_score", "reference_answer", "test_cases")
            return [{**{key: q.get(key) for key in keys},
                     "solution_code": (q.get("source") or {}).get("presentation", {}).get("solution_code")}
                    for q in items]
        stable_keys = ("submission_revision_ids", "knowledge_document_ids", "knowledge_content_versions")
        if (previous.setup != workflow.grading_setup
                or any(old.get(key) != input_manifest.get(key) for key in stable_keys)
                or content_questions(old.get("questions", [])) != content_questions(input_manifest["questions"])):
            raise VersionConflict("Grading inputs changed; review the setup before starting a new run.", code="grading_inputs_changed")
        from backend.services.grading_checkpoints import saved_experts, saved_math_plans, teacher_review_snapshots
        # A failed-only retry keeps the actual shared rules, rather than asking
        # a model to redistribute the same question's points. Old-engine or
        # missing-plan mathematical results are rerun in this new run.
        plans = saved_math_plans(latest.id, questions=questions) if math_qids else {}
        previous_results = grading_repository.list_results_for_run(latest.id)
        reusable = [result.model_dump(mode="json") for result in previous_results
                    if result.effective_score is not None and math.isfinite(result.effective_score)
                    and (result.q_id not in math_qids or result.q_id in plans)
                    and ("grading_paused" not in result.initial_review_reasons or result.teacher_review)]
        if len(reusable) == len(questions) * len(frozen_revision_ids):
            return {"status": "already_done", "task_id": task_id, "job_id": latest.id}
        input_manifest.update(retry_from_run_id=latest.id, reused_results=reusable, retry_scope="failed_only")
        input_manifest["teacher_review_overrides"] = teacher_review_snapshots(
            old, previous_results, questions=questions, revision_ids=frozen_revision_ids)
        if math_qids:
            input_manifest["math_rubric_plans"] = {
                qid: plan.model_dump(mode="json") for qid, plan in plans.items()
            }
        # Changing a provider still permits the established failed-item retry,
        # but partial expert votes from its previous configuration cannot mix in.
        input_manifest["reused_experts"] = (saved_experts(latest.id)
            if old.get("provider_configuration_fingerprint") == input_manifest.get("provider_configuration_fingerprint") else [])
        if math_qids:
            from backend.services.math_grading_context import expert_matches_plan
            input_manifest["reused_experts"] = [
                item for item in input_manifest["reused_experts"]
                if expert_matches_plan(item, math_qids, plans)
            ]
    # A new explicit full-batch intent reruns even identical inputs. HTTP
    # replays still bind to the original run above; failed-only retries reuse
    # valid scores, and legacy callers retain fingerprint-based reuse.
    input_manifest.update(
        grading_request_id=request_id,
        grading_request_revision=expected_workflow_revision,
        grading_recovery_policy="manual_retry",
    )
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
        replay = grading_repository.bind_grading_request(
            assignment_id=task_id, teacher_id=owner_id, request_id=request_id,
            request_revision=expected_workflow_revision,
        )
        if replay is not None:
            return _grading_request_response(task_id, replay)
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
    if run.status not in {"queued", "running"}:
        return _grading_request_response(task_id, run)
    return {"status": "started", "task_id": task_id, "job_id": run.id}


def _grading_request_response(task_id, run):
    if run.status in {"queued", "running"}:
        return {"status": "already_running", "task_id": task_id, "job_id": run.id}
    return {
        "status": "already_finished",
        "task_id": task_id, "job_id": run.id, "run_status": run.status,
    }


def task_results(*, task_id: str, owner_id: str) -> dict:
    task = get_task(task_id=task_id, owner_id=owner_id, full=True)
    workflow = workflow_repository.get_live_workflow(
        task_id, owner_id=owner_id
    )
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    run = _current_grading_run(workflow, runs)
    if run is None:
        return {"status": task["status"], "task_id": task_id}
    if run.status not in {"completed", "partial_failed", "cancelled"}:
        return {
            "status": "not_found" if run.status == "failed" else task["status"],
            "task_id": task_id,
            "error": _grading_failure_code(run),
        }
    results = grading_repository.list_results_for_run(run.id, include_missing=True)
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
        "grading_run_id": run.id, "grading_run_status": run.status,
        "retry_scope": "failed_only" if run.status == "partial_failed" else None,
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
        "confidence": result.ai_confidence,
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
    workflow = workflow_repository.get_live_workflow(
        task_id, owner_id=owner_id
    )
    workflow = _reconcile_terminal_active_operation(
        task_id=task_id, owner_id=owner_id, workflow=workflow
    )
    if workflow.active_job_id:
        raise InvalidTransition("The task is busy.", code="workflow_busy")
    now = time.time()
    substantive = bool({
        "number", "type", "stem", "criterion", "max_score", "reference_answer",
        "solution_code", "test_cases",
    }.intersection(patch))
    with session_scope() as session:
        workflow_row = workflow_repository.lock_workflow_revision(
            session, assignment_id=task_id, owner_id=owner_id, expected_revision=expected_revision,
        )
        assignment = workflow_repository._lock_live_assignment(
            session, assignment_id=task_id, owner_id=owner_id
        )
        if workflow_row.active_job_id:
            raise InvalidTransition("The task is busy.", code="workflow_busy")
        question = session.scalar(select(AssignmentQuestionRecord).where(
            assignment_repository.active_question_clause(),
            AssignmentQuestionRecord.assignment_id == task_id,
            AssignmentQuestionRecord.q_id == q_id,
        ))
        if question is None:
            raise NotFound("question")
        source = dict(question.source or {})
        presentation = dict(source.get("presentation") or {})
        presentation["preparation_issues"] = _effective_question_issues(presentation)
        original_problem = _serialize_problem(question)
        repaired_required_content = (
            not str(question.stem or "").strip()
            or not original_problem["rubric_point_summary"]["is_valid"]
        )
        manual_markers = [issue for issue in presentation.get("preparation_issues", [])
                          if issue.get("code") == "manual_completion_required"]
        protected_markers = [issue for issue in presentation.get("preparation_issues", [])
                            if question_issue_requires_confirmation(issue, presentation)]
        for key in (
            "review_status", "solution_code", "material_provenance",
            "ai_completion_provenance", "preparation_issues",
        ):
            if key in patch:
                presentation[key] = patch[key]
        if "preparation_issues" in patch and protected_markers:
            protected_ids = {issue.get("issue_id") for issue in protected_markers}
            presentation["preparation_issues"] = [
                issue for issue in presentation["preparation_issues"]
                if issue.get("issue_id") not in protected_ids
            ] + protected_markers
        if manual_markers:
            presentation["preparation_issues"] = [issue for issue in presentation.get("preparation_issues", [])
                if issue.get("code") != "manual_completion_required"] + manual_markers
            if substantive and patch.get("review_status") != "confirmed":
                presentation["review_status"] = "needs_review"
                presentation["preparation_issues"] = [
                    {**issue, "status": "open"} if issue.get("code") == "manual_completion_required" else issue
                    for issue in presentation["preparation_issues"]]
        fixed_fields = {field for key, field in {"stem": "stem", "reference_answer": "answer", "criterion": "rubric", "test_cases": "programming_tests", "solution_code": "programming_tests"}.items()
                        if patch.get(key) and (patch[key].strip() if isinstance(patch[key], str) else patch[key])
                        != (original_problem[key].strip() if isinstance(original_problem.get(key), str) else original_problem.get(key))}
        if fixed_fields:
            presentation["preparation_issues"] = [
                {**issue, "status": "resolved"} if issue.get("status") == "open"
                and issue.get("field") in fixed_fields
                and (not (issue.get("details") or {}).get("target")
                     or patch.get(issue["details"]["target"]))
                and issue.get("code") in {"parse_anomaly", "generation_failed", "recognition_partial", "recognition_needs_review", "low_confidence"}
                else issue for issue in presentation.get("preparation_issues", [])
            ]
        if patch.get("review_status") == "confirmed":
            fields = {"stem", "answer", "rubric", "programming_tests", "source", "max_score"}
            issues = presentation.get("preparation_issues", [])
            if _manual_question_fields_missing(question, presentation, patch) or not str(patch.get("stem", question.stem) or "").strip() or not str(patch.get("criterion", question.criterion) or "").strip() or any(
                issue.get("status") == "open" and issue.get("severity") == "blocking"
                for issue in issues
            ):
                raise ValidationError("Fix the failed recognition or required material before confirming.", code="question_review_blocked")
            if "max_score" in fields:
                presentation["max_score_review_status"] = "confirmed"
            presentation["preparation_issues"] = [
                {**issue, "status": "acknowledged"}
                if issue.get("status") == "open" and issue.get("field") in fields else issue
                for issue in issues
            ]
            presentation["review_status"] = (
                "confirmed" if presentation.get("max_score_review_status") == "confirmed"
                and not any(issue.get("status") == "open" for issue in presentation["preparation_issues"])
                else "needs_review"
            )
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
        if "number" in patch:
            number = str(patch["number"] or "").strip()
            if not number or len(number) > 64:
                raise ValidationError("Question number is required.", code="invalid_question_number")
            patch["number"] = number
        if "type" in patch:
            question_type = str(patch["type"] or "").strip()
            if not question_type or len(question_type) > 64:
                raise ValidationError("Question type is required.", code="invalid_question_type")
            patch["type"] = question_type
        if "stem" in patch or "number" in patch:
            structure = build_major_question_structure(
                {"number": patch.get("number", question.number), "stem": patch.get("stem", question.stem)},
                major_order=question.order_index,
                structure_source="deterministic",
                review_status="needs_review",
            )
        else:
            structure = MajorQuestionStructureV1.model_validate(
                presentation.get("question_structure")
                or build_major_question_structure(
                    {"number": question.number, "stem": question.stem},
                    major_order=question.order_index,
                    structure_source="legacy_single_question",
                    review_status="needs_review",
                ).model_dump()
            )
        try:
            rubric_summary = validate_rubric_points(
                str(patch.get("criterion", question.criterion) or ""),
                patch.get("max_score", question.max_score),
                structure,
            )
        except QuestionRubricValidationError as exc:
            raise ValidationError(
                "Explicit subpart rubric points must add up to the major-question maximum.",
                code=exc.summary.issue_code or "rubric_subpart_points_mismatch",
            ) from exc
        presentation["question_structure"] = structure.model_dump()
        presentation["rubric_point_summary"] = rubric_summary.model_dump()
        if repaired_required_content and substantive and patch.get("review_status") != "confirmed":
            presentation["review_status"] = "needs_review"
            presentation.setdefault("preparation_issues", []).append({
                "issue_id": f"required-repair-{question.q_id}-{question.version}",
                "q_id": question.q_id, "field": "stem", "severity": "blocking",
                "status": "resolved", "code": "required_review_confirmation",
            })
        source["presentation"] = presentation
        for key in (
            "number", "type", "stem", "criterion", "max_score", "reference_answer", "test_cases"
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
            workflow_row.analysis_generated_at = None
            workflow_row.analysis_error_code = None
            workflow_row.error_code = None
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
    workflow = workflow_repository.get_live_workflow(
        task_id, owner_id=owner_id
    )
    workflow = _reconcile_terminal_active_operation(
        task_id=task_id, owner_id=owner_id, workflow=workflow
    )
    if workflow.active_job_id:
        raise InvalidTransition("The task is busy.", code="workflow_busy")
    now = time.time()
    target_payload: dict[str, Any] | None = None
    with session_scope() as session:
        workflow_row = workflow_repository.lock_workflow_revision(
            session, assignment_id=task_id, owner_id=owner_id, expected_revision=expected_revision,
        )
        assignment = workflow_repository._lock_live_assignment(
            session, assignment_id=task_id, owner_id=owner_id
        )
        if workflow_row.active_job_id:
            raise InvalidTransition("The task is busy.", code="workflow_busy")
        presentation = session.scalar(
            select(workflow_repository.AssignmentStudentPresentationRecord).where(
                workflow_repository.AssignmentStudentPresentationRecord.assignment_id
                == task_id,
                workflow_repository.AssignmentStudentPresentationRecord.display_student_id
                == display_student_id,
                workflow_repository.AssignmentStudentPresentationRecord.is_active.is_(True),
            )
        )
        if presentation is None:
            retired = session.scalar(select(workflow_repository.AssignmentStudentPresentationRecord.id).where(
                workflow_repository.AssignmentStudentPresentationRecord.assignment_id == task_id,
                workflow_repository.AssignmentStudentPresentationRecord.is_active.is_(False),
                (workflow_repository.AssignmentStudentPresentationRecord.display_student_id == display_student_id)
                | (workflow_repository.AssignmentStudentPresentationRecord.student_id == display_student_id),
            ).limit(1))
            if retired is not None:
                raise NotFound("submission")
        internal_id = presentation.student_id if presentation else display_student_id
        submission = session.scalar(select(SubmissionRecord).where(
            SubmissionRecord.assignment_id == task_id,
            SubmissionRecord.student_id == internal_id,
        ))
        if submission is None or submission.current_revision_id is None:
            raise NotFound("submission")
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
        question = session.scalar(select(AssignmentQuestionRecord).where(
            assignment_repository.active_question_clause(),
            AssignmentQuestionRecord.assignment_id == task_id,
            AssignmentQuestionRecord.q_id == q_id,
        ))
        if question is None:
            raise NotFound("answer")
        missing_target = target is None
        if missing_target:
            # Historical imports may predate placeholders. Only the explicit
            # teacher edit below persists this missing known question.
            target = SubmissionAnswerRecord(id=f"pending-{question.id}", question_id=question.id,
                q_id=q_id, number=question.number, type=question.type, content="",
                flag=["recognition_manual_pending"])
            answer_rows.append(target)

        # Confirming recognition is metadata-only. Keep the submission revision,
        # grading generation and analysis intact when the recognized text/flags
        # did not change (including an unchanged save from the editor).
        content_changed = patch.get("content") is not None and patch["content"] != target.content
        failed_flags = _FAILED_ANSWER_REVIEW_FLAGS | _answer_recognition_failures(target.flag)
        original_failures = _answer_recognition_failures(target.flag)
        metadata_fields = (_metadata_only_recognition_fields(target.flag, target.content)
                           if patch.get("review_status") == "confirmed" else set())
        manual_blank = (bool(original_failures)
                        and "manual_blank_confirmed" in (patch.get("flag") or [])
                        and patch.get("content") is not None and not str(patch["content"]).strip()
                        and patch.get("review_status") == "confirmed")
        if manual_blank:
            patch = {**patch, "content": ""}
            content_changed = target.content != ""
        if metadata_fields and not content_changed:
            # Confirm metadata without rewriting already verified student work.
            repaired_flags = failed_flags | {"recognition_metadata_review"}
            patch = {**patch, "flag": [flag for flag in target.flag or [] if flag not in repaired_flags]}
        elif manual_blank or (content_changed and str(patch.get("content") or "").strip()
                and str(patch["content"]).strip() != str(target.content or "").strip()):
            # A teacher's replacement text repairs recognition failures while
            # preserving unrelated source/authorship observations.
            repaired_flags = failed_flags | {"recognition_manual_pending", "recognition_paused", "recognition_incomplete"}
            patch = {**patch, "flag": [flag for flag in (patch.get("flag", target.flag) or []) if flag not in repaired_flags]}
            if original_failures and patch.get("review_status") != "confirmed":
                patch["flag"] = list(dict.fromkeys([
                    *patch["flag"], "recognition_repaired_needs_confirmation",
                ]))
        elif original_failures and patch.get("flag") is not None:
            # Flag edits/one-click confirmation cannot erase missing evidence.
            patch = {**patch, "flag": list(dict.fromkeys([
                *patch["flag"], *sorted(original_failures),
            ]))}
        if (_REQUIRED_ANSWER_CONFIRMATION_FLAGS.intersection(target.flag or [])
                and patch.get("review_status") != "confirmed" and patch.get("flag") is not None):
            patch["flag"] = list(dict.fromkeys([
                *patch["flag"], *sorted(_REQUIRED_ANSWER_CONFIRMATION_FLAGS.intersection(target.flag or [])),
            ]))
        if patch.get("flag") is not None:
            patch = {**patch, "flag": [*_public_answer_flags(patch["flag"]),
                                       *[flag for flag in target.flag or [] if flag.startswith("answer_recognition_confidence:")]]}
        flags_changed = patch.get("flag") is not None and list(patch["flag"]) != list(target.flag or [])
        if patch.get("review_status") == "confirmed" and any(
            flag in failed_flags
            for flag in (patch.get("flag", target.flag) or [])
        ):
            raise ValidationError("Retry or correct the failed recognition before confirming.", code="answer_review_blocked")
        if not missing_target and not content_changed and not flags_changed and patch.get("review_status") is not None:
            review = session.get(
                workflow_repository.SubmissionAnswerPresentationRecord, target.id
            )
            if review is None:
                review = workflow_repository.SubmissionAnswerPresentationRecord(answer_id=target.id)
                session.add(review)
            review.review_status = str(patch["review_status"])
            review.updated_at = now
            workflow_row.workflow_revision += 1
            workflow_row.updated_at = now
            session.flush()
            return {
                "status": "ok", "stu_id": display_student_id, "q_id": q_id,
                "answer": {
                    "q_id": target.q_id, "number": target.number, "type": target.type,
                    "content": target.content, "flag": _public_answer_flags(target.flag, content=target.content),
                    "recognition_confidence": _answer_recognition_confidence(target.flag),
                    "review_status": review.review_status,
                },
                "workflow_revision": workflow_row.workflow_revision,
            }

        if assignment.status != education.AssignmentStatus.PUBLISHED.value:
            raise InvalidTransition("assignment_closed", code="assignment_closed")
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
        # Carry per-answer review statuses into the new revision. The copy
        # below mints fresh answer rows; without this, confirming one answer
        # would silently reset every sibling answer back to "pending"
        # (2026-08-28 "按下葫芦浮起瓢" fix).
        old_answer_ids = [answer.id for answer in answer_rows]
        carried_review_statuses = {
            row.answer_id: row.review_status
            for row in session.scalars(
                select(workflow_repository.SubmissionAnswerPresentationRecord)
                .where(
                    workflow_repository.SubmissionAnswerPresentationRecord.answer_id.in_(
                        old_answer_ids
                    )
                )
            ).all()
        } if old_answer_ids else {}
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
            number = question.number if is_target and "number" in metadata_fields else answer.number
            answer_type = question.type if is_target and "type" in metadata_fields else answer.type
            answer_id = f"ans_{uuid.uuid4().hex[:12]}"
            session.add(SubmissionAnswerRecord(
                id=answer_id,
                revision_id=new_revision.id,
                question_id=answer.question_id,
                q_id=answer.q_id,
                number=number,
                type=answer_type,
                content=content,
                flag=list(flags or []),
                created_at=now,
            ))
            if is_target:
                target_answer_id = answer_id
                target_payload = {
                    "q_id": answer.q_id,
                    "number": number,
                    "type": answer_type,
                    "content": content,
                    "flag": _public_answer_flags(flags, content=content),
                    "recognition_confidence": _answer_recognition_confidence(flags),
                    "review_status": patch.get("review_status") or "pending",
                }
                next_review_status = str(patch.get("review_status") or "pending")
            else:
                next_review_status = carried_review_statuses.get(answer.id)
            if next_review_status is not None:
                session.add(workflow_repository.SubmissionAnswerPresentationRecord(
                    answer_id=answer_id,
                    review_status=next_review_status,
                    updated_at=now,
                ))
        assert target_answer_id is not None and target_payload is not None
        submission.current_revision_id = new_revision.id
        submission.updated_at = now

        # The answer edit and downstream invalidation are one transaction: a
        # stale browser cannot create a new current revision without also
        # detaching the obsolete grading generation.
        workflow_row.presentation_status = "submissions_ready"
        workflow_row.grading_job_id = None
        workflow_row.last_failed_job_id = None
        workflow_row.analysis_status = "not_generated"
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
    workflow = workflow_repository.get_live_workflow(
        task_id, owner_id=owner_id
    )
    _reconcile_terminal_active_operation(
        task_id=task_id, owner_id=owner_id, workflow=workflow
    )
    normalized_display_id = new_display_id.strip()
    normalized_display_name = new_display_name.strip()
    if not normalized_display_id or not normalized_display_name:
        raise ValidationError(
            "Student ID and name are required.", code="student_identity_required",
        )
    if _student_identity_invalid(normalized_display_id, normalized_display_name):
        raise ValidationError(
            'Replace the default placeholder "[Unknown Student]" before confirming the identity.',
            code="student_identity_placeholder",
        )
    # Serialize identity confirmation/editing with other workflow mutations.
    # A failed or busy item must not consume a revision during bulk review.
    with session_scope() as session:
        workflow_row = workflow_repository.lock_workflow_revision(
            session, assignment_id=task_id, owner_id=owner_id, expected_revision=expected_revision,
        )
        workflow_repository._lock_live_assignment(
            session, assignment_id=task_id, owner_id=owner_id
        )
        if workflow_row.active_job_id:
            raise InvalidTransition("The task is busy.", code="workflow_busy")
        presentations = session.scalars(
            select(workflow_repository.AssignmentStudentPresentationRecord).where(
                workflow_repository.AssignmentStudentPresentationRecord.assignment_id == task_id,
                workflow_repository.AssignmentStudentPresentationRecord.is_active.is_(True),
            )
        ).all()
        presentation = next(
            (item for item in presentations if item.display_student_id == current_display_id), None,
        )
        if presentation is None:
            raise NotFound("student")
        if any(item.display_student_id == normalized_display_id
               and item.student_id != presentation.student_id for item in presentations):
            raise ValidationError(
                "The student ID is already used in this task.", code="student_identity_conflict",
            )
        presentation.display_student_id = normalized_display_id
        presentation.display_name = normalized_display_name
        presentation.identity_status = "matched"
        presentation.updated_at = time.time()
        workflow_row.workflow_revision += 1
        workflow_row.updated_at = presentation.updated_at
        session.flush()
        workflow_revision = workflow_row.workflow_revision
    task = get_task(task_id=task_id, owner_id=owner_id, full=True)
    return {
        "status": "ok", "previous_student_id": current_display_id,
        "student": task["student_data"][normalized_display_id],
        "workflow_revision": workflow_revision,
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
    workflow = workflow_repository.get_live_workflow(
        task_id, owner_id=owner_id
    )
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
            analysis_status="not_generated",
            analysis_generated_at=None, analysis_error_code=None,
        )
    rows = grading_repository.list_results_for_run(target_run.id)
    row = next((item for item in rows if item.student_id == internal_id and item.q_id == q_id), None)
    if row is None:
        row = grading_repository.ensure_missing_result_for_review(
            target_run.id, teacher_id=owner_id, student_id=internal_id, q_id=q_id)
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


def _correction_review_blocks_finalization(result) -> bool:
    """A replacement for an unusable AI score must be explicitly confirmed."""
    def valid_score(value):
        return (value is not None and math.isfinite(value)
                and 0 <= value <= result.ai_max_score)

    return (
        result.result_status in education.NON_SCOREABLE_RESULT_STATUSES
        or not valid_score(result.effective_score)
        or (not valid_score(result.ai_score)
            and (result.teacher_review or {}).get("confirmed") is not True)
    )


def _available_artifact_manifests(*, task_id: str, owner_id: str) -> list:
    # Retired formats must not reappear in either current or historical reports.
    return [
        record for record in workflow_repository.list_artifact_manifests(task_id, owner_id=owner_id)
        if is_current_artifact_manifest(record.manifest)
    ]


def finalization(*, task_id: str, owner_id: str) -> dict:
    workflow = workflow_repository.get_live_workflow(
        task_id, owner_id=owner_id
    )
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    run = _current_grading_run(workflow, runs)
    remaining = []
    required_review_count = 0
    confirmed_required_count = 0
    if run is not None:
        for result in grading_repository.list_results_for_run(run.id, include_missing=True):
            if (_correction_review_blocks_finalization(result)
                    or result.ai_score is None):
                required_review_count += 1
                review = result.teacher_review or {}
                if (
                    result.result_status
                    not in education.NON_SCOREABLE_RESULT_STATUSES
                    and review.get("confirmed") is True
                ):
                    confirmed_required_count += 1
            if _correction_review_blocks_finalization(result):
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
    reports = _available_artifact_manifests(task_id=task_id, owner_id=owner_id)
    has_analysis = any(record.result_version == workflow.analysis_result_version for record in reports)
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
        "analysis_status": workflow.analysis_status if has_analysis else "not_generated",
        "analysis_result_version": workflow.analysis_result_version if has_analysis else None,
        "analysis_generated_at": workflow.analysis_generated_at if has_analysis else None,
        "analysis_error": workflow.analysis_error_code if has_analysis else None,
        "available_result_versions": len(reports),
        "source_cleanup": source_storage_repository.cleanup_summary(
            assignment_id=task_id, owner_id=owner_id
        ),
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
    math_plans = _math_plans_for_projection(run_id, questions)

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
            question.q_id: _serialize_problem(
                question, math_plan=math_plans.get(question.q_id),
                feedback_language=((frozen_setup.setup if frozen_setup else {}) or {}).get("feedback_language", "zh"),
            ) for question in questions
        },
        "student_data": student_data,
    }


def result_snapshot(
    *, task_id: str, owner_id: str, result_version: int = 1,
    grading_run_id: str | None = None,
) -> dict:
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    workflow = workflow_repository.get_live_workflow(
        task_id, owner_id=owner_id
    )
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
    workflow = workflow_repository.get_live_workflow(task_id, owner_id=owner_id)
    runs = grading_repository.list_runs_for_assignment(task_id, actor_id=owner_id)
    run = _current_grading_run(workflow, runs)
    if run is None:
        raise NotFound("grading_run")

    def render_manifest(version: int, released_at: float, task_name: str) -> dict:
        # Called under the run/workflow locks before either snapshot or report
        # metadata is written. No provider calls, storage writes or mutations.
        if any(_correction_review_blocks_finalization(result)
               for result in grading_repository.list_results_for_run(run.id)):
            raise ResultNotReleasable("unresolved_failures")
        snapshot = result_snapshot(
            task_id=task_id, owner_id=owner_id, result_version=version,
            grading_run_id=run.id,
        )
        snapshot["created_at"] = released_at
        return build_artifact_manifest(
            task_id=task_id, task_name=task_name, snapshot=snapshot,
            generated_at=time.time(),
        )

    _workflow, _record, created = workflow_repository.generate_result_artifacts_atomic(
        assignment_id=task_id, grading_run_id=run.id, owner_id=owner_id,
        expected_revision=expected_revision, render_manifest=render_manifest,
        artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
    )
    return {
        "status": "ok" if created else "already_done",
        "unchanged": not created,
        **finalization(task_id=task_id, owner_id=owner_id),
        "artifacts": artifact_index(task_id=task_id, owner_id=owner_id),
    }


def artifact_index(*, task_id: str, owner_id: str) -> dict:
    workflow = workflow_repository.get_live_workflow(
        task_id, owner_id=owner_id
    )
    records = _available_artifact_manifests(task_id=task_id, owner_id=owner_id)
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
            "bundle_filename": artifact_download_filename(record.manifest, "bundle"),
            "files": [
                {**item, "filename": artifact_download_filename(record.manifest, item["artifact_id"])}
                for item in record.manifest.get("files", [])
                if is_artifact_enabled(item.get("artifact_id", ""))
            ],
        }
        for record in records
    ]
    has_current = any(item["current"] for item in versions)
    if current > 0 and not has_current:
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
        "analysis_status": (
            "stale" if any(item["current"] and item["status"] == "stale" for item in versions)
            else workflow.analysis_status if has_current else "not_generated"
        ),
        "analysis_result_version": workflow.analysis_result_version if has_current else None,
        "versions": versions,
    }


def delete_historical_artifact(*, task_id: str, owner_id: str, version: int) -> dict:
    workflow = workflow_repository.get_live_workflow(task_id, owner_id=owner_id)
    index = artifact_index(task_id=task_id, owner_id=owner_id)
    report = next((item for item in index["versions"] if item["version"] == version), None)
    if report is None or not report["files"]:
        raise NotFound("artifact_manifest")
    if report["current"] and report["status"] != "stale":
        raise InvalidTransition("Only historical reports can be deleted.")
    workflow_repository.delete_artifact_manifest_atomic(
        assignment_id=task_id, owner_id=owner_id, result_version=version,
        expected_revision=workflow.workflow_revision,
    )
    return artifact_index(task_id=task_id, owner_id=owner_id)


def artifact_bytes(
    *, task_id: str, owner_id: str, version: int, artifact_id: str
) -> tuple[bytes, str, str]:
    workflow_repository.get_live_workflow(task_id, owner_id=owner_id)
    record = workflow_repository.get_artifact_manifest(
        task_id, version, owner_id=owner_id
    )
    if not is_current_artifact_manifest(record.manifest) or not is_artifact_enabled(artifact_id):
        raise NotFound("artifact")
    snapshot = result_snapshot(
        task_id=task_id, owner_id=owner_id, result_version=version,
        grading_run_id=record.grading_run_id,
    )
    if snapshot["fingerprint"] != record.result_fingerprint:
        raise VersionConflict("artifact_source_changed")
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
    if {item.artifact_id for item in files} != {
        key for key in expected_files if is_artifact_enabled(key)
    }:
        raise VersionConflict("artifact_renderer_changed")
    for item in files:
        expected = expected_files.get(item.artifact_id)
        # Download names are presentation; verify content/other metadata here,
        # and authenticate the original stored filename in the manifest below.
        if expected != {**item.metadata(), "filename": expected.get("filename")}:
            raise VersionConflict("artifact_renderer_changed")
    # Verify the original manifest, including paused exports' metadata, without
    # rendering their bytes. Every downloadable file is verified above.
    rebuilt_fingerprint = artifact_fingerprint(
        task_id=task_id,
        task_name=task_name,
        snapshot=snapshot,
        generated_at=record.generated_at,
        files=record.manifest.get("files", []),
    )
    if (
        rebuilt_fingerprint
        != record.manifest.get("artifact_fingerprint")
    ):
        raise VersionConflict("artifact_manifest_changed")
    if artifact_id == "bundle":
        return (
            build_artifact_bundle(files, record.manifest),
            "application/zip",
            artifact_download_filename(record.manifest, "bundle"),
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
